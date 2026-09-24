#!/usr/bin/env python3
"""Zero-Friction Auto-Installer for skill-sync - Created by GTI Santander.

One-command setup:
1. Installs rclone if missing - package manager first, official checksum-verified
   download second, so a machine with no admin rights is not a dead end.
2. Auto-configures storage remote (uses default local cloud folder if no cloud remote exists yet).
3. Auto-configures skill categories (work, school, personal).
4. Registers session hooks in Claude Code settings.

The login watcher is *not* installed unless it is asked for with --watch. It is the one
piece that survives a reboot, and something that plants an autostart entry as a side
effect of "install" is exactly what a security review should object to. The Stop hook
already covers new skills on the next session.

Usage:
    python scripts/install.py [--watch] [--no-hooks]
"""

from __future__ import annotations

import argparse
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


sys.path.insert(0, str(HERE))
import provision  # noqa: E402  (local module, path set above)


def find_rclone():
    return provision.find_rclone()


def ensure_rclone():
    """Package manager if there is an unprivileged one, verified download otherwise."""
    exe = provision.ensure_rclone(log=log)
    if not exe:
        log("rclone could not be installed automatically. Install it yourself, then rerun:")
        log("    Windows: winget install Rclone.Rclone")
        log("    macOS:   brew install rclone")
        log("    Linux:   sudo apt install rclone | sudo dnf install rclone")
    return bool(exe)


def setup_zero_friction_remote() -> str:
    """Find an existing rclone remote, or automatically create a default local cloud storage folder."""
    exe = provision.find_rclone()
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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--watch", action="store_true",
                        help="also start the new-skill watcher at login (autostart entry)")
    parser.add_argument("--no-hooks", action="store_true",
                        help="do not register the Claude Code session hooks")
    args = parser.parse_args(argv)

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
    if not args.no_hooks:
        install_hooks()
    if args.watch:
        install_watcher()

    log("\n🎉 SETUP COMPLETE!")
    log("skill-sync is configured and ready to use.")
    log("Optional: To connect to Google Drive or Dropbox later, run `rclone config`.")
    if not args.watch:
        log("Optional: `python scripts/install_watch.py` asks about a new skill within "
            "seconds instead of on the next Claude Code turn. It adds a login item.")


if __name__ == "__main__":
    main()
