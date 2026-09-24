"""Usage counter: which skills are invoked in which project, mined from transcripts.

Part of skill-sync; `sync.py` is the entry point and re-exports these names.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from sync_core import (  # noqa: F401
    HOME,
    STATE_FILE,
    human_size,
    load_config,
    load_packs,
    load_state,
    local_skills_map,
    now_iso,
    save_json,
)


# --------------------------------------------------------------- usage counter

# Which skills actually get used in which project, so a pack can eventually be proposed
# from evidence instead of from memory. The signal is mined from Claude Code's own
# transcripts, reading only the bytes appended since the last scan, because the folder is
# hundreds of megabytes and this runs from the Stop hook.
CLAUDE_PROJECTS_DIR = HOME / ".claude" / "projects"
USAGE_BUDGET_BYTES = 40 * 1024 * 1024
USAGE_HOOK_BUDGET_BYTES = 8 * 1024 * 1024
USAGE_SKILL_RE = re.compile(
    r'"skill"\s*:\s*"([^"]{1,80})"|<command-name>/?([A-Za-z0-9_:.-]{1,80})</command-name>')
USAGE_CWD_RE = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.){1,400})"')


def merge_usage_case_duplicates(u) -> None:
    """Windows hands out both `C:\\x` and `c:\\x` for the same folder.

    Two spellings meant two buckets and a project's counts split between them, so the
    first spelling seen wins and the rest are folded into it.
    """
    seen = {}
    for key in list(u["projects"]):
        norm = os.path.normcase(key)
        if norm not in seen:
            seen[norm] = key
            continue
        keep = u["projects"][seen[norm]]
        drop = u["projects"].pop(key)
        skills = keep.setdefault("skills", {})
        for name, count in (drop.get("skills") or {}).items():
            skills[name] = skills.get(name, 0) + count
        keep["sessions"] = int(keep.get("sessions") or 0) + int(drop.get("sessions") or 0)


def usage_project_key(u, project: str) -> str:
    """The bucket this path belongs to, matching case-insensitively where that applies."""
    if project in u["projects"]:
        return project
    norm = os.path.normcase(project)
    for existing in u["projects"]:
        if os.path.normcase(existing) == norm:
            return existing
    return project


def usage_state():
    st = load_state()
    u = st.get("usage")
    if not isinstance(u, dict):
        u = {}
    u.setdefault("projects", {})
    u.setdefault("scan", {})
    merge_usage_case_duplicates(u)
    st["usage"] = u
    return st, u


def usage_known_names(cfg) -> set:
    """Only real skills are counted.

    Transcripts also carry `/model`, `/clear` and every other CLI command; intersecting
    with what is actually installed drops that noise without maintaining a denylist.
    """
    names = set(local_skills_map(cfg))
    for p in load_packs(cfg or {}).values():
        names |= set(p.get("skills") or [])
    return names


def scan_usage(cfg, budget_bytes=USAGE_BUDGET_BYTES, full=False) -> dict:
    result = {"files": 0, "bytes": 0, "hits": 0, "projects": 0, "pending": 0}
    if not CLAUDE_PROJECTS_DIR.is_dir():
        return result
    known = usage_known_names(cfg)
    st, u = usage_state()
    if full:
        # Forgetting the offsets without forgetting the counts would re-read every
        # transcript and add its hits a second time, inflating everything.
        u["scan"] = {}
        u["projects"] = {}
    touched = set()
    for folder in sorted(CLAUDE_PROJECTS_DIR.iterdir()):
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.jsonl")):
            key = str(f)
            rec = u["scan"].get(key) or {}
            try:
                size = f.stat().st_size
            except OSError:
                continue
            offset = int(rec.get("offset") or 0)
            if offset > size:
                offset = 0                      # rewritten or rotated: start over
            if offset >= size:
                continue
            if result["bytes"] >= budget_bytes:
                result["pending"] += 1          # next run picks this one up
                continue
            room = budget_bytes - result["bytes"]
            try:
                with open(f, "rb") as fh:
                    fh.seek(offset)
                    raw = fh.read(min(size - offset, room))
            except OSError:
                continue
            # Stop at the last complete line: the session being closed right now is still
            # appending, and half a line would both miscount and corrupt the offset.
            cut = raw.rfind(b"\n")
            if cut == -1:
                continue
            consumed = cut + 1
            text = raw[:consumed].decode("utf-8", errors="replace")
            result["bytes"] += consumed
            result["files"] += 1

            project = rec.get("project") or ""
            if not project:
                m = USAGE_CWD_RE.search(text)
                project = m.group(1).replace("\\\\", "\\") if m else folder.name
            project = usage_project_key(u, project)
            bucket = u["projects"].setdefault(project, {})
            skills = bucket.setdefault("skills", {})
            for m in USAGE_SKILL_RE.finditer(text):
                name = (m.group(1) or m.group(2) or "").split(":")[-1].strip()
                if name and name in known:
                    skills[name] = skills.get(name, 0) + 1
                    result["hits"] += 1
            if not rec:
                bucket["sessions"] = int(bucket.get("sessions") or 0) + 1
            bucket["last_seen"] = now_iso()
            u["scan"][key] = {"offset": offset + consumed, "project": project}
            touched.add(project)
            if offset + consumed < size:
                result["pending"] += 1          # budget ran out mid-file, or a partial line

    u["updated_at"] = now_iso()
    save_json(STATE_FILE, st)
    result["projects"] = len(touched)
    return result


def usage_for_project(project=None, cfg=None) -> list:
    """[(skill, count)] for one project, or across all of them, most used first."""
    _st, u = usage_state()
    wanted = None
    if project:
        try:
            wanted = str(Path(project).expanduser().resolve()).lower()
        except OSError:
            wanted = str(project).lower()
    totals = {}
    for key, bucket in u["projects"].items():
        if wanted is not None:
            try:
                here = str(Path(key).expanduser().resolve()).lower()
            except OSError:
                here = key.lower()
            if here != wanted:
                continue
        for name, n in (bucket.get("skills") or {}).items():
            totals[name] = totals.get(name, 0) + n
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))


def cmd_usage(args):
    cfg = load_config() or {}
    action = getattr(args, "usage_action", None) or "show"
    if action == "scan":
        budget = float(getattr(args, "budget_mb", None) or 40)
        res = scan_usage(cfg, budget_bytes=int(budget * 1024 * 1024),
                         full=getattr(args, "full", False))
        print(f"read {human_size(res['bytes'])} from {res['files']} transcript(s): "
              f"{res['hits']} skill invocation(s) in {res['projects']} project(s)")
        if res["pending"]:
            print(f"{res['pending']} transcript(s) left for the next scan (byte budget). "
                  f"Raise it with --budget-mb, or just run it again.")
        return 0

    _st, u = usage_state()
    if not u["projects"]:
        print("Nothing recorded yet. Build the history with:\n"
              "    python sync.py usage scan --full\n"
              "From then on the Stop hook keeps it up to date by itself.")
        return 2
    top = int(getattr(args, "top", None) or 10)
    project = getattr(args, "project", None)
    if project:
        rows = usage_for_project(project, cfg)
        print(f"{Path(project).expanduser().resolve()}\n")
        if not rows:
            print("  no skill usage recorded for this project")
            return 2
        for name, n in rows[:top]:
            print(f"  {name:<30} {n}")
        return 0

    print(f"skill usage per project (last scan {str(u.get('updated_at'))[:16]})\n")
    order = sorted(u["projects"].items(),
                   key=lambda kv: -sum((kv[1].get("skills") or {}).values()))
    for key, bucket in order:
        skills = bucket.get("skills") or {}
        if not skills:
            continue
        best = sorted(skills.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
        print(f"  {key}   ({bucket.get('sessions') or 0} session(s))")
        print(f"      {', '.join(f'{n} ({c})' for n, c in best)}")
    total = sum(sum((b.get('skills') or {}).values()) for b in u["projects"].values())
    print(f"\n{total} invocation(s) recorded. "
          f"`pack create <name> --from-usage --from-project <folder>` turns one project's "
          f"top skills into a pack.")
    return 0
