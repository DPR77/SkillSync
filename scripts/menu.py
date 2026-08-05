#!/usr/bin/env python3
"""Interactive ASCII menu for skill-sync.

Full-screen terminal UI with checkbox selection, arrow-key navigation, live filter
and colour-coded sync states. Pure standard library, works on Windows, macOS and Linux.

    python menu.py                # run the menu
    python menu.py --ascii        # plain ASCII frame characters
    python menu.py --no-color     # no ANSI colours
    python menu.py --self-check   # render every screen once and exit (no input, for tests)

This needs a real terminal. Claude Code's tool calls have no interactive stdin, so the
user must launch it themselves - inside Claude Code that means prefixing with `!`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync  # noqa: E402  (local module, path set above)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ------------------------------------------------------------------ terminal

class Term:
    """Raw-mode key reader + screen control."""

    def __init__(self):
        self.windows = os.name == "nt"
        self._fd = None
        self._old = None
        if self.windows:
            self._enable_vt()

    @staticmethod
    def _enable_vt():
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            pass

    def __enter__(self):
        if not self.windows:
            import termios
            import tty
            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l")   # alt screen, hide cursor
        sys.stdout.flush()
        return self

    def __exit__(self, *exc):
        self.cooked()
        sys.stdout.write("\x1b[?25h\x1b[?1049l")   # show cursor, leave alt screen
        sys.stdout.flush()
        return False

    def cooked(self):
        if not self.windows and self._old is not None:
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def raw(self):
        if not self.windows and self._old is not None:
            import tty
            tty.setcbreak(self._fd)

    def read_key(self) -> str:
        """Normalised key name: up/down/left/right/enter/esc/space/backspace/home/end or a char."""
        if self.windows:
            import msvcrt
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                nxt = msvcrt.getwch()
                return {"H": "up", "P": "down", "K": "left", "M": "right",
                        "G": "home", "O": "end", "I": "pgup", "Q": "pgdn",
                        "S": "delete"}.get(nxt, "")
            return self._norm(ch)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            import select
            if not select.select([sys.stdin], [], [], 0.05)[0]:
                return "esc"
            seq = sys.stdin.read(1)
            if seq != "[":
                return "esc"
            seq = sys.stdin.read(1)
            while seq.isdigit():
                seq += sys.stdin.read(1)
            return {"A": "up", "B": "down", "C": "right", "D": "left",
                    "H": "home", "F": "end", "5~": "pgup", "6~": "pgdn",
                    "3~": "delete"}.get(seq, "")
        return self._norm(ch)

    @staticmethod
    def _norm(ch: str) -> str:
        if ch in ("\r", "\n"):
            return "enter"
        if ch == " ":
            return "space"
        if ch in ("\x7f", "\b"):
            return "backspace"
        if ch == "\x1b":
            return "esc"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch

    def ask(self, prompt: str) -> str:
        """Line input; drops out of raw mode so editing keys behave."""
        self.cooked()
        sys.stdout.write("\x1b[?25h")
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        finally:
            sys.stdout.write("\x1b[?25l")
            self.raw()

    @staticmethod
    def pause(msg="press any key to continue"):
        sys.stdout.write(f"\n{C.dim(msg)}")
        sys.stdout.flush()

    @staticmethod
    def draw(lines):
        w, h = term_size()
        out = ["\x1b[H\x1b[2J"]
        for line in lines[:h - 1]:
            out.append(clip(line, w) + "\x1b[K")
        sys.stdout.write("\n".join(out))
        sys.stdout.flush()


def term_size():
    s = shutil.get_terminal_size((100, 32))
    return max(60, s.columns), max(20, s.lines)


# -------------------------------------------------------------- style layer

class Style:
    enabled = True

    def _w(self, code, s):
        return f"\x1b[{code}m{s}\x1b[0m" if self.enabled else str(s)

    def dim(self, s):
        return self._w("2", s)

    def bold(self, s):
        return self._w("1", s)

    def cyan(self, s):
        return self._w("36", s)

    def green(self, s):
        return self._w("32", s)

    def yellow(self, s):
        return self._w("33", s)

    def blue(self, s):
        return self._w("94", s)

    def red(self, s):
        return self._w("31", s)

    def magenta(self, s):
        return self._w("35", s)

    def inv(self, s):
        return self._w("7", s)


C = Style()

ANSI_RE = None


import unicodedata

def visible_len(s: str) -> int:
    global ANSI_RE
    if ANSI_RE is None:
        import re
        ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    clean = ANSI_RE.sub("", s)
    chars = list(clean)
    width = 0
    i = 0
    while i < len(chars):
        ch = chars[i]
        # Skip plain variation selectors (already handled via look-ahead below)
        if ch in ("\ufe0f", "\ufe0e"):
            i += 1
            continue
        # Skip combining / invisible marks
        cat = unicodedata.category(ch)
        if cat in ("Mn", "Me", "Cf"):
            i += 1
            continue
        # Look ahead: if next char is U+FE0F, this base char renders as 2-wide
        next_ch = chars[i + 1] if i + 1 < len(chars) else ""
        if next_ch == "\ufe0f":
            width += 2
            i += 2          # consume base char + variation selector
            continue
        east = unicodedata.east_asian_width(ch)
        if east in ("W", "F") or ord(ch) >= 0x1F000:
            width += 2
        else:
            width += 1
        i += 1
    return width


def clip(s: str, width: int) -> str:
    if visible_len(s) <= width:
        return s
    global ANSI_RE
    out, count = [], 0
    i = 0
    while i < len(s) and count < width:
        if s[i] == "\x1b":
            j = s.find("m", i)
            if j == -1:
                break
            out.append(s[i:j + 1])
            i = j + 1
            continue
        ch = s[i]
        w = visible_len(ch)
        if count + w > width:
            break
        out.append(ch)
        count += w
        i += 1
    return "".join(out) + "\x1b[0m"


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - visible_len(s))


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


class Glyphs:
    unicode = True

    @property
    def g(self):
        if self.unicode:
            return dict(tl="╭", tr="╮", bl="╰", br="╯", h="─", v="│",
                        check="[x]", empty="[ ]", cursor="›", bullet="•", arrow="→",
                        bar_full="█", bar_empty="░")
        return dict(tl="+", tr="+", bl="+", br="+", h="-", v="|",
                    check="[x]", empty="[ ]", cursor=">", bullet="*", arrow="->",
                    bar_full="#", bar_empty=".")


G = Glyphs()

BANNER_UNICODE = [
    "╔═╗╦╔═╦╦  ╦    ╔═╗╦ ╦╔╗╔╔═╗",
    "╚═╗╠╩╗║║  ║    ╚═╗╚╦╝║║║║  ",
    "╚═╝╩ ╩╩╩═╝╩═╝  ╚═╝ ╩ ╝╚╝╚═╝",
]
BANNER_ASCII = [
    " ___ _  _ _ _  _    ___ _   _ _  _  ___ ",
    "/ __| |/ | | || |  / __| \\ | | \\| |/ __|",
    "\\___|_|\\_|_|_||_|  \\___|_|\\_|_|\\_|\\___|",
]

STATE_STYLE = {
    sync.IN_SYNC: ("🟢 in-sync", C.green),
    sync.LOCAL_NEW: ("⚡ local-newer", C.yellow),
    sync.REMOTE_NEW: ("☁️ remote-newer", C.blue),
    sync.ONLY_LOCAL: ("📦 not-uploaded", C.magenta),
    sync.ONLY_REMOTE: ("📥 remote-only", C.cyan),
    sync.CONFLICT: ("🚨 CONFLICT", C.red),
}


def state_badge(state: str) -> str:
    label, colour = STATE_STYLE.get(state, (state, C.dim))
    return colour(label)


# ------------------------------------------------------------------- chrome

def box(title: str, width: int):
    g = G.g
    inner = width - 2
    top = g["tl"] + g["h"] * inner + g["tr"]
    bot = g["bl"] + g["h"] * inner + g["br"]
    return top, bot


def header(cfg, summary=None, width=None):
    width = width or term_size()[0]
    width = min(width, 100)
    g = G.g
    top, bot = box("", width)
    lines = [C.cyan(top)]
    banner = BANNER_UNICODE if G.unicode else BANNER_ASCII
    for i, row in enumerate(banner):
        right = ""
        if i == 1:
            right = C.dim("skills that follow you around  •  Created by GTI Santander")
        body = "  " + C.cyan(C.bold(row)) + ("   " + right if right else "")
        lines.append(C.cyan(g["v"]) + pad(body, width - 2) + C.cyan(g["v"]))
    lines.append(C.cyan(bot))

    if cfg:
        remote = sync.base_path(cfg)
        lines.append(f"  {C.dim('remote')}  {C.bold(remote)}    "
                     f"{C.dim('machine')}  {C.bold(cfg.get('machine'))}")
        lines.append(f"  {C.dim('groups')}  "
                     f"{', '.join(cfg.get('categories') or []) or C.dim('none yet')}")
    else:
        lines.append("  " + C.yellow("not configured yet - open  Setup  below"))
    if summary:
        lines.append("  " + summary)
    lines.append("")
    return lines


def summary_line(status: dict) -> str:
    counts = {}
    for i in status.values():
        counts[i["state"]] = counts.get(i["state"], 0) + 1
    parts = []
    for state in (sync.CONFLICT, sync.ONLY_LOCAL, sync.LOCAL_NEW, sync.REMOTE_NEW,
                  sync.ONLY_REMOTE, sync.IN_SYNC):
        n = counts.get(state, 0)
        if n:
            label, colour = STATE_STYLE[state]
            parts.append(colour(f"{n} {label}"))
    return f"  {G.g['bullet']}  ".join(parts) if parts else C.dim("no skills found")


def footer(keys):
    g = G.g
    parts = [f"{C.bold(k)} {C.dim(v)}" for k, v in keys]
    return ["", C.dim(g["h"] * min(term_size()[0], 100)), "  " + "   ".join(parts)]


def bar(done: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return ""
    filled = round(width * done / total)
    g = G.g
    return C.green(g["bar_full"] * filled) + C.dim(g["bar_empty"] * (width - filled))


# ------------------------------------------------------------------- pickers

class Picker:
    """Checkbox list with cursor, filter and bulk keys."""

    def __init__(self, items, render_row, title, checked=None, single=False, hint=""):
        self.items = items
        self.render_row = render_row
        self.title = title
        self.single = single
        self.hint = hint
        self.checked = set(checked or ())
        self.cursor = 0
        self.filter = ""
        self.filtering = False

    def visible(self):
        if not self.filter:
            return list(range(len(self.items)))
        f = self.filter.lower()
        return [i for i, it in enumerate(self.items) if f in str(it.get("key", "")).lower()]

    def frame(self, cfg, summary=None):
        lines = header(cfg, summary)
        lines.append("  " + C.bold(self.title))
        if self.hint:
            lines.append("  " + C.dim(self.hint))
        lines.append("")
        vis = self.visible()
        if not vis:
            lines.append("    " + C.dim("nothing matches the filter"))
        g = G.g
        _w, h = term_size()
        room = max(5, h - len(lines) - 8)
        start = max(0, min(self.cursor - room // 2, max(0, len(vis) - room)))
        for pos, idx in enumerate(vis[start:start + room]):
            item = self.items[idx]
            on_cursor = idx == self.cursor
            mark = g["check"] if idx in self.checked else g["empty"]
            if self.single:
                mark = g["cursor"] if on_cursor else " "
            row = self.render_row(item, idx in self.checked)
            # In single mode the mark IS the cursor, so prefix would double it
            prefix = C.cyan(g["cursor"]) if (on_cursor and not self.single) else " "
            text = f" {prefix} {mark} {row}"
            lines.append(C.inv(pad(text, min(term_size()[0], 100))) if on_cursor else text)
        if len(vis) > room:
            lines.append(C.dim(f"    ({len(vis)} items, showing {room})"))
        if self.filter or self.filtering:
            lines.append("")
            lines.append("  " + C.yellow(f"filter: {self.filter}_"))
        keys = [("up/down", "move"), ("enter", "confirm"), ("esc", "back")]
        if not self.single:
            keys[1:1] = [("space", "toggle"), ("a", "all"), ("n", "none"), ("i", "invert")]
        keys.append(("/", "filter"))
        return lines + footer(keys)

    def run(self, term, cfg, summary=None):
        """Returns list of chosen items, or None when cancelled."""
        while True:
            term.draw(self.frame(cfg, summary))
            try:
                key = term.read_key()
            except KeyboardInterrupt:
                return None
            vis = self.visible()
            if self.filtering:
                if key == "enter":
                    self.filtering = False
                elif key == "esc":
                    self.filtering, self.filter = False, ""
                elif key == "backspace":
                    self.filter = self.filter[:-1]
                elif len(key) == 1 and key.isprintable():
                    self.filter += key
                if vis := self.visible():
                    if self.cursor not in vis:
                        self.cursor = vis[0]
                continue

            if key in ("up", "down", "j", "k") and vis:
                here = vis.index(self.cursor) if self.cursor in vis else 0
                step = 1 if key in ("down", "j") else -1
                here = (here + step) % len(vis)
                self.cursor = vis[here]
            elif key == "home" and vis:
                self.cursor = vis[0]
            elif key == "end" and vis:
                self.cursor = vis[-1]
            elif key == "space" and not self.single:
                self.checked ^= {self.cursor}
            elif key == "a" and not self.single:
                self.checked |= set(vis)
            elif key == "n" and not self.single:
                self.checked -= set(vis)
            elif key == "i" and not self.single:
                self.checked ^= set(vis)
            elif key == "/":
                self.filtering, self.filter = True, ""
            elif key == "enter":
                if self.single:
                    return [self.items[self.cursor]] if self.items else []
                return [self.items[i] for i in sorted(self.checked)]
            elif key in ("esc", "q"):
                return None


# ------------------------------------------------------------------ actions

def run_action(term, label, fn):
    """Leave the UI, run a sync.py command, show its output, wait for a key."""
    term.cooked()
    sys.stdout.write("\x1b[H\x1b[2J")
    width = min(term_size()[0], 100)
    hr = G.g["h"] * width
    print(C.cyan(C.bold(f"  {label}")))
    print(C.dim(hr) + "\n")
    ok = True
    try:
        fn()
    except sync.SyncError as e:
        print(C.red(f"\nerror: {e}"))
        ok = False
    except KeyboardInterrupt:
        print(C.yellow("\ninterrupted"))
        ok = False
    except Exception as e:  # never crash the menu
        print(C.red(f"\nunexpected error: {e!r}"))
        ok = False
    print("\n" + C.dim(hr))
    if ok:
        print(C.green("  ✓  done"))
    # Single prompt — no term.pause() to avoid double message
    sys.stdout.write(C.dim("\n  press any key to continue ..."))
    sys.stdout.flush()
    term.raw()
    try:
        term.read_key()
    except KeyboardInterrupt:
        pass



def load_status(term, cfg, force=False, cache={}):
    if not force and cache.get("data") is not None and time.time() - cache.get("at", 0) < 300:
        return cache["data"]
    term.draw(header(cfg) + ["  " + C.dim("reading remote manifest ...")])
    try:
        data = sync.compute_status(cfg)
    except sync.SyncError as e:
        term.draw(header(cfg) + ["  " + C.red(f"error: {e}"), "",
                                 C.dim("  press any key")])
        term.read_key()
        data = {}
    cache["data"], cache["at"] = data, time.time()
    return data


def origin_badge(local_path: str | None) -> str:
    if not local_path:
        return C.dim("          ")
    p = str(local_path).lower()
    if ".claude" in p:
        return C.blue("🔵 Claude")
    elif ".gemini" in p:
        return C.cyan("🟣 Gemini")
    elif ".agents" in p:
        return C.yellow("🟠 Agents")
    return C.dim("⚪ Local ")


def skill_row(item, checked):
    i = item["info"]
    name = C.bold(pad(item["key"], 22))
    origin = origin_badge(i.get("local_path"))
    cat_raw = i.get("category") or ""
    cat = C.dim(pad(cat_raw or "no group", 14))
    size = C.dim(pad(sync.human_size(i.get("size")), 7)) if i.get("size") else C.dim(pad("", 7))
    badge = pad(state_badge(i["state"]), 20)
    return f"{name} {origin} {cat} {badge} {size}"


def to_items(status, names):
    return [{"key": n, "info": status[n]} for n in names]


def screen_push(term, cfg, status):
    actionable = [n for n, i in status.items() if i["state"] in (sync.ONLY_LOCAL, sync.LOCAL_NEW)]
    local = [n for n, i in status.items() if i["local"]]
    if not local:
        return
    items = to_items(status, sorted(local))
    checked = [k for k, it in enumerate(items) if it["key"] in actionable]
    picker = Picker(items, skill_row, "Upload skills",
                    checked=checked, hint="pre-selected: everything not yet on the remote")
    chosen = picker.run(term, cfg, summary_line(status))
    if not chosen:
        return
    names = [c["key"] for c in chosen]
    missing = [c["key"] for c in chosen if not c["info"].get("category")]
    assume = False
    if missing:
        # Show group picker inline instead of sending user back to Groups menu
        cats = list(cfg.get("categories") or [])
        cat_items = [{"key": c} for c in cats] + [{"key": "+ new group ..."}, {"key": f"⚡ Use default ('{cfg.get('default_category', 'work')}')"}]
        term.draw(header(cfg) + [
            "  " + C.yellow(f"{len(missing)} skill(s) need a group: "
                            f"{C.dim(', '.join(missing))}"),
            "",
        ])
        cat_picker = Picker(cat_items, lambda it, ch: it["key"],
                            "Assign these skills to which group?", single=True)
        picked_group = cat_picker.run(term, cfg)
        if not picked_group:
            return
        target = picked_group[0]["key"]
        if target.startswith("+"):
            target = term.ask("  new group name: ")
            if not target:
                return
        if target.startswith("⚡"):
            assume = True
        else:
            def do_assign():
                for n in missing:
                    sync.cmd_categorize(Namespace(skill=n, category=target, force=False))
            run_action(term, f"assign {len(missing)} skill(s) → {target}", do_assign)
    run_action(term, f"push {' '.join(names[:3])}{'...' if len(names) > 3 else ''}",
               lambda: sync.cmd_push(Namespace(skills=names, dry_run=False, force=False,
                                               no_scan=True, assume_default=assume)))



def screen_pull(term, cfg, status):
    remote_cats = {}
    for n, i in status.items():
        if i["remote"]:
            remote_cats.setdefault(i.get("category") or sync.NO_CATEGORY, []).append(n)
    if not remote_cats:
        term.draw(header(cfg) + ["  " + C.dim("the remote has no skills yet"), "",
                                 C.dim("  press any key")])
        term.read_key()
        return
    items = [{"key": c, "names": sorted(v)} for c, v in sorted(remote_cats.items())]

    def row(item, checked):
        new = [n for n in item["names"] if not status[n]["local"]]
        tail = C.green(f"{len(new)} new here") if new else C.dim("all present")
        return f"{pad(item['key'], 18)} {pad(plural(len(item['names']), 'skill'), 12)} {tail}"

    pre = [k for k, it in enumerate(items) if it["key"] in (cfg.get("categories") or [])]
    picker = Picker(items, row, "Download groups", checked=pre,
                    hint="a group you pull is remembered as subscribed on this machine")
    chosen = picker.run(term, cfg, summary_line(status))
    if not chosen:
        return
    cats = [c["key"] for c in chosen]
    run_action(term, f"pull {' '.join(cats)}",
               lambda: sync.cmd_pull(Namespace(categories=cats, skills=None, dest=None,
                                               dry_run=False, force=False)))


def _group_skill_row(item, checked):
    """Skill row used inside a group detail view (no origin badge needed)."""
    i = item["info"]
    name = pad(item["key"], 24)
    state_lbl = pad(state_badge(i["state"]), 22)
    size = C.dim(pad(sync.human_size(i.get("size")), 8)) if i.get("size") else C.dim(pad("", 8))
    return f"{name} {state_lbl} {size}"


def screen_group_detail(term, cfg, status, group_name):
    """Drill-down: show skills in a specific group and let the user reassign them."""
    while True:
        skills_in_group = sorted(n for n, i in status.items()
                                 if i.get("category") == group_name and i.get("local"))
        all_local = sorted(n for n, i in status.items() if i.get("local"))

        lines = header(cfg)
        lines += [
            f"  {C.bold('Group:')}  {C.cyan(group_name)}  "
            f"{C.dim(f'({len(skills_in_group)} skills)')}",
            "",
        ]
        if skills_in_group:
            for name in skills_in_group:
                info = status[name]
                badge = state_badge(info["state"])
                origin = origin_badge(info.get("local_path"))
                lines.append(f"    {pad(name, 22)} {origin}  {badge}")
        else:
            lines.append(C.dim("    (no skills in this group yet)"))
        lines += [
            "",
            C.dim("─" * min(term_size()[0], 100)),
            f"  {C.bold('a')} {C.dim('assign skills')}   "
            f"{C.bold('n')} {C.dim('rename group')}   "
            f"{C.bold('esc')} {C.dim('back')}",
        ]
        term.draw(lines)
        key = term.read_key()
        if key in ("esc", "q"):
            return
        if key == "n":
            new_name = term.ask(f"  rename '{group_name}' to: ")
            if new_name and new_name != group_name:
                # Reassign all skills in this group to the new name
                def do_rename():
                    for n, i in status.items():
                        if i.get("category") == group_name:
                            sync.cmd_categorize(Namespace(skill=n, category=new_name, force=True))
                    # Update config categories list
                    cats_cfg = list(cfg.get("categories") or [])
                    if group_name in cats_cfg:
                        cats_cfg[cats_cfg.index(group_name)] = new_name
                    sync.cmd_setup(Namespace(
                        remote=cfg.get("remote"), root=cfg.get("root"),
                        categories=",".join(cats_cfg),
                        default_category=cfg.get("default_category"),
                        machine=cfg.get("machine")))
                run_action(term, f"rename group '{group_name}' → '{new_name}'", do_rename)
                status = load_status(term, cfg, force=True)
                group_name = new_name   # follow the rename
            continue
        if key != "a":
            continue

        # Let the user pick which local skills to assign to this group
        items = to_items(status, all_local)
        pre_checked = [k for k, it in enumerate(items)
                       if it["info"].get("category") == group_name]
        picker = Picker(items, skill_row, f"Assign to '{group_name}'",
                        checked=pre_checked,
                        hint="tick skills → enter to confirm")
        chosen = picker.run(term, cfg, summary_line(status))
        if not chosen:
            continue

        def do():
            for c in chosen:
                sync.cmd_categorize(Namespace(skill=c["key"], category=group_name, force=True))

        run_action(term, f"assign → {group_name}", do)
        status = load_status(term, cfg, force=True)


def screen_groups(term, cfg, status):
    """Top-level Groups screen: shows a list of groups; clicking one drills in."""
    cats = list(cfg.get("categories") or [])
    ungrouped = sorted(n for n, i in status.items()
                       if not i.get("category") and i.get("local"))

    while True:
        # Build group list with counts
        group_counts = {c: sum(1 for i in status.values()
                               if i.get("category") == c and i.get("local"))
                        for c in cats}

        lines = header(cfg)
        lines += [C.bold("  Groups"), ""]
        for cat in cats:
            n = group_counts.get(cat, 0)
            pill = C.cyan(f"  {cat}")
            count = C.dim(f"  {n} skill{'s' if n != 1 else ''}")
            lines.append(f"    {pill}{count}")
        if ungrouped:
            lines.append("")
            lines.append(f"  {C.yellow(f'⚠  {len(ungrouped)} ungrouped skill(s):')} "
                         f"{C.dim(', '.join(ungrouped))}")
        lines += [
            "",
            C.dim("─" * min(term_size()[0], 100)),
        ]

        # Build a simple menu of groups + actions
        options = cats + (["⚠ Assign ungrouped"] if ungrouped else []) + ["+ New group", "← Back"]
        for idx, opt in enumerate(options):
            lines.append(f"  {C.bold(str(idx + 1))}  {opt}")
        lines += ["", C.dim("  press number key or esc to go back")]
        term.draw(lines)

        key = term.read_key()
        if key in ("esc", "q"):
            return

        # Number key navigation
        if key.isdigit():
            idx = int(key) - 1
            if 0 <= idx < len(options):
                choice = options[idx]
                if choice == "← Back":
                    return
                elif choice == "+ New group":
                    new_cat = term.ask("  new group name: ")
                    if new_cat and new_cat not in cats:
                        cats.append(new_cat)
                        run_action(term, "add group",
                                   lambda nc=new_cat: sync.cmd_setup(Namespace(
                                       remote=cfg.get("remote"), root=cfg.get("root"),
                                       categories=",".join(cats),
                                       default_category=cfg.get("default_category"),
                                       machine=cfg.get("machine"))))
                        cfg = sync.load_config() or cfg
                        cats = list(cfg.get("categories") or [])
                elif choice.startswith("⚠"):
                    # Assign ungrouped skills
                    items = to_items(status, ungrouped)
                    cat_items = [{"key": c} for c in cats] + [{"key": "+ new group ..."}]
                    cat_picker = Picker(cat_items, lambda it, ch: it["key"],
                                        "Assign ungrouped skills to group", single=True)
                    picked_group = cat_picker.run(term, cfg)
                    if picked_group:
                        target = picked_group[0]["key"]
                        if target.startswith("+"):
                            target = term.ask("  new group name: ")
                        if target:
                            def do():
                                for n in ungrouped:
                                    sync.cmd_categorize(Namespace(skill=n, category=target, force=False))
                            run_action(term, f"assign → {target}", do)
                            status = load_status(term, cfg, force=True)
                else:
                    # Drill into a group
                    screen_group_detail(term, cfg, status, choice)
                    status = load_status(term, cfg, force=True)


def screen_conflicts(term, cfg, status):
    conflicts = sorted(n for n, i in status.items() if i["state"] == sync.CONFLICT)
    if not conflicts:
        term.draw(header(cfg) + ["  " + C.green("no conflicts"), "", C.dim("  press any key")])
        term.read_key()
        return
    items = to_items(status, conflicts)

    def row(item, checked):
        i = item["info"]
        return (f"{pad(item['key'], 20)} {origin_badge(i.get('local_path'))} {C.dim('remote from')} "
                f"{pad(str(i.get('remote_machine')), 12)} {C.dim(str(i.get('remote_updated')))}")

    picker = Picker(items, row, "Conflicts - pick one to inspect / resolve", single=True,
                    hint="both sides changed since the last sync; the loser is kept as a backup")
    chosen = picker.run(term, cfg, summary_line(status))
    if not chosen:
        return
    name = chosen[0]["key"]
    side_items = [
        {"key": "view diff", "desc": "inspect SKILL.md diff between local and remote"},
        {"key": "local", "desc": "keep THIS computer's version, overwrite the remote"},
        {"key": "remote", "desc": "keep the remote version, overwrite this computer"}
    ]
    side = Picker(side_items, lambda it, ch: f"{pad(it['key'], 12)} {C.dim(it['desc'])}",
                  f"Resolve {name}", single=True).run(term, cfg)
    if not side:
        return
    action = side[0]["key"]
    if action == "view diff":
        run_action(term, f"merge diff {name}",
                   lambda: sync.cmd_merge(Namespace(skill=name, keep=None, json=False)))
        screen_conflicts(term, cfg, status)
        return
    keep = action
    run_action(term, f"resolve {name} --keep {keep}",
               lambda: sync.cmd_resolve(Namespace(skill=name, keep=keep)))


def screen_prune(term, cfg, status):
    orphans = sorted(n for n, i in status.items() if i["remote"] and not i["local"])
    if not orphans:
        term.draw(header(cfg) + ["  " + C.green("nothing to prune"), "", C.dim("  press any key")])
        term.read_key()
        return
    items = to_items(status, orphans)
    picker = Picker(items, skill_row, C.red("Delete from the remote"),
                    hint="DANGER: these skills are not on this computer. Deleting removes them "
                         "for every machine.")
    chosen = picker.run(term, cfg, summary_line(status))
    if not chosen:
        return
        names = [c["key"] for c in chosen]
    term.draw(header(cfg) + [
        "  " + C.red(C.bold("This permanently deletes from the remote:")), "",
    ] + [f"    {G.g['bullet']} {n}" for n in names] + [
        "", "  " + C.dim("If one of these only lives on another computer, it is lost there too."),
        "", "  " + C.yellow("type  DELETE  to confirm, anything else cancels"),
    ])
    if term.ask("  > ") != "DELETE":
        return
    run_action(term, "prune", lambda: sync.cmd_prune(Namespace(yes=True, only=names)))


def screen_setup(term, cfg):
    import subprocess as _sp
    exe = sync.rclone_bin(required=False)
    g = G.g
    width = min(term_size()[0], 100)
    hr = "─" * width

    # (icon, label, rclone-type, name-hint, default-name, create-args or None for local)
    PROVIDERS = [
        ("☁️",  "Google Drive",       "drive",    "gdrive",   "gdrive",
         ["config", "create", "gdrive", "drive", "scope", "drive"]),
        ("📦",  "Dropbox",            "dropbox",  "dropbox",  "dropbox",
         ["config", "create", "dropbox", "dropbox"]),
        ("🪟",  "OneDrive",           "onedrive", "onedrive", "onedrive",
         ["config", "create", "onedrive", "onedrive"]),
        ("🗄️", "S3 / Backblaze B2",  "s3",       "s3",       "mys3",
         ["config", "create", "mys3", "s3"]),
        ("📡",  "SFTP / WebDAV",      "sftp",     "sftp",     "mysftp",
         ["config", "create", "mysftp", "sftp"]),
        ("💾",  "Local folder / NAS", "local",    "local",    None,
         None),   # local handled specially — no rclone remote needed
    ]

    if not exe:
        term.draw(header(cfg) + [
            "  " + C.red("rclone is not installed."), "",
            "    Windows  " + C.cyan("winget install Rclone.Rclone"),
            "    macOS    " + C.cyan("brew install rclone"),
            "    Linux    " + C.cyan("curl https://rclone.org/install.sh | sudo bash"),
            "", "  " + C.dim("install it, then reopen this menu"), "",
            C.dim("  press any key")])
        term.read_key()
        return cfg

    def refresh_configured():
        _c, out, _e = sync.rclone(["listremotes"], check=False, timeout=60)
        cfg_set = {r.rstrip(":").lower() for r in out.splitlines() if r.strip()}
        raw = [r.strip() for r in out.splitlines() if r.strip()]
        return cfg_set, raw

    configured, remotes_raw = refresh_configured()
    active_remote = (cfg or {}).get("remote", "")

    def activate(remote):
        """Point skill-sync's config at `remote` and auto-push existing local skills."""
        nonlocal cfg, active_remote
        term.draw(header(cfg) + ["  " + C.bold(f"Remote: {remote}"), ""])
        root = term.ask(f"  folder inside remote [{(cfg or {}).get('root', 'ClaudeSkills')}]: ") \
            or (cfg or {}).get("root", "ClaudeSkills")
        groups = term.ask(f"  groups [{','.join((cfg or {}).get('categories') or ['work','school','personal'])}]: ") \
            or ",".join((cfg or {}).get("categories") or ["work", "school", "personal"])
        run_action(term, "setup",
                   lambda r=remote, ro=root, gr=groups: sync.cmd_setup(
                       Namespace(remote=r, root=ro, categories=gr,
                                 default_category=None, machine=None)))
        cfg = sync.load_config() or cfg
        active_remote = (cfg or {}).get("remote", "")
        run_action(term, "initial push — uploading all local skills",
                   lambda: sync.cmd_push(Namespace(skills=None, all=True,
                                                   no_scan=False, dry_run=False,
                                                   force=False, assume_default=True)))

    while True:
        items = []
        for icon, name, ptype, hint, default_name, create_args in PROVIDERS:
            is_local = (create_args is None)
            if is_local:
                # Local is "configured" if the current remote is a local path
                is_active = active_remote and not active_remote.endswith(":")
                found = [active_remote] if is_active else []
            else:
                found = [r for r in configured
                         if ptype in r.lower() or (default_name and default_name in r.lower())
                         or hint in r.lower()]

            active_match = any(
                r.rstrip(":").lower() in active_remote.lower() or
                active_remote.lower().rstrip(":") in r.lower()
                for r in found
            ) if found else (is_local and found)

            if found:
                if active_match:
                    label = C.cyan(f"⚡ {name}")   # currently active in skill-sync
                    detail = C.dim("active: " + (found[0] if is_local else ", ".join(r for r in sorted(found))))
                else:
                    label = C.green(f"✓  {name}")
                    detail = C.dim("configured: " + ", ".join(r for r in sorted(found)))
            else:
                label = f"   {name}"
                detail = C.dim("press enter to connect")

            items.append({
                "key": name, "icon": icon, "label": label, "detail": detail,
                "ptype": ptype, "hint": hint, "default_name": default_name,
                "create_args": create_args, "found": found,
                "is_local": is_local, "active_match": active_match,
            })

        def provider_row(item, checked):
            return f"{item['icon']}  {pad(item['label'], 34)} {item['detail']}"

        picker = Picker(items, provider_row,
                        "🔧 Setup — Cloud Providers",
                        single=True,
                        hint="⚡ active   ✓ configured   enter → use/connect   esc → back")
        chosen = picker.run(term, cfg)
        if not chosen:
            return cfg

        p = chosen[0]

        # ── Local folder: ask for path, no rclone remote needed ─────────────
        if p["is_local"]:
            term.draw(header(cfg) + [
                "  " + C.bold("💾 Local folder / NAS"), "",
                "  " + C.dim("Enter the full path to the folder where skills will be stored."),
                "  " + C.dim("Example:  D:\\SkillsBackup   or   /mnt/nas/skills"),
                "",
            ])
            path = term.ask("  folder path: ")
            if not path:
                continue
            path = path.strip().rstrip("\\/").replace("/", os.sep)
            remote = path
            root = term.ask("  subfolder inside it [ClaudeSkills]: ") or "ClaudeSkills"
            groups = term.ask("  groups, comma separated [work,school,personal]: ") \
                or "work,school,personal"
            run_action(term, "setup",
                       lambda r=remote, ro=root, gr=groups: sync.cmd_setup(
                           Namespace(remote=r, root=ro, categories=gr,
                                     default_category=None, machine=None)))
            cfg = sync.load_config() or cfg
            active_remote = (cfg or {}).get("remote", "")
            # Auto-push all local skills after initial setup
            run_action(term, "initial push — uploading all local skills",
                       lambda: sync.cmd_push(Namespace(skills=None, all=True,
                                                       no_scan=True, dry_run=False,
                                                       force=False, assume_default=True)))
            continue

        # ── Already configured: select and activate ──────────────────────────
        if p["found"]:
            remote_choices = [{"key": r} for r in remotes_raw
                              if p["ptype"] in r.lower()
                              or (p["default_name"] and p["default_name"] in r.lower())
                              or p["hint"] in r.lower()]
            if len(remote_choices) == 1:
                remote = remote_choices[0]["key"]
            else:
                picked2 = Picker(remote_choices, lambda it, ch: it["key"],
                                 f"Pick the {p['key']} remote to use",
                                 single=True).run(term, cfg)
                if not picked2:
                    continue
                remote = picked2[0]["key"]

            activate(remote)
            continue

        # ── Not configured: run rclone config create ─────────────────────────
        term.cooked()
        sys.stdout.write("\x1b[H\x1b[2J")
        rclone_cmd = [exe] + p["create_args"]
        print(f"  Connecting {p['key']} ...")
        print(f"  running: {' '.join(rclone_cmd)}")
        print(hr)
        print()
        print("  rclone will open a browser for OAuth authentication.")
        print("  Complete the login in the browser, then return here.")
        print()
        try:
            _sp.run(rclone_cmd, check=False)
        except Exception as e:
            print(f"  error: {e}")
        print()
        print("  press any key to return to the provider list ...")
        term.pause()
        term.raw()
        try:
            term.read_key()
        except KeyboardInterrupt:
            pass
        configured, remotes_raw = refresh_configured()

        # The OAuth step above only registers the remote with rclone - it does not
        # yet make it skill-sync's active remote. Activate it now, otherwise the
        # picker keeps showing the OLD remote as "active" even though the user just
        # connected a new one (they'd have to notice and select it a second time).
        new_matches = [r for r in remotes_raw
                       if p["ptype"] in r.lower()
                       or (p["default_name"] and p["default_name"] in r.lower())
                       or p["hint"] in r.lower()]
        if new_matches:
            remote = new_matches[0]
            if len(new_matches) > 1:
                picked2 = Picker([{"key": r} for r in new_matches], lambda it, ch: it["key"],
                                 f"Pick the {p['key']} remote to use",
                                 single=True).run(term, cfg)
                if not picked2:
                    continue
                remote = picked2[0]["key"]
            activate(remote)
        # loop back to picker



