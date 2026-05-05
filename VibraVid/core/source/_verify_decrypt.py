# 05.05.26

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple


logger = logging.getLogger(__name__)


def _resolve(path_or_name: str) -> Optional[str]:
    if os.path.isabs(path_or_name) and os.path.isfile(path_or_name):
        return path_or_name
    return shutil.which(path_or_name)


def _ffprobe_streams(ffprobe: str, file_path: str) -> Tuple[bool, str]:
    """Return (ok, message). ok=True means at least one decodable stream."""
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_streams",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "ffprobe timed out"
    
    except Exception as exc:
        return False, f"ffprobe failed to launch: {exc}"

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return False, f"ffprobe exit={result.returncode}: {output.strip()[:200]}"

    streams: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if line == "[STREAM]":
            current = {}
        elif line == "[/STREAM]":
            streams.append(current)
            current = {}
        elif "=" in line:
            key, _, value = line.partition("=")
            current[key.strip()] = value.strip()

    if not streams:
        return False, "ffprobe reported no streams"

    media_streams = [s for s in streams if s.get("codec_type", "") in {"video", "audio", "subtitle"}]
    if not media_streams:
        codec_names = ", ".join(s.get("codec_name", "?") for s in streams) or "(none)"
        return False, f"no audio/video stream (codec_type=data only): {codec_names}"

    bad = [s for s in media_streams if s.get("codec_name", "unknown") in {"unknown", "none", ""}]
    if bad:
        return False, "ffprobe still reports unknown codec — file likely encrypted"

    summary = ", ".join(f"{s.get('codec_type','?')}={s.get('codec_name','?')}" for s in media_streams)
    return True, summary


def _mp4dump_clean(mp4dump: str, file_path: str) -> Tuple[bool, str]:
    """
    Best-effort encryption-residue scan with Bento4's mp4dump.
    """
    try:
        result = subprocess.run(
            [mp4dump, "--verbosity", "0", file_path],
            capture_output=True,
            timeout=30,
        )
    
    except subprocess.TimeoutExpired:
        return True, "mp4dump timed out (skipped)"
    except Exception as exc:
        return True, f"mp4dump failed to launch: {exc} (skipped)"

    if result.returncode != 0:
        return True, "mp4dump non-zero exit (skipped)"

    text = ""
    for enc in ("utf-8", "utf-16", "utf-16-le", "latin-1"):
        try:
            text = result.stdout.decode(enc).lstrip("\ufeff")
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return True, "mp4dump produced no decodable output (skipped)"

    flagged = [
        marker
        for marker in ("[encv]", "[enca]", "[sinf]", "[saiz]", "[saio]", "[senc]")
        if marker in text.lower()
    ]
    if flagged:
        return False, f"residual encryption boxes: {','.join(flagged)}"
    return True, "no residual encryption boxes"



def _scan_mp4_boxes_for_encryption(file_path: str, max_bytes: int = 4 * 1024 * 1024) -> Tuple[bool, str]:
    """
    Lightweight, dependency-free scan: read up to *max_bytes* of the file and
    look for fully-decrypted hallmarks (no [encv]/[enca]/[sinf]/[saiz]/[saio]/[senc]
    boxes inside moov/moof) without invoking external tools.
    """
    try:
        with open(file_path, "rb") as fh:
            data = fh.read(max_bytes)
    except Exception as exc:
        return True, f"box scan skipped: {exc}"

    if not data:
        return True, "box scan skipped: empty"

    encryption_types = {b"encv", b"enca", b"sinf", b"saiz", b"saio", b"senc"}
    found = set()
    pos = 0
    end = len(data)
    while pos + 8 <= end:
        size = int.from_bytes(data[pos:pos + 4], "big")
        type_ = data[pos + 4:pos + 8]
        if size == 1:
            if pos + 16 > end:
                break
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
        if size <= 0:
            # malformed/streaming variant -> stop, don't false-flag
            break
        if type_ in encryption_types:
            found.add(type_.decode("ascii"))

        # heuristic: also scan inside container boxes by sliding 1 byte
        # if we are inside moov/trak/mdia/minf/stbl. For the simple top-level
        # walk we step by ``size``.
        pos += size

    # Fallback: if we didn't enter container traversal, also do a substring
    # scan of the first window — boxes' 4-cc are unique enough that false
    # positives are negligible for these short tags.
    if not found:
        for marker in encryption_types:
            if marker in data:
                found.add(marker.decode("ascii"))

    if found:
        return False, f"residual encryption boxes detected: {','.join(sorted(found))}"
    return True, "no residual encryption boxes (built-in scan)"


def verify_decrypted_media(file_path, *, ffprobe_path: Optional[str] = None, mp4dump_path: Optional[str] = None) -> Tuple[bool, str]:
    """
    Verify that *file_path* is a playable, fully decrypted media file.
    """
    p = Path(file_path)
    if not p.exists():
        return False, "output file missing"
    if p.stat().st_size == 0:
        return False, "output file is empty"

    # Resolve binaries lazily — keep this module free of project-wide imports.
    if ffprobe_path is None:
        try:
            from VibraVid.setup import get_ffprobe_path

            ffprobe_path = get_ffprobe_path() or _resolve("ffprobe")
        except Exception:
            ffprobe_path = _resolve("ffprobe")
    else:
        ffprobe_path = _resolve(ffprobe_path)

    if mp4dump_path is None:
        try:
            from VibraVid.setup import get_mp4dump_path

            mp4dump_path = get_mp4dump_path() or _resolve("mp4dump")
        except Exception:
            mp4dump_path = _resolve("mp4dump")
    else:
        mp4dump_path = _resolve(mp4dump_path)

    if not ffprobe_path:
        logger.warning("ffprobe not available; skipping codec-level verification")
        return True, "skipped (ffprobe missing)"

    ok, ffprobe_msg = _ffprobe_streams(ffprobe_path, str(p))
    if not ok:
        return False, ffprobe_msg

    mp4dump_msg = ""
    if mp4dump_path:
        clean, mp4dump_msg = _mp4dump_clean(mp4dump_path, str(p))
        if not clean:
            return False, f"{ffprobe_msg}; {mp4dump_msg}"
        if "skipped" not in mp4dump_msg:
            return True, f"{ffprobe_msg}; {mp4dump_msg}"

    # Fallback: built-in box scanner — runs even when Bento4 mp4dump is
    # absent or shadowed by an unrelated binary on PATH.
    clean, scan_msg = _scan_mp4_boxes_for_encryption(str(p))
    if not clean:
        return False, f"{ffprobe_msg}; {scan_msg}"
    detail = scan_msg if not mp4dump_msg else f"{mp4dump_msg}; {scan_msg}"
    return True, f"{ffprobe_msg}; {detail}"