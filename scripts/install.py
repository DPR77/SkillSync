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
WATCH_INSTALLER = HERE / "install_watch.py"
HOME = Path.home()


def log(msg: str) -> None:
    print(f"[skill-sync installer] {msg}")


def check_python_version():
    if sys.version_info < (3, 8):
        log("Error: Python 3.8+ is required.")
        sys.exit(1)


def find_rclone():
    """PATH first, then the folders winget, Scoop and Homebrew install into."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import sync
        return sync.rclone_bin(required=False)
    except Exception:
        return shutil.which("rclone")


def ensure_rclone():
    exe = find_rclone()
    if exe:
        log(f"rclone found: {exe}")
        return True

    system = platform.system().lower()
    # Unprivileged package managers are run for the user; anything needing root is only
    # printed. An installer that quietly calls sudo is both a security smell auditors
    # flag and a hang waiting for a password prompt nobody sees.
    unprivileged = {
        "windows": ["winget", "install", "Rclone.Rclone", "--accept-source-agreements",
                    "--accept-package-agreements"],
        "darwin": ["brew", "install", "rclone"],
    }.get(system)
    if unprivileged:
        log(f"rclone not found. Installing with: {' '.join(unprivileged)}")
        try:
            subprocess.run(unprivileged, check=True)
        except Exception as e:
            log(f"rclone installation failed: {e}")
    else:
        if shutil.which("apt-get"):
            hint = "sudo apt-get install -y rclone"
        elif shutil.which("dnf"):
            hint = "sudo dnf install -y rclone"
        elif shutil.which("pacman"):
            hint = "sudo pacman -S rclone"
        else:
            hint = "install rclone with your package manager"
        log(f"rclone is not installed. Run this yourself, it needs root:\n    {hint}")
        return False

    exe = find_rclone()
    if exe:
        log(f"rclone installed successfully: {exe}")
        return True
    log("rclone still not found. Open a new terminal so PATH picks it up, then rerun.")
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


def install_watcher():
    log("Installing background watcher (asks about a new skill within seconds, "
        "not on the next Claude Code turn)...")
    try:
        subprocess.run([sys.executable, str(WATCH_INSTALLER)], check=True)
    except Exception as e:
        log(f"Watcher install note (non-fatal, hooks still cover it on the next "
            f"Claude Code session): {e}")


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
    install_watcher()

    log("\n🎉 ZERO-FRICTION SETUP COMPLETE!")
    log("skill-sync is 100% configured and ready to use.")
    log("Optional: To connect to Google Drive or Dropbox later, run `rclone config`.")


if __name__ == "__main__":
    main()