def screen_hooks(term, cfg):
    import install_hooks
    settings = Path.home() / ".claude" / "settings.json"
    data = sync.load_json(settings, {}) or {}
    import json as _json
    installed = "hook-stop" in _json.dumps(data.get("hooks", {}))
    lines = header(cfg) + [
        "  " + C.bold("Automatic sync"), "",
        f"    status   {C.green('installed') if installed else C.yellow('not installed')}", "",
        "  " + C.dim("Stop hook          uploads changed skills when a session ends"),
        "  " + C.dim("SessionStart hook  tells you when the remote has newer skills"), "",
        "  " + (C.bold("r") + C.dim(" remove") if installed else C.bold("i") + C.dim(" install")),
        "  " + C.bold("esc") + C.dim(" back"),
    ]
    term.draw(lines)
    key = term.read_key()
    if installed and key == "r":
        run_action(term, "uninstall hooks",
                   lambda: install_hooks.main_with(uninstall=True))
    elif not installed and key == "i":
        run_action(term, "install hooks", lambda: install_hooks.main_with(uninstall=False))


def screen_doctor(term, cfg):
    run_action(term, "doctor", lambda: sync.cmd_doctor(Namespace()))


# ---------------------------------------------------------------- main menu

# Format: (key, icon, text_label, description)
# Icon and text are separated so padding is applied only to pure ASCII text.
MENU = [
    ("status",    "📊", "Status",        "Inspect local vs cloud skill differences"),
    ("push",      "📤", "Upload",         "Send changed skills to cloud remote"),
    ("pull",      "📥", "Download",       "Bring skill groups onto this computer"),
    ("groups",    "🏷️", "Groups",         "Organize skills into work / school / personal"),
    ("conflicts", "⚔️", "Conflicts",      "Inspect diffs & resolve conflicting edits"),
    ("hooks",     "🔄", "Auto-Sync",      "Install or remove automatic session hooks"),
    ("setup",     "🔧", "Setup",          "Configure Rclone remote & cloud storage"),
    ("doctor",    "🩺", "Doctor",         "Diagnose system health & dependencies"),
    ("prune",     "🗑️", "Delete Remote",  "Remove orphan skills from cloud storage"),
    ("help",      "❓", "Help",           "Show commands, shortcuts and badge legend"),
    ("quit",      "🚪", "Quit",           "Exit interactive menu"),
]


