#!/usr/bin/env python3
"""skill-sync - sync your Claude Code skills across machines with rclone.

Skills stay FLAT locally (Claude Code only discovers <skills-dir>/<name>/SKILL.md,
one level deep) and are organised into categories on the remote:

    <remote-root>/manifest.json
    <remote-root>/<category>/<skill>/...

Runtime state lives in ~/.claude/skill-sync/ (config.json, state.json, conflicts/,
trash/, sync.log) and is never uploaded.

Any rclone backend works: Google Drive, Dropbox, OneDrive, S3, Box, WebDAV, a local
folder, ... Run `rclone config` once to create a remote, then `sync.py setup`.

Exit codes: 0 ok / 1 error or blocked / 2 needs user input.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_core import (  # noqa: F401
    BIG_SKILL_BYTES,
    CONFIG_FILE,
    CONFLICT,
    HOME,
    HOOK_BUDGET_SECONDS,
    IN_SYNC,
    LOCAL_NEW,
    Lock,
    NO_CATEGORY,
    ONLY_LOCAL,
    ONLY_REMOTE,
    REMOTE_CHECK_INTERVAL,
    REMOTE_NEW,
    REPO_URL,
    SECRET_ALLOW_PRAGMA,
    SELF_NAME,
    SKILLS_DIR,
    STATE_DIR,
    STATE_FILE,
    Spinner,
    SyncError,
    TRASH_DIR,
    _interactive,
    get_all_skill_dirs,
    human_size,
    load_config,
    load_json,
    load_packs,
    load_state,
    local_skills_map,
    lock_is_live,
    log,
    machine_name,
    now_iso,
    progress_bar,
    require_config,
    save_json,
)
from sync_remote import (  # noqa: F401
    GIT_CLONE_DIR,
    MANIFEST_TEXT_MAX,
    base_path,
    ensure_git_clone,
    git_bin,
    git_cfg,
    git_sync_in,
    git_sync_out,
    normalise_remote,
    rclone,
    rclone_bin,
    read_manifest,
    rpath,
    sanitize_manifest,
    write_manifest,
)
from sync_scan import (  # noqa: F401
    category_matches,
    category_tree_lines,
    compute_status,
    entry_categories,
    fingerprint,
    fingerprint_cached,
    is_self,
    local_skills,
    manifest_entry,
    normalise_category,
    primary_category,
    pull_skill,
    purge_backups,
    push_skill,
    quick_sig,
    record_synced,
    scan_secrets,
    skill_dest_dir,
    skill_path,
    syncable,
)
from sync_usage import (  # noqa: F401
    CLAUDE_PROJECTS_DIR,
    USAGE_HOOK_BUDGET_BYTES,
    cmd_doctor,
    cmd_merge,
    cmd_prune,
    cmd_resolve,
    cmd_usage,
    scan_usage,
    usage_for_project,
    usage_state,
)
from sync_packs import (  # noqa: F401
    CLIENT_DIRS,
    CLIENT_PROJECT_DIRS,
    cmd_pack,
    pack_dest,
    place_one,
    resolve_pack,
)


# ------------------------------------------------------------------ commands

def repo_slug(url: str) -> str:
    """`git@host:team/skills.git`, `https://host/team/skills`, `D:\\git\\skills.git` -> `skills`.

    The backslash matters: a Windows path pasted as the repo URL otherwise came out as one
    enormous slug, and the clone failed on the resulting path length.
    """
    tail = re.split(r"[/:\\]", url.strip().rstrip("/\\"))[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return re.sub(r"[^A-Za-z0-9._-]", "-", tail)[:48] or "skills"


def cmd_setup(args):
    rclone_bin()
    _c, out, _e = rclone(["listremotes"], check=False, timeout=60)
    remotes = [r.strip() for r in out.splitlines() if r.strip()]

    git_url = getattr(args, "git", None)
    git_block = None
    if git_url:
        git_bin()
        clone = STATE_DIR / GIT_CLONE_DIR / repo_slug(git_url)
        git_block = {"url": git_url.strip(),
                     "branch": (getattr(args, "git_branch", None) or "main").strip(),
                     "clone": str(clone)}
        # The clone is an ordinary folder, so it becomes the rclone "remote" and every
        # existing code path keeps working; git only wraps the reads and writes.
        args.remote = str(clone)

    if not args.remote:
        if not remotes:
            print("No rclone remotes configured yet.")
            print("Create one (Google Drive, Dropbox, OneDrive, S3, ...) by running this")
            print("yourself - it is interactive, Claude cannot drive it:\n")
            print("    rclone config\n")
            print("Then: python sync.py setup --remote <name:> --categories work,school,personal")
            return 2
        print("Available rclone remotes:")
        for r in remotes:
            print("  " + r)
        print("\nPick one and run:")
        print("    python sync.py setup --remote <name:> --root ClaudeSkills \\")
        print("        --categories work,school,personal --default-category personal")
        return 2

    remote = normalise_remote(args.remote)
    is_local = ":" not in remote or re.match(r"^[A-Za-z]:[\\/]", remote)
    if remotes and not is_local:
        known = {r.rstrip(":") for r in remotes}
        if remote.split(":")[0] not in known:
            raise SyncError(f"remote '{remote}' does not exist. Available: {', '.join(remotes)}")

    cats = [normalise_category(c) for c in (args.categories or "personal").split(",")
            if c.strip()]
    old = load_config() or {}
    cfg = {
        "remote": remote,
        "root": (args.root if args.root is not None else "ClaudeSkills").strip("/\\"),
        "categories": cats,
        "default_category": args.default_category or cats[0],
        "machine": args.machine or machine_name(),
        "created_at": old.get("created_at") or now_iso(),
    }
    if git_block:
        cfg["git"] = git_block
    # Carry over everything this function does not own - packs, git, extra_skills_dirs,
    # hook_budget_seconds. The menu calls setup just to add a group, and a plain
    # overwrite silently deleted every pack the user had defined.
    for k, v in old.items():
        cfg.setdefault(k, v)
    save_json(CONFIG_FILE, cfg)
    if git_cfg(cfg):
        ensure_git_clone(cfg)
        git_sync_in(cfg)
    if not STATE_FILE.exists():
        save_json(STATE_FILE, {"skills": {}})

    code, _o, err = rclone(["lsf", base_path(cfg), "--max-depth", "1"], check=False, timeout=90)
    if git_cfg(cfg):
        print(f"Git repo    : {cfg['git']['url']}  (branch {cfg['git']['branch']})")
        print(f"Clone       : {cfg['git']['clone']}")
    print(f"Remote      : {base_path(cfg)}")
    print(f"Categories  : {', '.join(cats)}   (default: {cfg['default_category']})")
    print(f"Machine     : {cfg['machine']}")
    print(f"Skills dir  : {SKILLS_DIR}")
    if code != 0:
        print("\nNote: could not list the remote yet (it will be created on first push).")
        print("  " + err.strip().splitlines()[-1][:200] if err.strip() else "")
    print("\nNext: python sync.py status")
    return 0


def cmd_status(args):
    cfg = require_config()
    git_sync_in(cfg, quiet=getattr(args, "json", False))
    st = compute_status(cfg)

    if getattr(args, "json", False):
        s_dirs = [str(d) for d in get_all_skill_dirs(cfg)]
        print(json.dumps({"config": {"base": base_path(cfg), "categories": cfg["categories"],
                                     "default_category": cfg["default_category"],
                                     "machine": cfg["machine"], "skills_dirs": s_dirs},
                          "skills": st}, indent=2, ensure_ascii=False))
        return 0

    print(f"Remote: {base_path(cfg)}    machine: {cfg['machine']}")
    print(f"Subscribed categories: {', '.join(cfg['categories']) or '(none)'}\n")
    if not st:
        print("No skills found locally or on the remote.")
        return 0
    # Grouped by category, not one flat alphabetical list: the category is what decides
    # which machines pull a skill, so it is the axis worth reading down.
    w = max(len(n) for n in st)
    by_cat = {}
    for name, i in st.items():
        for cat in i.get("categories") or [i["category"] or NO_CATEGORY]:
            by_cat.setdefault(cat, []).append(name)
    for cat in sorted(by_cat):
        print(f"{cat} ({len(by_cat[cat])})")
        print("-" * (w + 12))
        for name in sorted(by_cat[cat]):
            print(f"  {name.ljust(w)}  {st[name]['state']}")
        print()

    uncategorised = [n for n, i in st.items() if i["local"] and not i["category"]]
    conflicts = [n for n, i in st.items() if i["state"] == CONFLICT]
    pending = [n for n, i in st.items() if i["state"] in (ONLY_LOCAL, LOCAL_NEW)]
    available = [n for n, i in st.items() if i["state"] in (ONLY_REMOTE, REMOTE_NEW)]
    print()
    if uncategorised:
        print(f"Needs a category ({len(uncategorised)}): {', '.join(uncategorised)}")
        print("  -> python sync.py categorize <skill> <category>")
    if pending:
        print(f"To upload ({len(pending)}): {', '.join(pending)}   -> python sync.py push")
    if available:
        print(f"Available on remote ({len(available)}): {', '.join(available)}   "
              f"-> python sync.py pull")
    if conflicts:
        print(f"CONFLICTS ({len(conflicts)}): {', '.join(conflicts)}")
        print("  -> python sync.py resolve <skill> --keep local|remote")
    if not (uncategorised or pending or available or conflicts):
        print("Everything is in sync.")
    return 0


def cmd_push(args):
    cfg = require_config()
    git_sync_in(cfg, quiet=getattr(args, "json", False))
    with Lock():
        manifest = read_manifest(cfg)
        st = compute_status(cfg, manifest)
        targets = syncable(args.skills or [n for n, i in st.items() if i["local"]])

        to_push, skipped, conflicts, uncategorised = [], [], [], []
        if args.skills and any(is_self(n) for n in args.skills):
            skipped.append((SELF_NAME, f"managed from {REPO_URL}, not through the remote "
                                       f"(reinstall it to update)"))
        for name in targets:
            i = st.get(name)
            if not i or not i["local"]:
                skipped.append((name, "not present locally"))
                continue
            if i["state"] == CONFLICT:
                conflicts.append(name)
                continue
            if i["state"] == REMOTE_NEW and not args.force:
                skipped.append((name, "remote is newer - use pull"))
                continue
            if i["state"] == IN_SYNC and not args.force:
                continue
            cats = list(i.get("categories") or [])
            category = i["category"] or (cats[0] if cats else None)
            if not category and args.assume_default:
                category = cfg["default_category"]
            if not category:
                uncategorised.append(name)
                continue
            if category not in cats:
                cats.insert(0, category)
            to_push.append((name, category, i, cats))

        if not getattr(args, "json", False):
            for name in uncategorised:
                print(f"no category: {name}  -> python sync.py categorize {name} <category>")
            for name in conflicts:
                print(f"CONFLICT: {name}  -> python sync.py resolve {name} --keep local|remote")
            for name, why in skipped:
                print(f"skipped {name}: {why}")

        if not to_push:
            if getattr(args, "json", False):
                print(json.dumps({"status": "nothing_to_upload", "uploaded": [], "conflicts": conflicts, "uncategorised": uncategorised, "skipped": skipped}))
            else:
                if not (conflicts or uncategorised):
                    print("Nothing to upload.")
            return 1 if (conflicts or uncategorised) else 0

        if not args.no_scan:
            blocked = []
            for name, _cat, i, _cats in to_push:
                sp = Path(i["local_path"]) if i.get("local_path") else (SKILLS_DIR / name)
                hits = scan_secrets(sp)
                if hits:
                    blocked.append((name, hits))
            if blocked:
                if getattr(args, "json", False):
                    print(json.dumps({"status": "blocked_credentials", "blocked": blocked}))
                else:
                    print("\nPossible credentials found - nothing was uploaded:")
                    for name, hits in blocked:
                        for rel, label, sample, lineno in hits:
                            print(f"  {name}/{rel}:{lineno}: {label} ({sample})")
                    print(f"Remove the secret, or mark that line with `{SECRET_ALLOW_PRAGMA}` "
                          f"in a comment if it is a false positive. `--no-scan` drops the "
                          f"check for the whole push.")
                return 1

        entries = {}
        done = []
        deferred = []
        deadline = getattr(args, "deadline", None)
        if deadline is None and getattr(args, "budget_seconds", None):
            deadline = time.monotonic() + args.budget_seconds
        json_out = getattr(args, "json", False)
        total = len(to_push)
        threads = getattr(args, "threads", 8) or 8
        lock_entries = threading.Lock()

        def _do_push_item(idx, item):
            name, category, i, cats = item
            sp = Path(i["local_path"]) if i.get("local_path") else (SKILLS_DIR / name)
            detail = f"{name} -> {category}/ ({i['files']} files, {human_size(i['size'])})"
            if not json_out:
                if i["size"] > BIG_SKILL_BYTES:
                    print(f"note: {name} is {human_size(i['size'])} - upload may be slow")
                if _interactive():
                    progress_bar(idx - 1, total, detail)
                else:
                    print(f"push [{idx}/{total}] {detail}")
            push_skill(cfg, name, category, dry_run=args.dry_run, skill_dir=sp)
            if not json_out and _interactive():
                progress_bar(idx, total, f"{name} done")
            if not args.dry_run:
                with lock_entries:
                    entries[name] = manifest_entry(cfg, category, i["local_hash"],
                                                   i["size"], i["files"], categories=cats)
                    done.append((name, category, i))

        try:
            if threads > 1 and len(to_push) > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
                    futures = [executor.submit(_do_push_item, idx, item) for idx, item in enumerate(to_push, 1)]
                    concurrent.futures.wait(futures)
            else:
                for n, (name, category, i, cats) in enumerate(to_push, 1):
                    if deadline and done and time.monotonic() > deadline:
                        deferred = [x[0] for x in to_push[n - 1:]]
                        break
                    _do_push_item(n, (name, category, i, cats))
        finally:
            # An upload can be interrupted, or killed by the Stop hook's timeout, after
            # files already landed on the remote. Record what did land, or the manifest
            # keeps claiming the old hash and the next run re-uploads everything.
            if entries:
                write_manifest(cfg, entries)
                for name, category, i in done:
                    record_synced(name, category, i["local_hash"], i["mtime"], i["files"])
                log(f"push {sorted(entries)}")

        if args.dry_run:
            if json_out:
                print(json.dumps({"status": "dry_run",
                                  "would_upload": [n for n, _c, _i, _cs in to_push]}))
            else:
                print(f"(dry-run) {total} skill(s) would be uploaded.")
            return 0

        if json_out:
            print(json.dumps({"status": "ok", "uploaded": [n for n, _, _ in done],
                              "deferred": deferred}))
        else:
            print(f"\nUploaded {len(entries)} skill(s) to {base_path(cfg)}:")
            for name, category, i in done:
                print(f"  {name}  ->  {category}/  "
                      f"({i['files']} files, {human_size(i['size'])})")
            if skipped:
                print(f"Skipped {len(skipped)}: "
                      f"{', '.join(n for n, _why in skipped[:6])}"
                      f"{' ...' if len(skipped) > 6 else ''}")
            if deferred:
                print(f"Ran out of time; {len(deferred)} left for next time: "
                      f"{', '.join(deferred[:5])}{' ...' if len(deferred) > 5 else ''}")
        if deferred:
            log(f"push deferred {deferred}")
        if entries:
            git_sync_out(cfg, f"push {', '.join(sorted(entries)[:6])}"
                              f"{' ...' if len(entries) > 6 else ''}")
        purge_backups(cfg)
        return 0


def cmd_pull(args):
    cfg = require_config()
    git_sync_in(cfg)
    with Lock():
        manifest = read_manifest(cfg)
        remote_skills = {n: e for n, e in manifest.get("skills", {}).items() if not is_self(n)}
        if not remote_skills:
            print(f"The remote {base_path(cfg)} has no skills yet. Run `push` on a machine "
                  f"that has them.")
            return 0

        if not args.categories and not args.skills:
            print("Categories on the remote:\n")
            by_cat = {}
            for n, e in remote_skills.items():
                for c in entry_categories(e) or [NO_CATEGORY]:
                    by_cat.setdefault(c, []).append(n)
            for line in category_tree_lines(by_cat):
                print(line)
            print("\nPull what you want on this machine:")
            print("    (a parent group brings down everything nested under it)")
            print("    python sync.py pull <category> [<category>...]")
            print("    python sync.py pull --skills <skill> [...]")
            return 2

        # --dest pins one folder (used by tests and for staging). Without it each skill
        # goes back where this machine already keeps it - writing everything into
        # SKILLS_DIR created a second copy of skills installed under another client.
        explicit_dest = Path(args.dest).expanduser() if args.dest else None
        if explicit_dest:
            explicit_dest.mkdir(parents=True, exist_ok=True)
        st = compute_status(cfg, manifest)

        wanted = []
        for n in (args.skills or []):
            if is_self(n):
                print(f"skipped {n}: managed from {REPO_URL} (reinstall it to update)")
            elif n not in remote_skills:
                print(f"skipped {n}: not on the remote")
            elif n not in wanted:
                wanted.append(n)
        for n, e in remote_skills.items():
            member_of = set(entry_categories(e)) or {NO_CATEGORY}
            if args.categories and n not in wanted and any(
                    category_matches(m, c) for m in member_of for c in args.categories):
                wanted.append(n)
        if args.categories:
            known = set()
            for e in remote_skills.values():
                known |= set(entry_categories(e)) or {NO_CATEGORY}
            for c in args.categories:
                if not any(category_matches(m, c) for m in known):
                    print(f"note: no category named '{c}' on the remote")

        pulled, conflicts, skipped_in_sync = [], [], []
        total_wanted = len(wanted)
        threads = getattr(args, "threads", 8) or 8
        pull_tasks = []

        for n, name in enumerate(sorted(wanted), 1):
            i = st.get(name, {})
            category = primary_category(remote_skills[name], NO_CATEGORY)
            dest_root = explicit_dest or skill_dest_dir(name, cfg)
            into_skills_dir = explicit_dest is None
            if into_skills_dir and not args.force:
                if i.get("state") == CONFLICT:
                    conflicts.append(name)
                    continue
                if i.get("state") == IN_SYNC:
                    skipped_in_sync.append(name)
                    continue
                if i.get("state") == LOCAL_NEW:
                    print(f"skipped {name}: your local copy is newer (push it, or pull --force)")
                    continue
            dest_root.mkdir(parents=True, exist_ok=True)
            pull_tasks.append((n, name, category, dest_root))

        def _do_pull_item(idx, name, category, dest_root):
            if _interactive():
                progress_bar(idx - 1, total_wanted, f"{category}/{name} -> {dest_root}")
            else:
                print(f"pull [{idx}/{total_wanted}] {category}/{name} -> {dest_root}")
            try:
                pull_skill(cfg, name, category, dest_root, dry_run=args.dry_run)
                if _interactive():
                    progress_bar(idx, total_wanted, f"{name} done")
                return (name, category, dest_root)
            except Exception as e:
                print(f"skipped {name}: {e}")
                return None

        if threads > 1 and len(pull_tasks) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
                futures = [executor.submit(_do_pull_item, idx, name, cat, dest) for idx, name, cat, dest in pull_tasks]
                for fut in concurrent.futures.as_completed(futures):
                    res = fut.result()
                    if res is not None:
                        pulled.append(res)
        else:
            for idx, name, cat, dest in pull_tasks:
                res = _do_pull_item(idx, name, cat, dest)
                if res is not None:
                    pulled.append(res)

        for name in conflicts:
            print(f"CONFLICT: {name}  -> python sync.py resolve {name} --keep local|remote")

        if args.dry_run:
            print(f"(dry-run) {len(pulled)} skill(s) would be downloaded")
            return 0

        if explicit_dest is None:
            for name, category, dest_root in pulled:
                fp, mtime, files, _size = fingerprint(dest_root / name)
                record_synced(name, category, fp, mtime, files)
            if args.categories:
                cfg["categories"] = sorted(set(cfg.get("categories", [])) | set(args.categories))
                save_json(CONFIG_FILE, cfg)

        if pulled:
            print(f"\nDownloaded {len(pulled)} skill(s):")
            for name, category, dest_root in pulled:
                print(f"  {category}/{name}  ->  {dest_root / name}")
        else:
            print("\nNothing to download.")
        if skipped_in_sync:
            print(f"Already up to date ({len(skipped_in_sync)}): "
                  f"{', '.join(skipped_in_sync[:6])}"
                  f"{' ...' if len(skipped_in_sync) > 6 else ''}")
        if pulled and explicit_dest is None:
            print("Restart Claude Code so it discovers the new skills.")
        log(f"pull {[n for n, _, _ in pulled]}")
        purge_backups(cfg)
        return 0 if not conflicts else 1


def cmd_categorize(args):
    """Set, add to, or remove from a skill's groups.

    (The git-backed remote is pulled first so the manifest being edited is the current
    one, and pushed again at the end.)

    Groups are membership, not a location: adding `work` to a skill that is already in
    `personal` leaves it in both. Only the primary group - the first one - decides which
    folder physically holds the skill on the remote, so a plain add never moves data.
    """
    cfg = require_config()
    git_sync_in(cfg)
    name = args.skill
    asked = [normalise_category(c) for c in args.categories if c and c.strip()]
    if not asked:
        raise SyncError("give at least one group name")
    if name not in local_skills_map(cfg) and not args.force:
        raise SyncError(f"skill '{name}' not found in any skills dir (use --force for a "
                        f"remote-only skill)")

    manifest = read_manifest(cfg)
    entry = manifest["skills"].get(name)
    current = entry_categories(entry)
    if not current:
        current = list((load_state()["skills"].get(name) or {}).get("categories") or [])

    if args.add:
        new_cats = current + [c for c in asked if c not in current]
    elif args.remove:
        new_cats = [c for c in current if c not in asked]
    else:
        new_cats = list(dict.fromkeys(asked))

    if not new_cats:
        raise SyncError(f"that would leave {name} in no group at all; assign another one "
                        f"first, or use `prune` to remove it from the remote")

    old_primary = primary_category(entry)
    new_primary = new_cats[0]

    if entry:
        # Only a change of primary moves anything: membership alone is metadata.
        if old_primary and old_primary != new_primary:
            code, _o, _e = rclone(["lsf", rpath(cfg, old_primary, name), "--max-depth", "1"],
                                  check=False, timeout=90)
            if code == 0:
                with Spinner(f"moving {name}: {old_primary}/ -> {new_primary}/"):
                    rclone(["moveto", rpath(cfg, old_primary, name),
                            rpath(cfg, new_primary, name)], timeout=1800)
        entry["category"] = new_primary
        entry["categories"] = new_cats
        entry["updated_at"] = now_iso()
        write_manifest(cfg, {name: entry})

    st = load_state()
    record = st["skills"].setdefault(name, {})
    record["category"] = new_primary
    record["categories"] = new_cats
    save_json(STATE_FILE, st)

    missing = [c for c in new_cats if c not in cfg.get("categories", [])]
    if missing:
        cfg["categories"] = sorted(set(cfg.get("categories", [])) | set(missing))
        save_json(CONFIG_FILE, cfg)

    if sorted(new_cats) == sorted(current):
        print(f"{name} is already in: {', '.join(new_cats)}")
    else:
        print(f"{name} -> groups: {', '.join(new_cats)}"
              + (f"   (stored under {new_primary}/)" if len(new_cats) > 1 else ""))
    if entry:
        git_sync_out(cfg, f"categorize {name} -> {', '.join(new_cats)}")
    else:
        print(f"not uploaded yet: python sync.py push {name}")
    return 0


def cmd_place(args):
    cfg = load_config()
    lmap = local_skills_map(cfg)
    name = args.skill
    src = lmap.get(name)
    if not src:
        raise SyncError(f"skill '{name}' not found in any local skills dir")

    targets = []
    for c in args.clients:
        key = c.lower()
        if key not in CLIENT_DIRS:
            raise SyncError(f"unknown client '{c}' - known: {', '.join(sorted(set(CLIENT_DIRS)))} "
                            f"(or pass --dest <folder> for anything else)")
        targets.append((key, CLIENT_DIRS[key]))
    if args.dest:
        targets.append((args.dest, Path(args.dest).expanduser()))
    if not targets:
        raise SyncError("give at least one client (claude, gemini, agents, cursor, "
                        "antigravity, opencode) or --dest <folder>")

    placed = []
    use_symlink = getattr(args, "symlink", False)
    for label, dest_root in targets:
        result, dst = place_one(src, dest_root, name, force=args.force,
                               symlink=use_symlink, label=label)
        if result == "placed":
            placed.append((label, dst))

    if placed:
        print("Restart the target client(s) so they discover the skill.")
    log(f"place {name} -> {[l for l, _ in placed]}")
    return 0 if placed or not targets else 1


# --------------------------------------------------------------------- hooks

def changed_since_state():
    """Skills whose cheap signature differs from the last recorded sync."""
    st = load_state()
    changed = []
    lmap = {n: p for n, p in local_skills_map().items() if not is_self(n)}
    for name, skill_path in lmap.items():
        prev = st["skills"].get(name)
        mtime, count = quick_sig(skill_path)
        if not prev:
            changed.append(name)
        elif count != prev.get("count") or mtime > float(prev.get("mtime") or 0) + 0.001:
            changed.append(name)
    return changed


def console_python() -> str:
    """A console-attached interpreter, even when this process itself runs under pythonw.exe.

    The scheduled/hidden check runs under pythonw.exe on purpose (no flash on every poll).
    But pythonw.exe has no console: reusing sys.executable for the interactive confirm-new
    window opens a cmd window that can't show or read anything - it just sits there empty.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
        found = shutil.which("python") or shutil.which("python3")
        if found:
            return found
    return str(exe)


