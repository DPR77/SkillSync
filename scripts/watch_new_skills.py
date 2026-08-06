#!/usr/bin/env python3
"""Background watcher: notice a new skill within seconds, not on the next 15-min poll
or the next Claude Code Stop event.

Polls every WATCH_INTERVAL seconds (cheap: it's just a directory listing across a
handful of folders) and, the moment a skill folder appears that quick_sig() hasn't
settled on yet, waits for its file count/mtime to stop changing (installers write
several files) and then runs the exact same check hook-stop already does - so
"already-tracked skills auto-push, brand-new ones open confirm-new" behaves
identically whether it's a Claude Code Stop event or this watcher that triggered it.

Runs forever. Meant to be started at login (see install_watch.py for the
per-OS autostart registration); safe to also just run directly in a terminal.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync  # noqa: E402

WATCH_INTERVAL = 5      # seconds between scans - a directory listing, not a file read
SETTLE_CHECKS = 2       # consecutive stable scans before treating a skill as "done writing"


def scan_signature(cfg) -> dict[str, tuple[float, int]]:
    lmap = {n: p for n, p in sync.local_skills_map(cfg).items() if not sync.is_self(n)}
    return {n: sync.quick_sig(p) for n, p in lmap.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=float, default=WATCH_INTERVAL)
    ap.add_argument("--once", action="store_true", help="one scan cycle, for testing")
    args = ap.parse_args()

    cfg = sync.load_config()
    if not cfg:
        print("skill-sync is not configured yet (sync.py setup) - watcher has nothing to do.",
              file=sys.stderr)
        return 1

    stable_since: dict[str, tuple[float, int]] = {}
    seen_stable_count: dict[str, int] = {}

    while True:
        try:
            cfg = sync.load_config() or cfg
            current = scan_signature(cfg)

            for name, sig in current.items():
                if stable_since.get(name) == sig:
                    seen_stable_count[name] = seen_stable_count.get(name, 0) + 1
                else:
                    stable_since[name] = sig
                    seen_stable_count[name] = 1

            for name in list(stable_since):
                if name not in current:
                    stable_since.pop(name, None)
                    seen_stable_count.pop(name, None)

            settled = {n for n, c in seen_stable_count.items() if c >= SETTLE_CHECKS}
            if settled:
                try:
                    ns = argparse.Namespace(quiet=True)
                    sync.cmd_hook_stop(ns)
                except Exception as e:
                    sync.log(f"watch_new_skills hook-stop call failed: {e!r}")
        except Exception as e:
            sync.log(f"watch_new_skills scan failed: {e!r}")

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
