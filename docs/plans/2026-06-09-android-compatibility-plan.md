# Android/Termux Compatibility Implementation Plan

> **For Antigravity:** REQUIRED WORKFLOW: Use `.agent/workflows/execute-plan.md` to execute this plan in single-flow mode.

**Goal:** Implement a comprehensive Termux/Android compatibility layer for VibraVid, including an automated interactive bootstrapping script (`termux_install.sh`) and runtime checks to prevent invalid binary downloads.

**Architecture:** Detect Termux environments programmatically. In Termux, route binary checks to system packages (ffmpeg, bento4, mkvtoolnix) and offer cargo-based compilation for custom binaries (velora). Auto-set the output path to shared Android storage.

**Tech Stack:** Python, Bash, Android ADB.

---

### Task 1: Update setup checkers in checker.py

**Files:**
- Modify: `VibraVid/setup/checker.py`

**Step 1: Write helper function and edit checkers**
Implement `is_termux() -> bool` and update checkers to handle Termux environment:
- If `is_termux()` is true, checks should look at system PATH. If missing, print clean instructions to install them via `pkg` or compile via `cargo` instead of attempting desktop Linux binary downloads.
- In `check_velora()`, if cargo is available, attempt compiling Velora from source and placing it in the binary directory automatically.

**Step 2: Commit changes**
```bash
git add VibraVid/setup/checker.py
git commit -m "feat(setup): add Termux environment checks and cargo compilation for velora"
```

---

### Task 2: Update configuration defaults in config.py

**Files:**
- Modify: `VibraVid/utils/config.py`

**Step 1: Set default root_path for Termux**
In `ConfigManager._load_config`, if `is_termux` is True and `root_path` is not customized (e.g. still "Video" or relative), default it to `/sdcard/Movies/VibraVid`.

**Step 2: Commit changes**
```bash
git add VibraVid/utils/config.py
git commit -m "feat(config): default root_path to shared storage in Termux"
```

---

### Task 3: Create termux_install.sh

**Files:**
- Create: `termux_install.sh`

**Step 1: Write interactive installer script**
Implement a script that:
- Ensures storage permissions.
- Installs `ffmpeg`, `bento4`, `mkvtoolnix`.
- Detects cargo/rust/clang and compiles `velora` if missing.
- Sets up default configs and creates directories.
- Installs VibraVid with proper compiler environment.

**Step 2: Make executable and commit**
```bash
chmod +x termux_install.sh
git add termux_install.sh
git commit -m "feat: add interactive termux_install.sh setup script"
```

---

### Task 4: Local verification

**Step 1: Run verification**
Verify that VibraVid on Mac still works perfectly and doesn't trigger any Termux/Android fallback logic.

**Step 2: Commit**
```bash
git commit --allow-empty -m "test: verify no regressions on Mac desktop"
```