def spawn_confirm_window():
    """Pop a real, separate console window running `confirm-new` (best-effort).

    A Stop hook has no stdin the user can type into, and blocking Stop only reaches
    the user if they're mid-conversation. A detached window with a real y/n prompt
    works either way and doesn't depend on Claude relaying anything.
    """
    script = str(Path(__file__).resolve())
    py = console_python()
    try:
        if os.name == "nt":
            subprocess.Popen(
                ["cmd", "/c", "start", "skill-sync: new skills", "cmd", "/k",
                 py, script, "confirm-new"],
                close_fds=True,
            )
        else:
            for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
                path = shutil.which(term)
                if path:
                    subprocess.Popen([path, "-e", py, script, "confirm-new"],
                                      close_fds=True)
                    break
        return True
    except Exception as e:
        log(f"spawn_confirm_window failed: {e!r}")
        return False


def cmd_hook_stop(args):
    """Auto-push skills already tracked in sync state; ask before pushing brand-new ones.

    A skill with no entry in state.json has never been synced anywhere - silently
    uploading it the first time is exactly what "detect it and ask me" was meant to
    avoid. Those get surfaced once via a separate console window (see
    spawn_confirm_window/cmd_confirm_new) with a real y/n prompt, so the hook itself
    never decides on its own and never has to guess whether anyone is watching chat.
    Skills already known to state (an edit to something already synced) keep
    auto-pushing as before - that direction was never the complaint.
    """
    cfg = load_config()
    if not cfg or not rclone_bin(required=False):
        return 0
    if lock_is_live():
        return 0                                 # a real sync is running; it will cover this
    # Usage counting runs before the early return below: most sessions change no skill at
    # all, and those are exactly the sessions whose usage is worth recording.
    try:
        scan_usage(cfg, budget_bytes=USAGE_HOOK_BUDGET_BYTES)
    except Exception as e:                                    # never break a session
        log(f"usage scan skipped: {e!r}")
    changed = changed_since_state()
    if not changed:
        return 0

    st = load_state()
    already_notified = set(st.get("notified_new", []))
    brand_new = [n for n in changed if n not in st["skills"]]
    ask_now = [n for n in brand_new if n not in already_notified]
    updated = [n for n in changed if n not in brand_new]

    if updated:
        sizes = {n: (skill_path(n, cfg) and fingerprint_cached(skill_path(n, cfg))[3]) or 0
                 for n in updated}
        updated.sort(key=lambda n: sizes[n])
        # Closing a session must not hang on a slow link. Smallest first, within a budget;
        # whatever does not fit is named in the log and goes up next time.
        budget = float(cfg.get("hook_budget_seconds") or HOOK_BUDGET_SECONDS)
        ns = argparse.Namespace(skills=updated, dry_run=False, force=False, no_scan=False,
                                json=False, deadline=time.monotonic() + budget,
                                assume_default=bool(cfg.get("auto_default_category", True)))
        try:
            cmd_push(ns)
        except SyncError as e:
            print(f"[skill-sync] auto-upload skipped: {str(e).splitlines()[0]}", file=sys.stderr)
            log(f"hook-stop error: {e}")
        except Exception as e:                                   # never break the session
            log(f"hook-stop unexpected error: {e!r}")

    if ask_now:
        # Mark notified before the window even opens: the window is the source of
        # truth for what gets pushed from here on, this flag only stops the hook
        # from popping a new window on every single Stop event.
        st["notified_new"] = sorted(already_notified | set(ask_now))
        save_json(STATE_FILE, st)
        if not spawn_confirm_window():
            names = ", ".join(sorted(ask_now))
            print(
                f"[skill-sync] {len(ask_now)} new local skill(s) not yet synced anywhere: "
                f"{names}. Run `python \"{script_path()}\" confirm-new` to review and push them.",
                file=sys.stderr,
            )
    return 0


