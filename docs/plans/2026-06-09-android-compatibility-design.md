# Android/Termux Compatibility Design

## Goal
Extend VibraVid's compatibility to Android/Termux by:
1. Providing an interactive installation and configuration script (`termux_install.sh`).
2. Modifying runtime binary checks to detect Termux and prevent download of incompatible Linux binaries, showing helpful messages instead.
3. Automatically defaulting output storage to shared Android Movies storage (`/sdcard/Movies/VibraVid`) under Termux.

## Architecture

### 1. `termux_install.sh`
A Bash bootstrapping script to be run by the user in Termux. It will:
- Check for Termux and storage permissions (`termux-setup-storage`).
- Install system dependencies (`python`, `ffmpeg`, `bento4`, `mkvtoolnix`).
- Detect/install Rust and Clang, then compile `Velora` from source and copy the binary to `~/.local/bin/binary/velora`.
- Set default output path to `/sdcard/Movies/VibraVid` and create a symlink `~/Video`.
- Install VibraVid with Python compiler settings optimized for Android (setting `ANDROID_API_LEVEL=24`).
- Create a lowercase command alias `vibravid` pointing to the entrypoint.

### 2. Runtime Python Modifications
We will modify the following Python modules:
- **`VibraVid/setup/checker.py`**:
  - Add `is_termux()` check.
  - If in Termux, check system PATH. If a tool is missing, do not download desktop Linux binaries. Instead, show a warning asking the user to run `termux_install.sh` or manually run the respective `pkg` or `cargo` command.
- **`VibraVid/utils/config.py`**:
  - If in Termux and `root_path` is default/relative (`Video`), dynamically default it to `/sdcard/Movies/VibraVid`.

## Testing Plan
- Test on Mac (ensure no behavior is broken for standard installations).
- Test on the Android tablet using ADB shell simulating Termux environment conditions.
