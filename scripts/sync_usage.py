"""Usage counter: which skills are invoked in which project, mined from transcripts.

Part of skill-sync; `sync.py` is the entry point and re-exports these names.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
from pathlib import Path

from sync_core import (  # noqa: F401
    CONFIG_FILE,
    CONFLICTS_DIR,
    HOME,
    KEEP_TRASH_DAYS,
    LOCK_FILE,
    LOG_FILE,
    Lock,
    NO_CATEGORY,
    STATE_DIR,
    STATE_FILE,
    Spinner,
    SyncError,
    TRASH_DIR,
    get_all_skill_dirs,
    human_size,
    load_config,
    load_json,
    load_packs,
    load_state,
    local_skills_map,
    lock_is_live,
    log,
    now_iso,
    require_config,
    save_json,
    stamp,
)
from sync_remote import (  # noqa: F401
    base_path,
    git_bin,
    git_cfg,
    git_clone_path,
    git_run,
    git_sync_in,
    git_sync_out,
    rclone,
    rclone_bin,
    read_manifest,
    rpath,
    write_manifest,
)
from sync_scan import (  # noqa: F401
    entry_categories,
    fingerprint,
    local_skills,
    local_version,
    manifest_entry,
    primary_category,
    pull_skill,
    push_skill,
    record_synced,
    skill_dest_dir,
    skill_path,
    stash_remote_copy,
    trash_size_bytes,
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


def cmd_resolve(args):
    cfg = require_config()
    git_sync_in(cfg)
    name = args.skill
    with Lock():
        manifest = read_manifest(cfg)
        entry = manifest["skills"].get(name)
        if not entry:
            raise SyncError(f"{name} is not on the remote - nothing to resolve")
        category = primary_category(entry, NO_CATEGORY)
        categories = entry_categories(entry) or [category]

        # The skill may live under any client's folder, not just SKILLS_DIR.
        local_dir = skill_path(name, cfg)

        if args.keep == "local":
            if local_dir is None:
                raise SyncError(f"{name} does not exist in any local skills dir")
            backup = stash_remote_copy(cfg, name, category)
            print(f"remote version saved to: {backup}")
            with Spinner(f"hashing {name}"):
                fp, mtime, files, size = fingerprint(local_dir)
            push_skill(cfg, name, category, skill_dir=local_dir)
            write_manifest(cfg, {name: manifest_entry(cfg, category, fp, size, files,
                                                      categories=categories)})
            record_synced(name, category, fp, mtime, files)
            git_sync_out(cfg, f"resolve {name} (keep local)")
            print(f"resolved: LOCAL version of {name} is now on the remote")
        else:
            dest_root = skill_dest_dir(name, cfg)
            if local_dir is not None:
                backup = CONFLICTS_DIR / f"{name}-local-{stamp()}"
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(local_dir, backup, dirs_exist_ok=True)
                print(f"local version saved to: {backup}")
            dest_root.mkdir(parents=True, exist_ok=True)
            pull_skill(cfg, name, category, dest_root)
            with Spinner(f"hashing {name}"):
                fp, mtime, files, _size = fingerprint(dest_root / name)
            record_synced(name, category, fp, mtime, files)
            print(f"resolved: REMOTE version of {name} is now local ({dest_root / name})")
        log(f"resolve {name} keep={args.keep}")
        return 0


def cmd_prune(args):
    cfg = require_config()
    git_sync_in(cfg)
    with Lock():
        manifest = read_manifest(cfg)
        local = set(local_skills())
        orphans = sorted(n for n in manifest.get("skills", {}) if n not in local)
        if not args.only:
            targets = orphans
        else:
            targets = [n for n in orphans if n in args.only]
            missing = set(args.only) - set(orphans)
            for n in sorted(missing):
                print(f"skipped {n}: still exists locally or is not on the remote")
        if not targets:
            print("Nothing to prune: every remote skill also exists on this machine.")
            return 0

        print("Remote skills that do NOT exist on this machine:")
        for n in targets:
            e = manifest["skills"][n]
            print(f"  {n}  (category {e.get('category')}, uploaded by {e.get('machine')} "
                  f"on {e.get('updated_at')})")
        if not args.yes:
            print("\nWARNING: deleting is permanent for every machine. If a skill only lives on")
            print("another computer, pruning it here loses it there too.")
            print("Re-run with --yes to delete (optionally --only <skill> ...).")
            return 2

        for n in targets:
            category = primary_category(manifest["skills"][n], NO_CATEGORY)
            print(f"deleting remote {category}/{n}")
            rclone(["purge", rpath(cfg, category, n)], check=False, timeout=900)
        write_manifest(cfg, {}, drop=targets)
        st = load_state()
        for n in targets:
            st["skills"].pop(n, None)
        save_json(STATE_FILE, st)
        print(f"Removed {len(targets)} skill(s) from the remote.")
        log(f"prune {targets}")
        git_sync_out(cfg, f"prune {', '.join(sorted(targets)[:6])}")
        return 0


def cmd_doctor(args):
    ok = True
    cfg = load_config()
    s_dirs = get_all_skill_dirs(cfg)
    dirs_str = ", ".join(str(d) for d in s_dirs)
    exe = rclone_bin(required=False)
    settings = HOME / ".claude" / "settings.json"
    data = load_json(settings, {}) or {}
    hooks = json.dumps(data.get("hooks", {}))

    if getattr(args, "json", False):
        print(json.dumps({
            "version": local_version(),
            "skills_dirs": [str(d) for d in s_dirs],
            "total_skills": len(local_skills(cfg)),
            "state_dir": str(STATE_DIR),
            "rclone": exe,
            "has_config": bool(cfg),
            "stop_hook": "hook-stop" in hooks,
            "session_start_hook": "hook-session-start" in hooks,
            "lock_exists": LOCK_FILE.exists(),
            "lock_live": lock_is_live(),
            "trash_bytes": trash_size_bytes(),
        }, indent=2, ensure_ascii=False))
        return 0

    print(f"version        : {local_version()}")
    print(f"skills dirs    : {dirs_str} "
          f"({len(local_skills(cfg))} total skills)")
    print(f"state dir      : {STATE_DIR}")
    if exe:
        _c, out, _e = rclone(["version"], check=False, timeout=30)
        print(f"rclone         : {exe} ({out.splitlines()[0] if out else 'unknown'})")
    else:
        ok = False
        print("rclone         : NOT FOUND  -> python scripts/provision.py rclone")

    if not cfg:
        ok = False
        print("config         : missing  -> python sync.py setup --remote <remote:>")
    else:
        print(f"config         : {CONFIG_FILE}")
        print(f"remote         : {base_path(cfg)}")
        print(f"categories     : {', '.join(cfg.get('categories', [])) or '(none)'}")
        packs = load_packs(cfg)
        if packs:
            print(f"packs          : {', '.join(sorted(packs))}")
        g = git_cfg(cfg)
        if g:
            clone = git_clone_path(cfg)
            gexe = git_bin(required=False)
            print(f"git repo       : {g['url']}  (branch {g.get('branch')})")
            if not gexe:
                ok = False
                print("git            : NOT FOUND  -> install git and put it on PATH")
            elif not (clone / ".git").exists():
                print(f"git clone      : {clone}  (not cloned yet, happens on first use)")
            else:
                _c, out, _e = git_run(clone, ["status", "--porcelain"], check=False)
                _c2, ahead, _e2 = git_run(
                    clone, ["rev-list", "--count", f"origin/{g.get('branch')}..HEAD"],
                    check=False)
                dirty = len([l for l in out.splitlines() if l.strip()])
                print(f"git clone      : {clone}")
                print(f"git state      : {dirty} uncommitted change(s), "
                      f"{(ahead or '0').strip() or '0'} commit(s) not pushed")
        if exe:
            code, _o, err = rclone(["lsf", base_path(cfg), "--max-depth", "1"],
                                   check=False, timeout=90)
            if code == 0:
                print("remote reach   : OK")
            else:
                ok = False
                print(f"remote reach   : FAILED - {err.strip().splitlines()[-1][:160] if err else ''}")

    print(f"Stop hook      : {'installed' if 'hook-stop' in hooks else 'not installed'}")
    print(f"SessionStart   : {'installed' if 'hook-session-start' in hooks else 'not installed'}")
    if "hook-stop" not in hooks:
        print("                 -> python scripts/install_hooks.py")
    if LOCK_FILE.exists():
        if lock_is_live():
            print(f"lock           : held by a running sync ({LOCK_FILE})")
        else:
            print(f"lock           : stale ({LOCK_FILE}) - the next run clears it by itself")
    held = trash_size_bytes()
    if held:
        print(f"backups        : {human_size(held)} in {TRASH_DIR.parent} "
              f"(deleted after {KEEP_TRASH_DAYS} days)")
    print(f"log            : {LOG_FILE}")
    return 0 if ok else 1


def cmd_merge(args):
    cfg = require_config()
    name = args.skill
    manifest = read_manifest(cfg)
    entry = manifest.get("skills", {}).get(name)
    if not entry:
        raise SyncError(f"skill '{name}' is not present on the remote")

    category = primary_category(entry, NO_CATEGORY)
    lmap = local_skills_map(cfg)
    local_dir = lmap.get(name)
    if not local_dir:
        raise SyncError(f"skill '{name}' does not exist locally")

    remote_stash = stash_remote_copy(cfg, name, category)

    local_md = local_dir / "SKILL.md"
    remote_md = remote_stash / "SKILL.md"

    diff_lines = []
    if local_md.exists() and remote_md.exists():
        l_text = local_md.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        r_text = remote_md.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        diff_lines = list(difflib.unified_diff(
            r_text, l_text,
            fromfile=f"remote/{category}/{name}/SKILL.md",
            tofile=f"local/{name}/SKILL.md"
        ))

    diff_text = "".join(diff_lines)

    if getattr(args, "json", False):
        print(json.dumps({
            "skill": name,
            "category": category,
            "local_path": str(local_dir),
            "remote_stash": str(remote_stash),
            "has_conflict": bool(diff_lines),
            "diff": diff_text,
        }, indent=2, ensure_ascii=False))
    else:
        print(f"Merge analysis for skill '{name}':")
        print(f"  Local path   : {local_dir}")
        print(f"  Remote stash  : {remote_stash}")
        if diff_lines:
            print("\n--- SKILL.md Diff (Remote -> Local) ---")
            print(diff_text)
        else:
            print("\nSKILL.md files are identical.")

    if args.keep:
        resolve_args = argparse.Namespace(skill=name, keep=args.keep)
        return cmd_resolve(resolve_args)

    return 0