def menu_frame(cfg, status, cursor):
    lines = header(cfg, summary_line(status) if status else None)
    lines.append("")
    width = min(term_size()[0], 100)
    TEXT_W = 14  # pure ASCII text column width (no emoji)
    for idx, (key, icon, text_label, desc) in enumerate(MENU):
        marker = C.cyan(G.g["cursor"]) if idx == cursor else " "
        label_part = C.bold(text_label) if idx == cursor else text_label
        row = f"  {marker} {icon} {pad(label_part, TEXT_W)} {C.dim(desc)}"
        lines.append(C.inv(pad(row, width)) if idx == cursor else row)
    if status:
        total = len(status)
        synced = sum(1 for i in status.values() if i["state"] == sync.IN_SYNC)
        pct = round(100 * synced / total) if total else 0
        lines += ["", f"  {bar(synced, total)}  {C.bold(f'{pct}%')} {C.dim(f'({synced}/{total} in sync)')}"]
    return lines + footer([("up/down (j/k)", "move"), ("enter", "open"), ("r", "refresh"),
                           ("q", "quit")])


def screen_skill_detail(term, cfg, status, skill_name):
    """Action panel for a single skill selected from the Status screen."""
    while True:
        info = status.get(skill_name, {})
        state = info.get("state", "?")
        badge = state_badge(state)
        origin = origin_badge(info.get("local_path"))
        lines = header(cfg) + [
            f"  {C.bold(skill_name)}  {origin}  {badge}",
            f"  {C.dim('category:')}  {info.get('category') or C.dim('none')}   "
            f"{C.dim('size:')}  {sync.human_size(info.get('size'))}",
            "",
            C.dim("─" * min(term_size()[0], 100)),
            C.bold("  Actions:"),
            "",
        ]
        # Build context-aware actions based on state
        actions = []
        if state in (sync.LOCAL_NEW, sync.ONLY_LOCAL):
            actions.append(("u", "📤 Upload",  "push this skill to the cloud"))
        if state in (sync.REMOTE_NEW, sync.ONLY_REMOTE):
            actions.append(("d", "📥 Download", "pull this skill from the cloud"))
        if state == sync.CONFLICT:
            actions.append(("l", "⚔️  Keep Local",  "resolve conflict keeping local version"))
            actions.append(("r", "☁️  Keep Remote", "resolve conflict keeping remote version"))
        actions.append(("g", "🏷️  Set Group",  "assign or change this skill's group"))
        actions.append(("esc", "← Back",       "go back to status list"))

        for key, label, desc in actions:
            lines.append(f"  {C.bold(key)}  {label}  {C.dim(desc)}")
        term.draw(lines)
        key = term.read_key()
        if key in ("esc", "q"):
            return
        if key == "u" and state in (sync.LOCAL_NEW, sync.ONLY_LOCAL):
            run_action(term, f"push {skill_name}",
                       lambda: sync.cmd_push(Namespace(skills=[skill_name], all=False,
                                                       no_scan=True, dry_run=False,
                                                       force=False, assume_default=False)))
            status.update(load_status(term, cfg, force=True))
        elif key == "d" and state in (sync.REMOTE_NEW, sync.ONLY_REMOTE):
            run_action(term, f"pull {skill_name}",
                       lambda: sync.cmd_pull(Namespace(categories=None, skills=[skill_name],
                                                       dest=None, dry_run=False, force=False)))
            status.update(load_status(term, cfg, force=True))
        elif key == "l" and state == sync.CONFLICT:
            run_action(term, f"resolve {skill_name} --keep local",
                       lambda: sync.cmd_resolve(Namespace(skill=skill_name, keep="local")))
            status.update(load_status(term, cfg, force=True))
        elif key == "r" and state == sync.CONFLICT:
            run_action(term, f"resolve {skill_name} --keep remote",
                       lambda: sync.cmd_resolve(Namespace(skill=skill_name, keep="remote")))
            status.update(load_status(term, cfg, force=True))
        elif key == "g":
            cats = list(cfg.get("categories") or [])
            cat_items = [{"key": c} for c in cats] + [{"key": "+ new group ..."}]
            cat_picker = Picker(cat_items, lambda it, ch: it["key"],
                                "Which group?", single=True)
            picked = cat_picker.run(term, cfg)
            if picked:
                category = picked[0]["key"]
                if category.startswith("+"):
                    category = term.ask("  new group name: ")
                if category:
                    run_action(term, f"categorize {skill_name} → {category}",
                               lambda cat=category: sync.cmd_categorize(
                                   Namespace(skill=skill_name, category=cat, force=False)))
                    status.update(load_status(term, cfg, force=True))


