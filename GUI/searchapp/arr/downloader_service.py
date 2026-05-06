# 07.05.26

"""
Downloader Service — replaces the standalone Downloader.py from VibraVidArr.

Instead of spawning a subprocess (`VibraVid --search ...`), this service
directly calls the VibraVid internal streaming API (`get_api(site).search()` /
`start_download()`) using the same pipeline that the GUI uses.
"""

import datetime
import json
import logging
import pathlib
import time
from typing import Any, Dict, Optional

from VibraVid.utils.os import os_manager
from .clients.sonarr_client import SonarrClient
from .clients.radarr_client import RadarrClient

logger = logging.getLogger("ARR")


class ArrDownloaderService:
    """Downloads media by invoking VibraVid's native streaming API pipeline."""

    def __init__(self, sonarr: SonarrClient, radarr: RadarrClient):
        self.sonarr = sonarr
        self.radarr = radarr
        self.last_error: Optional[str] = None

    # ── public ───────────────────────────────────────────

    def download(self, item: dict) -> bool:
        """Dispatch a single missing item (serie or movie) to VibraVid's pipeline."""
        content_type = item.get("content_type")
        if content_type == "serie":
            return self._process_serie(item)
        elif content_type == "movie":
            return self._process_movie(item)
        else:
            logger.error(f"Unknown content_type: {content_type}")
            return False

    # ── serie ────────────────────────────────────────────

    def _process_serie(self, serie: dict) -> bool:
        from searchapp.views import _run_download_in_thread
        self.last_error = None

        title = serie["title"]
        series_id = serie.get("id")
        provider = serie.get("provider", "streamingcommunity")
        any_success = False

        # Resolve original title from Sonarr or TMDB
        tmdb_id = serie.get("tmdbId")
        original_title = self._resolve_sonarr_title(title, series_id, tmdb_id)
        search_title = original_title or title
        logger.info(f"[_process_serie] Title='{title}', Original='{original_title}', Search='{search_title}', TMDB ID='{tmdb_id}'")

        year = serie.get("year")
        year_range = self._build_year_range(year)

        for season in serie.get("seasons", []):
            season_num = season["number"]
            for episode in season.get("episodes", []):
                ep_num = episode["episodeNumber"]
                ep_id = episode.get("id")

                if not ep_id:
                    logger.warning(
                        f"S{season_num}E{ep_num} of '{title}' has no episode ID, skipping"
                    )
                    continue

                if self.sonarr.is_episode_in_queue(ep_id):
                    logger.info(f"S{season_num}E{ep_num} of '{title}' already in Sonarr queue, skipping")
                    continue

                display_title = f"{search_title} - S{season_num} E{ep_num}"
                logger.info(f"⏳ Downloading '{display_title}' via {provider}")

                item_payload = self._search_and_build_payload(
                    search_title, provider,
                    year_range=year_range,
                    expected_title=search_title,
                    expected_year=year,
                    tmdb_id=serie.get("tmdbId"),
                    media_type="tv",
                )
                if not item_payload:
                    logger.error(f"✖️ Could not find '{search_title}' on {provider}")
                    self.last_error = "search_no_results"
                    continue

                # Use Sonarr's path for the series, fallback to OUTPUT config root
                series_root = serie.get("path", "")
                if not series_root:
                    series_root = self._fallback_series_root(title)

                # Target folder: series root + season subfolder
                target_folder = str(pathlib.Path(series_root).joinpath(f"S{season_num:02d}"))
                logger.info(f"[S{season_num}E{ep_num}] Target folder (Sonarr's path): '{target_folder}'")

                # Download directly to Sonarr's path
                future = _run_download_in_thread(
                    site=provider,
                    item_payload=item_payload,
                    season=str(season_num),
                    episodes=str(ep_num),
                    media_type="Serie",
                    output_path=target_folder,
                )
                any_success = True

                try:
                    future.result(timeout=7200)  # wait for download to actually finish
                    time.sleep(2)

                    # Get series root path for rescan
                    series_root = serie.get("path", "")
                    if not series_root:
                        series_root = self._fallback_series_root(title)
                    logger.info(f"[S{season_num}E{ep_num}] Using series root path: '{series_root}'")
                    season_folder = str(pathlib.Path(series_root).joinpath(f"S{season_num:02d}"))

                    # Get the EXACT title and year that the website returned, because VibraVid saves using those
                    result_name = item_payload.get("name", search_title)
                    result_year = item_payload.get("year", year)
                    
                    # VibraVid's actual output folder (from Sonarr's perspective)
                    vibrativo_folder = self._get_vibrativo_serie_output(series_root, result_name, season_num, result_year)
                    
                    # Update Sonarr's root path for the series to match VibraVid's output folder
                    if vibrativo_folder:
                        self.sonarr.update_series_path(serie["id"], vibrativo_folder)

                    # Rescan series on the new path
                    try:
                        self.sonarr.command_rescan_series(serie["id"])
                        time.sleep(1)
                        self.sonarr.command_downloaded_episodes_scan(vibrativo_folder)
                        logger.info(f"Rescan completed for S{season_num}E{ep_num}")
                    except Exception as scan_exc:
                        logger.warning(f"Rescan failed: {scan_exc}")

                    # Verify import state without manual import payload
                    imported = False
                    for _ in range(24):  # Wait up to 120 seconds
                        try:
                            episode = self.sonarr.get_episode(ep_id)
                            if episode.get("hasFile") or episode.get("episodeFileId"):
                                imported = True
                                break
                        except Exception as exc:
                            logger.warning(f"Failed to verify Sonarr episode import: {exc}")
                        time.sleep(5)
                    if not imported:
                        logger.error(f"S{season_num}E{ep_num} import not confirmed in Sonarr")
                        self.last_error = "import_not_confirmed"
                        any_success = False
                        continue

                    self.sonarr.set_episode_unmonitored([ep_id])
                    logger.info(f"S{season_num}E{ep_num} of '{title}' completed and unmonitored")
                except Exception as exc:
                    logger.error(f"S{season_num}E{ep_num} of '{title}' failed: {exc}")
                    self.last_error = str(exc)
                    # Don't unmonitor on failure → stays in Sonarr's wanted list for retry
                    any_success = False

        return any_success

    # ── movie ────────────────────────────────────────────

    def _process_movie(self, movie: dict) -> bool:
        from searchapp.views import _run_download_in_thread
        self.last_error = None

        title = movie["title"]
        movie_id = movie["id"]
        tmdb_id = movie.get("tmdbId")
        provider = movie.get("provider", "streamingcommunity")

        if self.radarr.is_movie_in_queue(movie_id):
            logger.info(f"'{title}' already in Radarr queue, skipping")
            return False

        # Resolve original title from Radarr
        original_title = self._resolve_radarr_title(movie_id)
        search_title = original_title or title

        year = movie.get("year")
        year_range = self._build_year_range(year)

        logger.info(f"⏳ Downloading movie '{search_title}' ({year}) via {provider}")

        item_payload = self._search_and_build_payload(
            search_title, provider,
            year_range=year_range,
            expected_title=search_title,
            expected_year=year,
            tmdb_id=tmdb_id,
            media_type="movie",
        )
        if not item_payload:
            logger.error(f"Could not find movie '{search_title}' on {provider}")
            self.last_error = "search_no_results"
            return False

        # Use Radarr's path for the movie, fallback to OUTPUT config root
        target_folder = movie.get("path", "")
        if not target_folder:
            target_folder = self._fallback_movie_root(title)
        logger.info(f"[_process_movie] Target folder (Radarr's path): '{target_folder}'")

        future = _run_download_in_thread(
            site=provider,
            item_payload=item_payload,
            season=None,
            episodes=None,
            media_type="Film",
            output_path=target_folder,
        )

        try:
            future.result(timeout=7200)  # wait for download to actually finish
            time.sleep(2)

            # Get movie root path for manual import
            movie_root = movie.get("path", "")
            if not movie_root:
                movie_root = self._fallback_movie_root(title)

            # Get the EXACT title and year that the website returned
            result_name = item_payload.get("name", search_title)
            result_year = item_payload.get("year", year)

            # VibraVid's actual output folder (from Radarr's perspective)
            vibrativo_folder = self._get_vibrativo_movie_output(movie_root, result_name, result_year)
            
            # Update Radarr's root path for the movie
            if vibrativo_folder:
                self.radarr.update_movie_path(movie_id, vibrativo_folder)

            # Rescan movie on the new path
            try:
                self.radarr.command_rescan_movie(movie_id)
                time.sleep(1)
                self.radarr.command_downloaded_movies_scan(vibrativo_folder)
                logger.info(f"Rescan completed for '{title}'")
            except Exception as scan_exc:
                logger.warning(f"Rescan failed: {scan_exc}")

            # Verify import state without manual import payload
            imported = False
            for _ in range(60):  # Wait up to 300 seconds
                try:
                    movie_obj = self.radarr.get_movie_by_id(movie_id)
                    if movie_obj.get("hasFile") or movie_obj.get("movieFileId"):
                        imported = True
                        break
                except Exception as exc:
                    logger.warning(f"Failed to verify Radarr movie import: {exc}")
                time.sleep(5)
            if not imported:
                logger.error(f"Movie '{title}' import not confirmed in Radarr")
                self.last_error = "import_not_confirmed"
                return False

            self.radarr.set_movie_unmonitored(movie_id)
            logger.info(f"'{title}' completed and unmonitored")
            return True
        except Exception as exc:
            logger.error(f"'{title}' failed: {exc}")
            self.last_error = str(exc)
            # Don't unmonitor on failure → stays in Radarr's wanted list for retry
            return False

    # ── helpers ──────────────────────────────────────────

    @staticmethod
    def _verify_title_match(result_name: str, expected_title: str,
                            result_year: Optional[int] = None,
                            expected_year: Optional[int] = None) -> bool:
        """Verify a search result matches the expected title/year from ARR metadata.

        Uses normalized string comparison (lowercase, accents removed, punctuation stripped).
        """
        if not result_name or not expected_title:
            return False

        import re
        import unicodedata

        def normalize(s: str) -> str:
            """Normalize: lowercase, remove accents, remove punctuation, collapse spaces."""
            s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
            s = re.sub(r'[^\w\s]', ' ', s.lower())
            s = re.sub(r'\s+', ' ', s).strip()
            return s

        rn = normalize(result_name)
        et = normalize(expected_title)

        # Exact match or one contains the other (after normalization)
        if rn == et or et in rn or rn in et:
            # Year check with +/- 1 year tolerance
            if expected_year is not None and result_year is not None:
                try:
                    return abs(int(result_year) - int(expected_year)) <= 1
                except (ValueError, TypeError):
                    pass
            return True

        return False

    def _search_and_build_payload(self, title: str, provider: str,
                                  year_range: Optional[str] = None,
                                  expected_title: Optional[str] = None,
                                  expected_year: Optional[int] = None,
                                  tmdb_id: Optional[int] = None,
                                  media_type: str = "tv") -> Optional[Dict[str, Any]]:
        """Search VibraVid's streaming API for a title and return an item_payload dict.

        Uses TMDB API to get alternative titles for verification when tmdb_id is provided.
        This handles translations (e.g., "Born Again" vs "Rinascita") correctly.
        """
        try:
            from searchapp.api import get_api

            api = get_api(provider)

            # Get alternative titles from TMDB if tmdb_id is available
            tmdb_titles = []
            if tmdb_id:
                try:
                    from VibraVid.utils.tmdb_client import tmdb_client as tmdb
                    # Get titles in Italian (for streamingcommunity) and English
                    for lang in ["it", "en"]:
                        alt_titles = tmdb.get_alternative_titles(tmdb_id, media_type, lang)
                        tmdb_titles.extend(alt_titles)
                    # Deduplicate
                    tmdb_titles = list(set(t.strip() for t in tmdb_titles if t.strip()))
                    logger.info(f"TMDB alternative titles for {tmdb_id}: {tmdb_titles[:5]}")
                except Exception as tmdb_exc:
                    logger.debug(f"Failed to get TMDB alternative titles: {tmdb_exc}")

            # Search using the title
            results = api.search(title)

            if not results:
                logger.warning(f"No search results for '{title}' on {provider}")
                return None

            # Parse year range into integers
            year_start = None
            year_end = None
            if year_range:
                try:
                    parts = year_range.split("-")
                    year_start = int(parts[0])
                    year_end = int(parts[1])
                except (ValueError, IndexError):
                    logger.debug(f"Could not parse year_range '{year_range}'")

            verify_title = expected_title or title

            # Build list of titles to match against
            titles_to_match = [verify_title] + tmdb_titles

            # Scan results: if year matches, accept first result
            best = None
            for r in results:
                # Year range check
                if year_start is not None and year_end is not None:
                    if not r.year:
                        continue
                    try:
                        r_year = int(r.year)
                        if not (year_start <= r_year <= year_end):
                            continue
                    except (ValueError, TypeError):
                        continue

                # Accept this result (year matches)
                best = r
                logger.info(f"Accepted: '{r.name}' ({r.year}) - year matches")
                break

            # Fallback: if year matches, accept first result (handles translations)
            if best is None and results:
                first = results[0]
                if first.year:
                    try:
                        r_year = int(first.year)
                        if year_start is None or year_end is None or (year_start <= r_year <= year_end):
                            best = first
                            logger.info(f"Accepted first result by year match: '{first.name}' ({first.year})")
                    except (ValueError, TypeError):
                        pass

            # Don't fallback to random results - skip if no verified match
            if best is None:
                logger.error(
                    f"No verified match found for '{verify_title}' "
                    f"(year_range={year_range}). Skipping download. "
                    f"First result was: '{results[0].name}' ({results[0].year})"
                )
                return None

            return {**best.__dict__, "is_movie": best.is_movie}

        except Exception as exc:
            logger.error(f"Search failed for '{title}' on {provider}: {exc}")
            return None

    def _resolve_sonarr_title(self, title: str, series_id: Optional[int], tmdb_id: Optional[int] = None) -> Optional[str]:
        """Try to get the original title from Sonarr for better search results.

        First tries Sonarr's originalTitle. If not set, falls back to TMDB API
        to get the Italian title directly from tmdbId.
        """
        sonarr_original = None

        # Primary: fast lookup by ID
        if series_id:
            try:
                series = self.sonarr.get_series_by_id(series_id)
                sonarr_title = series.get("title", "")
                sonarr_original = series.get("originalTitle", "")
                logger.info(f"[_resolve_sonarr_title] Sonarr title='{sonarr_title}', originalTitle='{sonarr_original}'")

                if sonarr_original and sonarr_original.lower() != title.lower():
                    logger.info(f"Using original title from Sonarr: '{sonarr_original}'")
                    return sonarr_original
            except Exception as exc:
                logger.debug(f"Sonarr series lookup by ID {series_id} failed: {exc}")

        # Fallback: get Italian title from TMDB if originalTitle is not set
        if tmdb_id and (not sonarr_original or sonarr_original.lower() == title.lower()):
            try:
                from VibraVid.utils.tmdb_client import tmdb_client as tmdb
                details = tmdb._make_request(f"tv/{tmdb_id}", {"language": "it"})
                it_title = details.get("name", "")
                if it_title and it_title.lower() != title.lower():
                    logger.info(f"Using Italian title from TMDB: '{it_title}'")
                    return it_title
            except Exception as tmdb_exc:
                logger.debug(f"Failed to get Italian title from TMDB: {tmdb_exc}")

        # Fallback: search all series by title (mirrors old Downloader.py)
        try:
            series_list = self.sonarr.get_series()
            title_lower = title.lower()
            for s in series_list:
                s_title = s.get("title", "").lower()
                s_slug = s.get("titleSlug", "").lower()
                s_original = s.get("originalTitle", "").lower()
                if title_lower in (s_title, s_slug, s_original):
                    original = s.get("originalTitle")
                    if original and original.lower() != title_lower:
                        logger.info(f"Using original title from Sonarr (fallback): '{original}'")
                        return original
                    break
        except Exception as exc:
            logger.debug(f"Sonarr series list fallback failed: {exc}")

        return None

    def _resolve_radarr_title(self, movie_id: int) -> Optional[str]:
        """Try to get the original title from Radarr.

        Falls back to scanning all movies by title if the ID lookup fails."""
        try:
            movie = self.radarr.get_movie_by_id(movie_id)
            original = movie.get("originalTitle")
            if original:
                logger.info(f"Using original title from Radarr: '{original}'")
                return original
        except Exception as exc:
            logger.debug(f"Radarr movie lookup by ID {movie_id} failed: {exc}")

        return None

    @staticmethod
    def _build_year_range(year) -> Optional[str]:
        if not year:
            return None
        try:
            y = int(year)
            now = datetime.datetime.now().year
            if y >= (now - 1):
                return f"{y}-9999"
            else:
                return f"{y}-{y + 1}"
        except (ValueError, TypeError):
            return None

    def _fallback_series_root(self, title: str) -> str:
        from VibraVid.utils import config_manager
        base = config_manager.config.get("OUTPUT", "root_path")
        folder = config_manager.config.get("OUTPUT", "serie_folder_name")
        return str(pathlib.Path(base).joinpath(folder, title))

    def _fallback_movie_root(self, title: str) -> str:
        from VibraVid.utils import config_manager
        base = config_manager.config.get("OUTPUT", "root_path")
        folder = config_manager.config.get("OUTPUT", "movie_folder_name")
        return str(pathlib.Path(base).joinpath(folder, title))

    def _get_vibrativo_serie_output(self, arr_series_path: str, search_title: str, season_num: int, year: Optional[int] = None) -> str:
        """Compute the VibraVid output path relative to Sonarr's root folder."""
        if not arr_series_path:
            return ""
        try:
            from VibraVid.services._base.tv_display_manager import map_episode_path
            import pathlib
            
            # Pass the year as string if available to match VibraVid's exact logic
            series_year = str(year) if year else None
            path_components, _ = map_episode_path(series_name=search_title, series_year=series_year, season_number=season_num)
            
            if "\\" in arr_series_path:
                root = pathlib.PureWindowsPath(arr_series_path).parent
            else:
                root = pathlib.PurePosixPath(arr_series_path).parent
                
            # Append ONLY the series folder (path_components[0]), ignoring the season subfolder
            if path_components:
                root = root / path_components[0]
                
            return str(root)
        except Exception as exc:
            logger.debug(f"Could not compute VibraVid serie output path: {exc}")
        return ""

    def _get_vibrativo_movie_output(self, arr_movie_path: str, search_title: str, year: Optional[int] = None) -> str:
        """Compute the VibraVid output path relative to Radarr's root folder."""
        if not arr_movie_path:
            return ""
        try:
            from VibraVid.services._base.tv_display_manager import map_movie_path
            import pathlib
            
            # Pass the year as string if available
            title_year = str(year) if year else None
            path_components, _ = map_movie_path(title_name=search_title, title_year=title_year)
            
            if "\\" in arr_movie_path:
                root = pathlib.PureWindowsPath(arr_movie_path).parent
            else:
                root = pathlib.PurePosixPath(arr_movie_path).parent
                
            for part in path_components:
                root = root / part
            return str(root)
        except Exception as exc:
            logger.debug(f"Could not compute VibraVid movie output path: {exc}")
        return ""

    def _confirm_episode_import(self, series_id: int, episode_id: int,
                                scan_folders: Optional[list] = None,
                                season_folder: Optional[str] = None) -> bool:
        """Try to import episode files from each candidate folder into Sonarr."""
        # Back-compat: accept the old season_folder kwarg
        if scan_folders is None:
            scan_folders = [season_folder] if season_folder else []

        for folder in scan_folders:
            if not folder:
                continue
            try:
                lookup_items = self.sonarr.manual_import_lookup(folder, series_id=series_id)
                import_payload = []
                for item in lookup_items:
                    path = str(item.get("path", "")).strip()
                    if not path:
                        continue
                    
                    # Sonarr v3 requires seriesId and episodeIds at root level for POST
                    ep_ids = [ep["id"] for ep in item.get("episodes", []) if "id" in ep]
                    if not ep_ids:
                        ep_ids = [episode_id]  # Fallback to the requested episode if not parsed
                        
                    post_item = dict(item)
                    post_item["seriesId"] = series_id
                    post_item["episodeIds"] = ep_ids
                    import_payload.append(post_item)

                if import_payload:
                    self.sonarr.manual_import(import_payload)
                    logger.info(f"Manual import submitted for {len(import_payload)} file(s) from '{folder}'")
                    break
            except Exception as exc:
                logger.warning(f"Sonarr manual import from '{folder}' failed: {exc}")

        # Verify import state: episode must have an attached file id.
        for _ in range(24):  # Wait up to 120 seconds
            try:
                episode = self.sonarr.get_episode(episode_id)
                if episode.get("hasFile") or episode.get("episodeFileId"):
                    return True
            except Exception as exc:
                logger.warning(f"Failed to verify Sonarr episode import: {exc}")
            time.sleep(5)

        return False

    def _confirm_movie_import(self, movie_id: int,
                              scan_folders: Optional[list] = None,
                              movie_root: Optional[str] = None) -> bool:
        """Try to import movie files from each candidate folder into Radarr."""
        # Back-compat: accept the old movie_root kwarg
        if scan_folders is None:
            scan_folders = [movie_root] if movie_root else []

        for folder in scan_folders:
            if not folder:
                continue
            try:
                lookup_items = self.radarr.manual_import_lookup(folder, movie_id=movie_id)
                import_payload = []
                for item in lookup_items:
                    path = str(item.get("path", "")).strip()
                    if not path:
                        continue
                        
                    post_item = dict(item)
                    post_item["movieId"] = movie_id
                    import_payload.append(post_item)
                    
                if import_payload:
                    self.radarr.manual_import(import_payload)
                    logger.info(f"Manual import submitted for {len(import_payload)} file(s) from '{folder}'")
                    break
            except Exception as exc:
                logger.warning(f"Radarr manual import from '{folder}' failed: {exc}")

        # Verify import state: movie must have an attached file id or hasFile=True.
        for _ in range(60):  # Wait up to 300 seconds
            try:
                movie = self.radarr.get_movie_by_id(movie_id)
                if movie.get("hasFile") or movie.get("movieFileId"):
                    return True
            except Exception as exc:
                logger.warning(f"Failed to verify Radarr movie import: {exc}")
            time.sleep(5)
        return False
