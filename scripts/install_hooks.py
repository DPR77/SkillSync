#!/usr/bin/env python3
"""Install (or remove) the skill-sync hooks in Claude Code's user settings.

Adds two hooks to ~/.claude/settings.json:

  Stop          -> sync.py hook-stop           auto-upload changed skills when a session ends
  SessionStart  -> sync.py hook-session-start  one-line notice when the remote has newer skills

Both are no-ops until `sync.py setup` has been run, and both always exit 0 so a
network or rclone problem can never break a Claude Code session.

Usage:
    python install_hooks.py            # install / update
    python install_hooks.py --uninstall
    python install_hooks.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYNC_SCRIPT = HERE / "sync.py"
SETTINGS = Path.home() / ".claude" / "settings.json"

MARKER = "skill-sync"          # identifies our hooks for update/uninstall
STOP_TIMEOUT = 120
SESSION_TIMEOUT = 20


def python_exe() -> str:
    """Interpreter to run the hook with, preferring a stable name over a venv path."""
    exe = Path(sys.executable)
    if "venv" in exe.parts or ".venv" in exe.parts:
        for candidate in ("python3", "python"):
            found = shutil.which(candidate)
            if found:
                return found
    return str(exe)


def quote(s: str) -> str:
    return f'"{s}"' if " " in s else s


def hook_command(subcommand: str) -> str:
    return f"{quote(python_exe())} {quote(str(SYNC_SCRIPT))} {subcommand} --quiet"


def is_ours(entry: dict) -> bool:
    return MARKER in json.dumps(entry)


def install(settings: dict) -> dict:
    hooks = settings.setdefault("hooks", {})

    stop = [g for g in hooks.get("Stop", []) if not is_ours(g)]
    stop.append({"hooks": [{"type": "command",
                            "command": hook_command("hook-stop"),
                            "timeout": STOP_TIMEOUT}]})
    hooks["Stop"] = stop

    start = [g for g in hooks.get("SessionStart", []) if not is_ours(g)]
    start.append({"matcher": "startup|resume",
                  "hooks": [{"type": "command",
                             "command": hook_command("hook-session-start"),
                             "timeout": SESSION_TIMEOUT}]})
    hooks["SessionStart"] = start
    return settings


def uninstall(settings: dict) -> dict:
    hooks = settings.get("hooks", {})
    for event in ("Stop", "SessionStart"):
        remaining = [g for g in hooks.get(event, []) if not is_ours(g)]
        if remaining:
            hooks[event] = remaining
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    return settings


def main_with(uninstall: bool = False, dry_run: bool = False, settings: str | None = None) -> int:
    """Programmatic entry point, used by menu.py."""
    return run(argparse.Namespace(uninstall=uninstall, dry_run=dry_run, settings=settings))


def run(args) -> int:
    path = Path(args.settings).expanduser() if args.settings else SETTINGS
    if not SYNC_SCRIPT.exists():
        print(f"error: {SYNC_SCRIPT} not found", file=sys.stderr)
        return 1

    if path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"error: {path} is not valid JSON ({e}). Fix it first.", file=sys.stderr)
            return 1
    else:
        settings = {}

    updated = uninstall(dict(settings)) if args.uninstall else install(dict(settings))
    rendered = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"

    if args.dry_run:
        print(rendered)
        return 0

    if path.exists():
        backup = path.with_name(f"{path.name}.skill-sync-backup-"
                                f"{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"backup: {backup}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")

    if args.uninstall:
        print(f"skill-sync hooks removed from {path}")
    else:
        print(f"skill-sync hooks installed in {path}")
        print(f"  Stop         -> {hook_command('hook-stop')}")
        print(f"  SessionStart -> {hook_command('hook-session-start')}")
        print("Restart Claude Code (or run /hooks) for them to take effect.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Install skill-sync hooks into Claude Code settings")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--settings", help="path to settings.json (default: ~/.claude/settings.json)")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