def status_screen(term, cfg, status):
    """Status list: select a skill to open its action panel."""
    while True:
        items = to_items(status, sorted(status))
        picker = Picker(items, skill_row, "Status", single=True,
                        hint="enter → open actions   esc → back")
        chosen = picker.run(term, cfg, summary_line(status))
        if not chosen:
            return
        skill_name = chosen[0]["key"]
        screen_skill_detail(term, cfg, status, skill_name)
        status = load_status(term, cfg, force=True)


def main_loop(term):
    cursor = 0
    cfg = sync.load_config()
    status = load_status(term, cfg) if cfg else {}
    while True:
        term.draw(menu_frame(cfg, status, cursor))
        try:
            key = term.read_key()
        except KeyboardInterrupt:
            return 0
        if key in ("up", "down", "j", "k"):
            step = 1 if key in ("down", "j") else -1
            cursor = (cursor + step) % len(MENU)
            continue
        if key in ("q", "esc"):
            return 0
        if key == "r":
            cfg = sync.load_config()
            status = load_status(term, cfg, force=True) if cfg else {}
            continue
        if key != "enter":
            continue

        choice = MENU[cursor][0]
        if choice == "quit":
            return 0
        if choice == "setup":
            cfg = screen_setup(term, cfg)
            status = load_status(term, cfg, force=True) if cfg else {}
            continue
        if not cfg:
            term.draw(header(cfg) + ["  " + C.yellow("run Setup first"), "",
                                     C.dim("  press any key")])
            term.read_key()
            continue
        if choice == "hooks":
            screen_hooks(term, cfg)
            continue
        if choice == "doctor":
            screen_doctor(term, cfg)
            continue

        if choice == "help":
            screen_help(term, cfg)
            continue
        status = load_status(term, cfg)
        {"status": status_screen, "push": screen_push, "pull": screen_pull,
         "groups": screen_groups, "conflicts": screen_conflicts,
         "prune": screen_prune}[choice](term, cfg, status)
        status = load_status(term, cfg, force=True)