def script_path() -> str:
    return str(Path(__file__).resolve())


def skill_description(skill_dir: Path) -> str:
    """Best-effort one-line description pulled from SKILL.md frontmatter."""
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r"^---\s*$(.*?)^---\s*$", text, re.MULTILINE | re.DOTALL)
    body = m.group(1) if m else text[:400]
    m = re.search(r"^description:\s*(.*)$", body, re.MULTILINE)
    if not m:
        return ""
    value = m.group(1).strip()
    if value in (">", "|", ">-", "|-"):
        # Folded/literal block scalar: collect the indented lines that follow.
        lines = []
        for line in body.split(value, 1)[1].splitlines()[1:]:
            if not line.strip():
                break
            if not line.startswith((" ", "\t")):
                break
            lines.append(line.strip())
        value = " ".join(lines)
    return value.strip('"\' ').replace("  ", " ")[:220]


def cmd_confirm_new(args):
    """Interactive: list never-synced local skills and ask, one by one, to push or skip.

    Meant to run in its own console window (spawn_confirm_window does that), but works
    fine run directly too: `python sync.py confirm-new`.
    """
    cfg = require_config()
    st = load_state()
    lmap = {n: p for n, p in local_skills_map(cfg).items() if not is_self(n)}
    pending = sorted(n for n in lmap if n not in st["skills"])

    def pause(msg):
        try:
            input(msg)
        except EOFError:
            pass

    print("=== skill-sync: skills nuevas sin sincronizar ===\n")
    if not pending:
        print("No hay ninguna pendiente.")
        pause("\nPulsa Enter para cerrar...")
        return 0

    to_push = []
    for name in pending:
        desc = skill_description(lmap[name])
        print(f"- {name}")
        if desc:
            print(f"    {desc}")
        try:
            ans = input("  Subir esta skill? [s/N]: ").strip().lower()
        except EOFError:
            ans = ""
        if ans in ("s", "si", "sÃ­", "y", "yes"):
            to_push.append(name)
        print()

    st["notified_new"] = sorted(set(st.get("notified_new", [])) | set(pending))
    save_json(STATE_FILE, st)

    if not to_push:
        print("Nada seleccionado, no se sube nada.")
        pause("\nPulsa Enter para cerrar...")
        return 0

    ns = argparse.Namespace(skills=to_push, dry_run=False, force=False, no_scan=False,
                            json=False, deadline=None,
                            assume_default=bool(cfg.get("auto_default_category", True)))
    try:
        cmd_push(ns)
    except SyncError as e:
        print(f"\nError subiendo: {e}")
    pause("\nListo. Pulsa Enter para cerrar...")
    return 0


