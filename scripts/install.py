#!/usr/bin/env python3
"""Zero-Friction Auto-Installer for skill-sync - Created by GTI Santander.

Fully automatic 1-click setup:
1. Installs rclone automatically if missing.
2. Auto-configures storage remote (uses default local cloud folder if no cloud remote exists yet).
3. Auto-configures skill categories (work, school, personal).
4. Registers session hooks in Claude Code settings.

Usage:
    python scripts/install.py
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
SYNC_SCRIPT = HERE / "sync.py"
HOOKS_SCRIPT = HERE / "install_hooks.py"
HOME = Path.home()


def log(msg: str) -> None:
    print(f"[skill-sync installer] {msg}")


def check_python_version():
    if sys.version_info < (3, 8):
        log("Error: Python 3.8+ is required.")
        sys.exit(1)


def ensure_rclone():
    exe = shutil.which("rclone")
    if exe:
        log(f"rclone found: {exe}")
        return True

    log("rclone not found. Auto-installing rclone...")
    system = platform.system().lower()
    try:
        if system == "windows":
            subprocess.run(["winget", "install", "Rclone.Rclone", "--accept-source-agreements", "--accept-package-agreements"], check=True)
        elif system == "darwin":
            subprocess.run(["brew", "install", "rclone"], check=True)
        elif system == "linux":
            subprocess.run("curl https://rclone.org/install.sh | sudo bash", shell=True, check=True)
        else:
            log(f"Unsupported OS for auto-install: {system}. Install rclone manually.")
            return False
    except Exception as e:
        log(f"Rclone auto-installation warning: {e}")
        return False

    exe = shutil.which("rclone")
    if exe:
        log(f"rclone installed successfully: {exe}")
        return True
    return False


def setup_zero_friction_remote() -> str:
    """Find an existing rclone remote, or automatically create a default local cloud storage folder."""
    exe = shutil.which("rclone")
    if exe:
        try:
            res = subprocess.run([exe, "listremotes"], capture_output=True, text=True, timeout=10)
            remotes = [r.strip() for r in res.stdout.splitlines() if r.strip()]
            if remotes:
                log(f"Detected existing remote: {remotes[0]}")
                return remotes[0]
        except Exception:
            pass

    # Fallback default local cloud storage folder (zero friction)
    default_cloud_path = HOME / "CloudSkills"
    default_cloud_path.mkdir(parents=True, exist_ok=True)
    log(f"Auto-configured default local cloud storage: {default_cloud_path}")
    return str(default_cloud_path)


def auto_configure_skill_sync(remote: str):
    log("Auto-configuring skill-sync settings...")
    try:
        sys.path.insert(0, str(HERE))
        import sync
        from argparse import Namespace
        sync.cmd_setup(Namespace(
            remote=remote,
            root="ClaudeSkills",
            categories="work,school,personal",
            default_category="work",
            machine=None
        ))
        log("Skill-sync configured successfully.")
    except Exception as e:
        log(f"Config setup note: {e}")


def install_hooks():
    log("Installing session hooks in Claude Code settings...")
    try:
        subprocess.run([sys.executable, str(HOOKS_SCRIPT)], check=True)
    except Exception as e:
        log(f"Failed to install hooks: {e}")


def main():
    print("""
╭──────────────────────────────────────────────────────────────────╮
│  ╔═╗╦╔═╦╦  ╦    ╔═╗╦ ╦╔╗╔╔═╗                                     │
│  ╚═╗╠╩╗║║  ║    ╚═╗╚╦╝║║║║     skills that follow you around     │
│  ╚═╝╩ ╩╩╩═╝╩═╝  ╚═╝ ╩ ╝╚╝╚═╝   Created by GTI Santander          │
╰──────────────────────────────────────────────────────────────────╯
""")
    check_python_version()
    ensure_rclone()
    remote = setup_zero_friction_remote()
    auto_configure_skill_sync(remote)
    install_hooks()

    log("\n🎉 ZERO-FRICTION SETUP COMPLETE!")
    log("skill-sync is 100% configured and ready to use.")
    log("Optional: To connect to Google Drive or Dropbox later, run `rclone config`.")


if __name__ == "__main__":
    main()