def screen_help(term, cfg):
    g = G.g
    width = min(term_size()[0], 100)
    hr = C.dim(g["h"] * width)
    lines = header(cfg)
    lines += [
        C.bold("  Commands"),
        "",
        f"  {C.cyan('status')}       {C.dim('Show which skills differ between this PC and the cloud')}",
        f"  {C.cyan('push')}         {C.dim('Upload new or modified skills to the cloud remote')}",
        f"  {C.cyan('pull')}         {C.dim('Download skill categories from the cloud to this PC')}",
        f"  {C.cyan('groups')}       {C.dim('Assign skills to categories: work / school / personal')}",
        f"  {C.cyan('conflicts')}    {C.dim('View diffs and pick which version wins')}",
        f"  {C.cyan('auto-sync')}    {C.dim('Install hooks that auto-sync on session start / end')}",
        f"  {C.cyan('setup')}        {C.dim('Configure Rclone remote, cloud root folder and groups')}",
        f"  {C.cyan('doctor')}       {C.dim('Run diagnostics on Rclone, config and hooks')}",
        f"  {C.cyan('delete remote')}{C.dim(' Remove orphan skills from the cloud (with confirmation)')}",
        "",
        hr,
        C.bold("  Keyboard Shortcuts"),
        "",
        f"  {C.bold('up / down')}   or   {C.bold('j / k')}     Move cursor",
        f"  {C.bold('enter')}                          Open selected option",
        f"  {C.bold('space')}                          Toggle checkbox (in pickers)",
        f"  {C.bold('a / n / i')}                      Select all / none / invert",
        f"  {C.bold('/')}                              Filter skills by name",
        f"  {C.bold('r')}                              Refresh status from remote",
        f"  {C.bold('q / esc')}                        Go back / quit",
        "",
        hr,
        C.bold("  State Badge Legend"),
        "",
        f"  {C.green('🟢 in-sync')}        Skill matches the cloud version",
        f"  {C.yellow('⚡ local-newer')}    Local edits not yet uploaded",
        f"  {C.blue('☁️  remote-newer')}   Cloud has a newer version to download",
        f"  {C.magenta('📦 not-uploaded')}   Skill exists locally but never pushed",
        f"  {C.cyan('📥 remote-only')}    Skill is on the cloud but not on this PC",
        f"  {C.red('🚨 CONFLICT')}       Both sides changed — manual resolution needed",
        "",
        hr,
        C.bold("  Origin Badge Legend"),
        "",
        f"  {C.blue('🔵 Claude')}    Skill from  ~/.claude/skills/",
        f"  {C.cyan('🟣 Gemini')}    Skill from  ~/.gemini/config/skills/",
        f"  {C.yellow('🟠 Agents')}    Skill from  .agents/skills/",
        "",
        C.dim("  press any key to go back"),
    ]
    term.draw(lines)
    term.read_key()