def remove_legacy_watcher():
    """Delete the login item that versions before 2.5.0 could install. Its script is gone,
    so a leftover entry would only fail at every login."""
    name = "skill-sync-watch"
    appdata = Path(os.environ.get("APPDATA") or (HOME / "AppData" / "Roaming"))
    for f in (appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{name}.vbs",
              HOME / "Library" / "LaunchAgents" / f"com.{name}.plist",
              HOME / ".config" / "systemd" / "user" / f"{name}.service",
              HOME / ".config" / "autostart" / f"{name}.desktop"):
        try:
            if f.is_file():
                f.unlink()
                log(f"removed legacy watcher entry {f}")
        except OSError as e:
            log(f"could not remove legacy watcher entry {f}: {e!r}")


def cmd_hook_session_start(args):
    """One-line notice when the remote has skills this machine does not."""
    remove_legacy_watcher()
    cfg = load_config()
    if not cfg or not rclone_bin(required=False):
        return 0
    st = load_state()
    if not args.force and time.time() - float(st.get("last_remote_check") or 0) < REMOTE_CHECK_INTERVAL:
        return 0
    try:
        git_sync_in(cfg, quiet=True)
        manifest = read_manifest(cfg)
    except Exception as e:
        log(f"hook-session-start skipped: {e!r}")
        return 0
    st = load_state()
    st["last_remote_check"] = time.time()
    save_json(STATE_FILE, st)

    subscribed = set(cfg.get("categories", []))
    local = set(local_skills(cfg))
    new, updated = [], []
    for name, e in manifest.get("skills", {}).items():
        if subscribed and not (set(entry_categories(e)) & subscribed):
            continue
        if name not in local:
            new.append(name)
            continue
        prev = st["skills"].get(name, {})
        if prev.get("hash") and e.get("hash") and prev["hash"] != e["hash"] \
                and e.get("machine") != cfg.get("machine"):
            updated.append(name)

    parts = []
    if new:
        parts.append(f"{len(new)} new ({', '.join(sorted(new)[:4])})")
    if updated:
        parts.append(f"{len(updated)} updated ({', '.join(sorted(updated)[:4])})")
    if parts:
        print(f"[skill-sync] remote has {' and '.join(parts)}. Run /skill-sync pull")
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name.replace(".exe", "") or "sync.py",
        description="Sync Claude Code skills across machines with rclone.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="configure remote, root folder and categories")
    s.add_argument("--remote", help="rclone remote, e.g. gdrive: or dropbox:, or a local path")
    s.add_argument("--git", help="git repository URL to keep the skills in, instead of a "
                                "cloud remote (it is cloned locally and pushed for you)")
    s.add_argument("--git-branch", dest="git_branch", default=None,
                   help="branch to use with --git (default: main)")
    s.add_argument("--root", default="ClaudeSkills", help="folder inside the remote")
    s.add_argument("--categories", help="comma separated, e.g. work,school,personal")
    s.add_argument("--default-category", dest="default_category")
    s.add_argument("--machine", help="name for this computer (default: hostname)")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("status", help="compare local skills with the remote")
    s.add_argument("--json", action="store_true", help="machine readable output")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("push", help="upload changed skills")
    s.add_argument("skills", nargs="*", help="limit to these skills")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true", help="upload even if unchanged")
    s.add_argument("--no-scan", action="store_true", help="skip the credential scan")
    s.add_argument("--assume-default", action="store_true",
                   help="use the default category for skills without one")
    s.add_argument("--json", action="store_true", help="machine readable output")
    s.add_argument("--budget", type=float, dest="budget_seconds",
                   help="stop starting new uploads after this many seconds; the rest go "
                        "on the next run")
    s.add_argument("--threads", type=int, default=8, help="number of parallel transfer threads (default: 8)")
    s.set_defaults(func=cmd_push, deadline=None)

    s = sub.add_parser("pull", help="download skills by category")
    s.add_argument("categories", nargs="*")
    s.add_argument("--skills", nargs="+", help="download specific skills by name")
    s.add_argument("--dest", help="alternative destination folder (for testing)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true", help="overwrite the local copy")
    s.add_argument("--threads", type=int, default=8, help="number of parallel transfer threads (default: 8)")
    s.set_defaults(func=cmd_pull)

    s = sub.add_parser("categorize", help="set, add or remove a skill's groups")
    s.add_argument("skill")
    s.add_argument("categories", nargs="+", metavar="category",
                   help="the skill's groups; the first one holds it on the remote")
    s.add_argument("--add", action="store_true", help="add to the groups it is already in")
    s.add_argument("--remove", action="store_true", help="remove these groups, keep the rest")
    s.add_argument("--force", action="store_true", help="allow a remote-only skill")
    s.set_defaults(func=cmd_categorize)

    s = sub.add_parser("place", help="copy a local skill into another AI client's skills folder")
    s.add_argument("skill")
    s.add_argument("clients", nargs="*",
                   help="claude, gemini, agents (cursor/antigravity/opencode share this dir)")
    s.add_argument("--dest", help="custom destination folder instead of/besides a client name")
    s.add_argument("--force", action="store_true", help="overwrite an existing copy at the destination")
    s.add_argument("--symlink", action="store_true", help="create a directory junction / symlink instead of copying files")
    s.set_defaults(func=cmd_place)

    s = sub.add_parser("pack", help="bundle skills and deploy them into a project or client")
    s.set_defaults(func=cmd_pack, _pack_parser=s)
    psub = s.add_subparsers(dest="pack_action", metavar="<action>")

    def _dest_flags(target, allow_link=True):
        target.add_argument("--project", help="deploy inside this project folder")
        target.add_argument("--here", action="store_true",
                            help="deploy in the current folder")
        target.add_argument("--client", help="claude (default), gemini, agents, cursor, "
                                             "antigravity, opencode")
        if allow_link:
            target.add_argument("--link", action="store_true",
                                help="junction/symlink to the master copy, not a copy")
            target.add_argument("--copy", action="store_true",
                                help="copy files (the default)")

    psub.add_parser("list", help="every pack and how many skills it carries")

    pp = psub.add_parser("show", help="the skills a pack resolves to")
    pp.add_argument("name")

    pp = psub.add_parser("create", help="define a new pack")
    pp.add_argument("name")
    pp.add_argument("--skills", nargs="+", default=[], help="skill names to include")
    pp.add_argument("--extends", nargs="+", default=[], help="packs to inherit from")
    pp.add_argument("--from-project", dest="from_project",
                   help="seed it with the skills already in this project")
    pp.add_argument("--from-usage", dest="from_usage", action="store_true",
                   help="seed it with the skills actually used in --from-project "
                        "(see `usage show`)")
    pp.add_argument("--top", type=int, default=8,
                   help="how many of the most used skills --from-usage takes")
    pp.add_argument("--group", help="label for grouping packs in `pack list`")
    pp.add_argument("--client", help="default client for this pack")
    pp.add_argument("--link", action="store_true", help="default to symlinks, not copies")
    pp.add_argument("--force", action="store_true", help="redefine an existing pack")

    pp = psub.add_parser("add", help="add skills to a pack")
    pp.add_argument("name")
    pp.add_argument("skills", nargs="+")

    pp = psub.add_parser("rm", help="take skills out of a pack")
    pp.add_argument("name")
    pp.add_argument("skills", nargs="+")

    pp = psub.add_parser("delete", help="delete a pack definition")
    pp.add_argument("name")
    pp.add_argument("--force", action="store_true", help="also unhook packs that extend it")

    pp = psub.add_parser("apply", help="deploy a pack into a project or a client")
    pp.add_argument("name")
    _dest_flags(pp)
    pp.add_argument("--force", action="store_true", help="overwrite what is already there")
    pp.add_argument("--no-pull", dest="no_pull", action="store_true",
                   help="do not download skills that are missing on this machine")
    pp.add_argument("--dry-run", dest="dry_run", action="store_true")

    pp = psub.add_parser("move", help="move a deployed pack from one client to another")
    pp.add_argument("name")
    pp.add_argument("--from", dest="from_client", required=True)
    pp.add_argument("--to", dest="to_client", required=True)
    pp.add_argument("--project")
    pp.add_argument("--here", action="store_true")
    pp.add_argument("--force", action="store_true")
    pp.add_argument("--dry-run", dest="dry_run", action="store_true")

    pp = psub.add_parser("remove", help="uninstall a deployed pack from a folder")
    pp.add_argument("name")
    _dest_flags(pp, allow_link=False)
    pp.add_argument("--dry-run", dest="dry_run", action="store_true")

    pp = psub.add_parser("where", help="which packs are deployed in a project")
    pp.add_argument("--project")

    psub.add_parser("publish", help="upload the pack definitions to the remote")

    pp = psub.add_parser("fetch", help="bring pack definitions down from the remote")
    pp.add_argument("--keep-local", dest="keep_local", action="store_true",
                   help="do not overwrite a pack that already exists here")

    s = sub.add_parser("usage", help="per-project skill usage, mined from the transcripts")
    s.set_defaults(func=cmd_usage)
    usub = s.add_subparsers(dest="usage_action", metavar="<action>")
    up = usub.add_parser("scan", help="record invocations appended since the last scan")
    up.add_argument("--full", action="store_true",
                    help="rebuild the counts from every transcript; history whose "
                         "transcript is gone is not recovered")
    up.add_argument("--budget-mb", dest="budget_mb", type=float, default=40,
                    help="stop after this many MB and leave the rest for next time")
    up = usub.add_parser("show", help="what has been recorded")
    up.add_argument("--project", help="only this project")
    up.add_argument("--top", type=int, default=10)

    s = sub.add_parser("resolve", help="resolve a conflict, keeping one side")
    s.add_argument("skill")
    s.add_argument("--keep", choices=["local", "remote"], required=True)
    s.set_defaults(func=cmd_resolve)

    s = sub.add_parser("merge", help="inspect diff and merge Markdown skill files")
    s.add_argument("skill")
    s.add_argument("--keep", choices=["local", "remote"], help="resolve after inspecting diff")
    s.add_argument("--json", action="store_true", help="output diff as JSON")
    s.set_defaults(func=cmd_merge)

    s = sub.add_parser("prune", help="delete remote skills that no longer exist locally")
    s.add_argument("--yes", action="store_true", help="actually delete")
    s.add_argument("--only", nargs="+", help="limit to these skills")
    s.set_defaults(func=cmd_prune)

    s = sub.add_parser("doctor", help="diagnose setup problems")
    s.add_argument("--json", action="store_true", help="machine readable output")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("hook-stop", help="internal: auto-push when a session ends")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_hook_stop)

    s = sub.add_parser("hook-session-start", help="internal: notify about remote updates")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_hook_session_start)

    s = sub.add_parser("confirm-new", help="interactively review and push never-synced skills")
    s.set_defaults(func=cmd_confirm_new)

    return p


def main():
    args = build_parser().parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return args.func(args) or 0
    except SyncError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
