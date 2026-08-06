#!/usr/bin/env python3
"""Register watch_new_skills.py to start automatically at login, and start it now.

Cross-platform, no admin/root needed:
  Windows -> a .vbs launcher dropped in the Startup folder (runs pythonw.exe hidden)
  macOS   -> a LaunchAgent plist in ~/Library/LaunchAgents, loaded with launchctl
  Linux   -> a systemd --user unit, enabled and started; falls back to an XDG
             autostart .desktop entry if systemd --user isn't available

Usage:
    python install_watch.py            # install autostart + start it now
    python install_watch.py --uninstall
    python install_watch.py --start-only   # just run it now, skip autostart registration
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WATCH_SCRIPT = HERE / "watch_new_skills.py"
HOME = Path.home()
NAME = "skill-sync-watch"


def console_python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def windowless_python() -> str:
    """pythonw.exe on Windows if available (no console flash at login), else python."""
    exe = Path(console_python())
    if sys.platform == "win32":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def start_now() -> bool:
    """Launch the watcher for the current session, detached, no console window."""
    try:
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            subprocess.Popen([windowless_python(), str(WATCH_SCRIPT)],
                              startupinfo=si, close_fds=True,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            subprocess.Popen([windowless_python(), str(WATCH_SCRIPT)],
                              close_fds=True, start_new_session=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"warning: could not start the watcher now: {e!r}", file=sys.stderr)
        return False


# --------------------------------------------------------------------- Windows

def windows_startup_dir() -> Path:
    import os
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def install_windows():
    startup = windows_startup_dir()
    startup.mkdir(parents=True, exist_ok=True)
    vbs = startup / f"{NAME}.vbs"
    pyw = windowless_python()
    # WScript.Shell.Run(..., 0, False): window style 0 = hidden, no wait.
    vbs.write_text(
        'Set shell = CreateObject("WScript.Shell")\r\n'
        f'shell.Run """{pyw}"" ""{WATCH_SCRIPT}""", 0, False\r\n',
        encoding="utf-8",
    )
    print(f"installed: {vbs}")


def uninstall_windows():
    vbs = windows_startup_dir() / f"{NAME}.vbs"
    if vbs.exists():
        vbs.unlink()
        print(f"removed: {vbs}")
    else:
        print("nothing to remove (not installed)")


# --------------------------------------------------------------------- macOS

def macos_plist_path() -> Path:
    return HOME / "Library" / "LaunchAgents" / f"com.{NAME}.plist"


def install_macos():
    plist = macos_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    py = console_python()
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.{NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{py}</string>
        <string>{WATCH_SCRIPT}</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/dev/null</string>
    <key>StandardErrorPath</key><string>/dev/null</string>
</dict>
</plist>
""", encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(plist)], check=False,
                    capture_output=True)
    subprocess.run(["launchctl", "load", str(plist)], check=False, capture_output=True)
    print(f"installed: {plist}")


def uninstall_macos():
    plist = macos_plist_path()
    subprocess.run(["launchctl", "unload", str(plist)], check=False, capture_output=True)
    if plist.exists():
        plist.unlink()
        print(f"removed: {plist}")
    else:
        print("nothing to remove (not installed)")


# --------------------------------------------------------------------- Linux

def linux_systemd_unit() -> Path:
    return HOME / ".config" / "systemd" / "user" / f"{NAME}.service"


def linux_autostart_desktop() -> Path:
    return HOME / ".config" / "autostart" / f"{NAME}.desktop"


def install_linux():
    py = console_python()
    if shutil.which("systemctl"):
        unit = linux_systemd_unit()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(f"""[Unit]
Description=skill-sync: watch for new skills

[Service]
ExecStart={py} {WATCH_SCRIPT}
Restart=on-failure

[Install]
WantedBy=default.target
""", encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{NAME}.service"], check=False)
        print(f"installed: {unit} (systemd --user)")
        return
    desktop = linux_autostart_desktop()
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text(f"""[Desktop Entry]
Type=Application
Name=skill-sync watch
Exec={py} {WATCH_SCRIPT}
X-GNOME-Autostart-enabled=true
NoDisplay=true
""", encoding="utf-8")
    print(f"installed: {desktop} (XDG autostart - no systemd --user found)")


def uninstall_linux():
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{NAME}.service"],
                        check=False, capture_output=True)
    unit = linux_systemd_unit()
    if unit.exists():
        unit.unlink()
        print(f"removed: {unit}")
    desktop = linux_autostart_desktop()
    if desktop.exists():
        desktop.unlink()
        print(f"removed: {desktop}")
    if not unit.exists() and not desktop.exists():
        print("nothing to remove (not installed)")


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--start-only", action="store_true",
                     help="just launch the watcher now, skip autostart registration")
    args = ap.parse_args()

    if args.start_only:
        return 0 if start_now() else 1

    installer, uninstaller = {
        "win32": (install_windows, uninstall_windows),
        "darwin": (install_macos, uninstall_macos),
    }.get(sys.platform, (install_linux, uninstall_linux))

    if args.uninstall:
        uninstaller()
        return 0

    installer()
    if start_now():
        print("watcher running now (no need to log out/in).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