# ---------------------------------------------------------------- self check

def self_check():
    """Render every frame once with fake data - no terminal input needed."""
    cfg = {"remote": "gdrive:", "root": "ClaudeSkills", "machine": "demo-pc",
           "categories": ["work", "school", "personal"], "default_category": "personal"}
    status = {
        "web-builder": dict(name="web-builder", local=True, local_path="~/.claude/skills/web-builder",
                            remote=True, category="work", state=sync.IN_SYNC, size=41000, files=6,
                            remote_machine="laptop", remote_updated="2026-08-01T10:00:00+00:00"),
        "thesis-helper": dict(name="thesis-helper", local=True, local_path="~/.gemini/config/skills/thesis-helper",
                              remote=False, category=None, state=sync.ONLY_LOCAL, size=9000, files=2,
                              remote_machine=None, remote_updated=None),
        "last30days": dict(name="last30days", local=True, local_path="~/.claude/skills/last30days",
                           remote=True, category="personal", state=sync.CONFLICT, size=8400000, files=44,
                           remote_machine="desktop", remote_updated="2026-08-03T22:10:00+00:00"),
        "recipe-notes": dict(name="recipe-notes", local=False, local_path=None, remote=True, category="personal",
                             state=sync.ONLY_REMOTE, size=3000, files=1, remote_machine="desktop",
                             remote_updated="2026-07-30T08:00:00+00:00"),
    }
    print("\n=== main menu ===")
    print("\n".join(menu_frame(cfg, status, 1)))
    print("\n=== skill picker (upload) ===")
    items = to_items(status, sorted(n for n, i in status.items() if i["local"]))
    p = Picker(items, skill_row, "Upload skills", checked=[1],
               hint="pre-selected: everything not yet on the remote")
    p.cursor = 1
    print("\n".join(p.frame(cfg, summary_line(status))))
    print("\n=== group picker (download) ===")
    cats = [{"key": "work", "names": ["web-builder"]},
            {"key": "personal", "names": ["last30days", "recipe-notes"]}]
    cp = Picker(cats, lambda it, ch: f"{pad(it['key'], 18)} "
                                     f"{pad(str(len(it['names'])) + ' skills', 12)} "
                                     f"{C.green('1 new here')}",
                "Download groups", checked=[0])
    print("\n".join(cp.frame(cfg, summary_line(status))))
    print("\n=== not configured ===")
    print("\n".join(header(None)))
    return 0


def main():
    import json
    ap = argparse.ArgumentParser(description="Interactive menu for skill-sync")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--ascii", action="store_true", help="ASCII-only frame characters")
    ap.add_argument("--self-check", action="store_true", help="render screens once and exit")
    ap.add_argument("--json", action="store_true", help="output status in JSON format")
    args = ap.parse_args()

    if args.no_color or os.environ.get("NO_COLOR"):
        C.enabled = False
    enc = (sys.stdout.encoding or "").lower()
    if args.ascii or ("utf" not in enc and "65001" not in enc):
        G.unicode = False

    if args.self_check:
        return self_check()

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        if args.json:
            cfg = sync.load_config()
            status = sync.compute_status(cfg) if cfg else {}
            print(json.dumps({"config": cfg, "skills": status}, indent=2, ensure_ascii=False))
            return 0
        print("skill-sync menu needs a real terminal (interactive stdin).")
        print("Run it yourself; inside Claude Code prefix the command with '!':")
        print(f"    ! python {Path(__file__).resolve()}")
        print("\nNon-interactive alternative:")
        print(f"    python {Path(__file__).resolve().parent / 'sync.py'} status --json")
        return 2

    sync.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with Term() as term:
        return main_loop(term)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
