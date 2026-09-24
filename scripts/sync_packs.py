"""Packs: named bundles of skills deployed into one project or client.

Part of skill-sync; `sync.py` is the entry point and re-exports these names.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from sync_core import (  # noqa: F401
    CONFIG_FILE,
    HOME,
    Lock,
    NO_CATEGORY,
    SyncError,
    TRASH_DIR,
    get_all_skill_dirs,
    load_json,
    load_packs,
    local_skills_map,
    log,
    now_iso,
    require_config,
    save_json,
    stamp,
)
from sync_scan import (  # noqa: F401
    is_self,
    primary_category,
)
from sync_remote import (  # noqa: F401
    base_path,
    git_sync_in,
    git_sync_out,
    pull_skill,
    read_manifest,
    write_manifest,
)
from sync_usage import (  # noqa: F401
    usage_for_project,
)


# claude-code-setup/claude-plugins-official/... clients share ".agents/skills" by
# convention (README lists Antigravity, Cursor and OpenCode as reading that folder).
CLIENT_DIRS = {
    "claude": HOME / ".claude" / "skills",
    "gemini": HOME / ".gemini" / "config" / "skills",
    "agents": HOME / ".agents" / "skills",
    "cursor": HOME / ".agents" / "skills",
    "antigravity": HOME / ".agents" / "skills",
    "opencode": HOME / ".agents" / "skills",
}


def place_one(src: Path, dest_root: Path, name: str, force=False, symlink=False,
              label="", quiet=False):
    """Copy or junction one skill folder into `dest_root`, backing up what it replaces.

    Returns (result, destination) where result is "placed", "same" or "exists".
    """
    dst = dest_root / name
    is_link = dst.is_symlink() or (os.name == "nt" and os.path.islink(dst))
    if dst.exists() and dst.resolve() == src.resolve():
        if not quiet:
            print(f"skip {label}: {name} is already there")
        return "same", dst
    dest_root.mkdir(parents=True, exist_ok=True)
    if dst.exists() or is_link:
        if not force:
            if not quiet:
                print(f"skip {label}: {name} already exists there (use --force to overwrite)")
            return "exists", dst
        backup = TRASH_DIR / stamp() / (label or "placed") / name
        backup.parent.mkdir(parents=True, exist_ok=True)
        if is_link or dst.is_file():
            try:
                dst.unlink()
            except OSError:
                if os.name == "nt" and dst.is_dir():
                    os.rmdir(dst)
        elif dst.is_dir():
            shutil.move(str(dst), str(backup))

    if symlink:
        if os.name == "nt":
            res = subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if res.returncode != 0:
                try:
                    os.symlink(src, dst, target_is_directory=True)
                except Exception as err:
                    raise SyncError(f"failed to create junction/symlink on Windows: {err}")
        else:
            os.symlink(src, dst, target_is_directory=True)
        if not quiet:
            print(f"placed (symlink) {name} -> {label} ({dst})")
    else:
        shutil.copytree(src, dst)
        if not quiet:
            print(f"placed {name} -> {label} ({dst})")
    return "placed", dst


# --------------------------------------------------------------------- packs

PACK_LOCKFILE = ".skill-pack.json"
PACK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DEFAULT_PACK_CLIENT = "claude"

# Where a client keeps skills *inside a project*, as opposed to CLIENT_DIRS which is the
# machine-wide folder. Cursor, Antigravity and OpenCode share `.agents/skills` here too.
CLIENT_PROJECT_DIRS = {
    "claude": Path(".claude") / "skills",
    "gemini": Path(".gemini") / "skills",
    "agents": Path(".agents") / "skills",
    "cursor": Path(".agents") / "skills",
    "antigravity": Path(".agents") / "skills",
    "opencode": Path(".agents") / "skills",
}


def save_packs(cfg, packs: dict) -> None:
    cfg["packs"] = packs
    save_json(CONFIG_FILE, cfg)


def valid_pack_name(raw: str) -> str:
    name = str(raw).strip()
    if not PACK_NAME_RE.match(name):
        raise SyncError(f"invalid pack name '{raw}': letters, digits, dot, dash and "
                        f"underscore only - a pack is a label, not a path")
    return name


def require_pack(packs, name):
    if name not in packs:
        known = ", ".join(sorted(packs)) or "none defined yet"
        raise SyncError(f"no pack called '{name}' (known: {known})")
    return packs[name]


def resolve_pack(packs, name, _chain=()) -> list:
    """Every skill in a pack, parents first, deduplicated, order preserved."""
    if name in _chain:
        raise SyncError("these packs extend each other in a circle: "
                        + " -> ".join(list(_chain) + [name]))
    pack = require_pack(packs, name)
    out = []
    for parent in pack.get("extends") or []:
        for s in resolve_pack(packs, parent, tuple(_chain) + (name,)):
            if s not in out:
                out.append(s)
    for s in pack.get("skills") or []:
        if s not in out:
            out.append(s)
    return [s for s in out if not is_self(s)]


def pack_dest(client, project=None) -> Path:
    """The folder a pack is deployed into: project root x client, or the client's own."""
    key = (client or DEFAULT_PACK_CLIENT).lower()
    if project:
        rel = CLIENT_PROJECT_DIRS.get(key)
        if rel is None:
            raise SyncError(f"unknown client '{client}' - known: "
                            f"{', '.join(sorted(CLIENT_PROJECT_DIRS))}")
        return (Path(project).expanduser().resolve() / rel)
    root = CLIENT_DIRS.get(key)
    if root is None:
        raise SyncError(f"unknown client '{client}' - known: "
                        f"{', '.join(sorted(set(CLIENT_DIRS)))}")
    return root


def scan_project_skills(project) -> list:
    """Skill names already sitting in a project, whichever client's folder holds them."""
    root = Path(project).expanduser().resolve()
    if not root.exists():
        raise SyncError(f"{root} does not exist")
    found, seen_dirs = [], set()
    candidates = [root / rel for rel in CLIENT_PROJECT_DIRS.values()] + [root]
    for d in candidates:
        if d in seen_dirs or not d.is_dir():
            continue
        seen_dirs.add(d)
        for child in sorted(d.iterdir()):
            if child.is_dir() and (child / "SKILL.md").exists() and child.name not in found:
                found.append(child.name)
    return [n for n in found if not is_self(n)]


def warn_if_scanned(dest: Path, cfg=None) -> None:
    """`.agents/skills` under the working directory is one of the folders push scans.

    A pack applied there looks like a pile of brand-new skills to `confirm-new`, which
    would then offer to upload second copies of skills that are already on the remote.
    """
    try:
        target = dest.resolve()
    except OSError:
        return
    for d in get_all_skill_dirs(cfg):
        try:
            if d.exists() and target == d.resolve():
                print(f"\nnote: {target} is a folder skill-sync scans for skills. If "
                      f"confirm-new offers to upload these copies, answer n.")
                return
        except OSError:
            continue


def read_lockfile(dest: Path) -> dict:
    data = load_json(dest / PACK_LOCKFILE, {}) or {}
    if not isinstance(data.get("packs"), dict):
        data["packs"] = {}
    return data


def write_lockfile(dest: Path, data: dict) -> None:
    if data["packs"]:
        dest.mkdir(parents=True, exist_ok=True)
        save_json(dest / PACK_LOCKFILE, data)
    else:
        try:
            (dest / PACK_LOCKFILE).unlink()
        except OSError:
            pass


def _pack_summary(packs, name, lmap) -> str:
    try:
        skills = resolve_pack(packs, name)
    except SyncError as e:
        return f"  {name:<18} {str(e)[:60]}"
    pack = packs[name]
    missing = [s for s in skills if s not in lmap]
    bits = [f"{len(skills)} skills"]
    if pack.get("extends"):
        bits.append("extends " + ", ".join(pack["extends"]))
    if missing:
        bits.append(f"{len(missing)} not on this machine")
    return f"  {name:<18} {'   '.join(bits)}"


def _pack_list(args, cfg, packs):
    if not packs:
        print("No packs defined yet. A pack is a named list of skills you deploy into a "
              "project or a client:\n")
        print("    python sync.py pack create web --skills web-builder impeccable dataviz")
        print("    python sync.py pack create acme --extends web --from-project C:\\dev\\acme")
        print("    python sync.py pack apply web --project C:\\dev\\newsite")
        return 2
    lmap = local_skills_map(cfg)
    by_group = {}
    for name, p in packs.items():
        by_group.setdefault(p.get("group") or "", []).append(name)
    for group in sorted(by_group):
        print(f"\n{group}/" if group else "")
        for name in sorted(by_group[group]):
            print(_pack_summary(packs, name, lmap))
    print("\n    python sync.py pack show <pack>")
    print("    python sync.py pack apply <pack> --project <folder> [--client claude]")
    return 0


def _pack_show(args, cfg, packs):
    name = args.name
    pack = require_pack(packs, name)
    skills = resolve_pack(packs, name)
    lmap = local_skills_map(cfg)
    own = set(pack.get("skills") or [])
    print(f"pack        {name}")
    if pack.get("group"):
        print(f"group       {pack['group']}")
    if pack.get("extends"):
        print(f"extends     {', '.join(pack['extends'])}")
    print(f"client      {pack.get('client') or DEFAULT_PACK_CLIENT}"
          f"   mode {pack.get('mode') or 'copy'}")
    print(f"skills      {len(skills)}\n")
    for s in skills:
        where = "here" if s in lmap else "missing locally"
        origin = "" if s in own else " (inherited)"
        print(f"  {s:<28} {where}{origin}")
    missing = [s for s in skills if s not in lmap]
    if missing:
        print(f"\n{len(missing)} skill(s) are not on this machine; `pack apply` downloads "
              f"them from the remote.")
    return 0


def _pack_create(args, cfg, packs):
    name = valid_pack_name(args.name)
    if name in packs and not args.force:
        raise SyncError(f"pack '{name}' already exists - use `pack add {name} <skill>` to "
                        f"extend it, or --force to redefine it from scratch")
    extends = [valid_pack_name(e) for e in (args.extends or [])]
    for parent in extends:
        require_pack(packs, parent)
    skills = [s for s in dict.fromkeys(args.skills or []) if not is_self(s)]
    if getattr(args, "from_usage", False):
        where = args.from_project or str(Path.cwd())
        top = int(getattr(args, "top", None) or 8)
        used = usage_for_project(where, cfg)[:top]
        if not used:
            raise SyncError(f"no recorded skill usage for {where}. Build the history with "
                            f"`usage scan --full`, and check `usage show` for what is there.")
        print(f"from usage in {where}:")
        for used_name, count in used:
            print(f"  {used_name:<28} used {count}x")
            if used_name not in skills and not is_self(used_name):
                skills.append(used_name)
    elif args.from_project:
        for s in scan_project_skills(args.from_project):
            if s not in skills:
                skills.append(s)
    if not skills and not extends:
        raise SyncError("a pack needs at least one skill, or an --extends parent")
    packs[name] = {
        "skills": skills,
        "extends": extends,
        "group": args.group,
        "client": (args.client or DEFAULT_PACK_CLIENT).lower(),
        "mode": "link" if args.link else "copy",
        "updated_at": now_iso(),
        "machine": cfg.get("machine"),
    }
    # A cycle only becomes visible once the pack is in the dict.
    try:
        resolved = resolve_pack(packs, name)
    except SyncError:
        packs.pop(name, None)
        raise
    save_packs(cfg, packs)
    lmap = local_skills_map(cfg)
    unknown = [s for s in resolved if s not in lmap]
    print(f"pack '{name}': {len(resolved)} skill(s)")
    for s in resolved:
        print(f"  {s}{'' if s in lmap else '   (not on this machine)'}")
    if unknown:
        print(f"\n{len(unknown)} of them will be downloaded from the remote on apply.")
    print(f"\n    python sync.py pack apply {name} --project <folder>")
    return 0


def _pack_edit(args, cfg, packs, adding: bool):
    name = args.name
    pack = require_pack(packs, name)
    asked = [s for s in dict.fromkeys(args.skills) if s.strip()]
    if not asked:
        raise SyncError("name at least one skill")
    current = list(pack.get("skills") or [])
    if adding:
        new = current + [s for s in asked if s not in current and not is_self(s)]
    else:
        new = [s for s in current if s not in asked]
        inherited = [s for s in asked if s in resolve_pack(packs, name) and s not in current]
        for s in inherited:
            print(f"note: {s} comes from a parent pack - remove it there, or from "
                  f"{name}'s --extends")
    if new == current:
        print(f"{name} is unchanged: {', '.join(current) or '(empty)'}")
        return 0
    if not new and not pack.get("extends"):
        raise SyncError(f"that would leave '{name}' empty; delete the pack instead")
    pack["skills"] = new
    pack["updated_at"] = now_iso()
    save_packs(cfg, packs)
    print(f"{name} -> {len(resolve_pack(packs, name))} skill(s): "
          f"{', '.join(resolve_pack(packs, name))}")
    return 0


def _pack_delete(args, cfg, packs):
    name = args.name
    require_pack(packs, name)
    children = [n for n, p in packs.items() if name in (p.get("extends") or [])]
    if children and not args.force:
        raise SyncError(f"'{name}' is extended by {', '.join(children)}; delete those "
                        f"first or pass --force to drop the inheritance")
    packs.pop(name, None)
    for child in children:
        packs[child]["extends"] = [e for e in packs[child]["extends"] if e != name]
    save_packs(cfg, packs)
    print(f"deleted pack '{name}'"
          + (f" and unhooked it from {', '.join(children)}" if children else ""))
    print("Anything already deployed into a project stays where it is "
          "(`pack remove` uninstalls that).")
    return 0


def _pack_target(args, packs, name):
    """(destination, client, project, link?) for the apply/move/remove family."""
    pack = packs.get(name) or {}
    project = str(Path.cwd()) if getattr(args, "here", False) else args.project
    client = (getattr(args, "client", None) or pack.get("client")
              or DEFAULT_PACK_CLIENT).lower()
    if getattr(args, "link", False):
        link = True
    elif getattr(args, "copy", False):
        link = False
    else:
        link = pack.get("mode") == "link"
    return pack_dest(client, project), client, project, link


def _pack_apply(args, cfg, packs):
    name = args.name
    require_pack(packs, name)
    skills = resolve_pack(packs, name)
    dest, client, project, link = _pack_target(args, packs, name)
    lmap = local_skills_map(cfg)
    missing_local = [s for s in skills if s not in lmap]

    manifest = None
    if missing_local and not args.no_pull:
        try:
            manifest = read_manifest(cfg)
        except SyncError as e:
            print(f"note: could not read the remote index ({e}); missing skills are skipped")

    print(f"pack '{name}' -> {dest}")
    print(f"  client {client}   mode {'symlink' if link else 'copy'}   "
          f"{len(skills)} skill(s)"
          + (f"   project {project}" if project else "   (machine-wide)"))
    if args.dry_run:
        for s in skills:
            src = "local" if s in lmap else ("remote" if manifest and s in
                                             manifest.get("skills", {}) else "NOT FOUND")
            print(f"  (dry-run) {s:<28} from {src}")
        return 0

    placed, skipped, downloaded, not_found = [], [], [], []
    for s in skills:
        src = lmap.get(s)
        if src:
            result, _dst = place_one(src, dest, s, force=args.force, symlink=link,
                                    label=client)
            (placed if result == "placed" else skipped).append(s)
            continue
        entry = (manifest or {}).get("skills", {}).get(s)
        if not entry:
            not_found.append(s)
            continue
        if (dest / s).exists() and not args.force:
            print(f"skip {client}: {s} already exists there (use --force to overwrite)")
            skipped.append(s)
            continue
        try:
            pull_skill(cfg, s, primary_category(entry, NO_CATEGORY), dest)
            downloaded.append(s)
            placed.append(s)
        except SyncError as e:
            print(f"skipped {s}: {e}")
            not_found.append(s)

    lock = read_lockfile(dest)
    lock["packs"][name] = {
        "skills": skills,
        "mode": "link" if link else "copy",
        "client": client,
        "applied_at": now_iso(),
        "machine": cfg.get("machine"),
    }
    write_lockfile(dest, lock)

    print(f"\n{len(placed)} placed"
          + (f" ({len(downloaded)} downloaded from the remote)" if downloaded else "")
          + (f", {len(skipped)} already there" if skipped else ""))
    if not_found:
        print(f"not found anywhere: {', '.join(not_found)}")
    if placed:
        print(f"Restart {client} so it discovers them.")
    warn_if_scanned(dest, cfg)
    log(f"pack apply {name} -> {dest}")
    return 0 if not not_found else 1


def _pack_move(args, cfg, packs):
    name = args.name
    require_pack(packs, name)
    skills = resolve_pack(packs, name)
    project = str(Path.cwd()) if getattr(args, "here", False) else args.project
    src_root = pack_dest(args.from_client, project)
    dst_root = pack_dest(args.to_client, project)
    if src_root.resolve() == dst_root.resolve():
        raise SyncError(f"'{args.from_client}' and '{args.to_client}' are the same folder "
                        f"({src_root}) - nothing to move")
    print(f"pack '{name}': {src_root}  ->  {dst_root}")
    if args.dry_run:
        for s in skills:
            print(f"  (dry-run) {s:<28} "
                  f"{'move' if (src_root / s).exists() else 'not in the source folder'}")
        return 0

    lmap = local_skills_map(cfg)
    moved, skipped, absent = [], [], []
    for s in skills:
        src = src_root / s
        if not src.exists():
            fallback = lmap.get(s)
            if fallback and not (dst_root / s).exists():
                result, _d = place_one(fallback, dst_root, s, force=args.force,
                                      symlink=False, label=args.to_client)
                (moved if result == "placed" else skipped).append(s)
            else:
                absent.append(s)
            continue
        result, _dst = place_one(src, dst_root, s, force=args.force, symlink=False,
                                label=args.to_client)
        if result != "placed":
            skipped.append(s)
            continue
        backup = TRASH_DIR / stamp() / args.from_client / s
        backup.parent.mkdir(parents=True, exist_ok=True)
        try:
            if src.is_symlink() or (os.name == "nt" and os.path.islink(src)):
                src.unlink()
            else:
                shutil.move(str(src), str(backup))
        except OSError as e:
            print(f"warning: copied {s} but could not remove {src}: {e}")
        moved.append(s)

    src_lock = read_lockfile(src_root)
    entry = src_lock["packs"].pop(name, None)
    write_lockfile(src_root, src_lock)
    dst_lock = read_lockfile(dst_root)
    dst_lock["packs"][name] = {
        "skills": skills, "mode": "copy", "client": args.to_client,
        "applied_at": now_iso(), "machine": cfg.get("machine"),
        "moved_from": args.from_client,
    }
    write_lockfile(dst_root, dst_lock)
    if entry is None:
        print(f"note: no lockfile entry for '{name}' in the source folder")

    print(f"\n{len(moved)} moved"
          + (f", {len(skipped)} left alone" if skipped else "")
          + (f", {len(absent)} were not there: {', '.join(absent)}" if absent else ""))
    print(f"Replaced/removed copies are recoverable under {TRASH_DIR}")
    if moved:
        print(f"Restart {args.to_client}.")
    log(f"pack move {name} {args.from_client} -> {args.to_client}")
    return 0


def _pack_remove(args, cfg, packs):
    name = args.name
    dest, client, project, _link = _pack_target(args, packs, name)
    lock = read_lockfile(dest)
    entry = lock["packs"].get(name)
    if entry:
        skills = list(entry.get("skills") or [])
    elif name in packs:
        skills = resolve_pack(packs, name)
        print(f"note: no lockfile entry in {dest}; using the pack's current definition")
    else:
        raise SyncError(f"'{name}' is neither deployed in {dest} nor a known pack")

    # Anything another still-applied pack needs stays put.
    keep = set()
    for other, oe in lock["packs"].items():
        if other != name:
            keep |= set(oe.get("skills") or [])
    removed, kept = [], []
    for s in skills:
        target = dest / s
        is_link = target.is_symlink() or (os.name == "nt" and os.path.islink(target))
        if not target.exists() and not is_link:
            continue
        if s in keep:
            kept.append(s)
            continue
        if args.dry_run:
            removed.append(s)
            continue
        backup = TRASH_DIR / stamp() / f"{name}-{client}" / s
        backup.parent.mkdir(parents=True, exist_ok=True)
        try:
            if is_link:
                target.unlink()
            else:
                shutil.move(str(target), str(backup))
            removed.append(s)
        except OSError as e:
            print(f"could not remove {target}: {e}")
    if args.dry_run:
        print(f"(dry-run) would remove {len(removed)} skill(s) from {dest}: "
              f"{', '.join(removed)}")
        return 0
    lock["packs"].pop(name, None)
    write_lockfile(dest, lock)
    print(f"removed {len(removed)} skill(s) from {dest}")
    if kept:
        print(f"kept (another applied pack needs them): {', '.join(kept)}")
    print(f"Backed up under {TRASH_DIR}")
    log(f"pack remove {name} from {dest}")
    return 0


def _pack_where(args, cfg, packs):
    project = args.project or str(Path.cwd())
    roots, seen = [], set()
    for client in sorted(CLIENT_PROJECT_DIRS):
        d = pack_dest(client, project)
        if d not in seen:
            seen.add(d)
            roots.append((client, d))
    found = False
    print(f"project {Path(project).expanduser().resolve()}\n")
    for client, d in roots:
        lock = read_lockfile(d)
        if not lock["packs"]:
            continue
        found = True
        print(f"  {d}")
        for pname, e in sorted(lock["packs"].items()):
            drift = ""
            if pname in packs:
                now = set(resolve_pack(packs, pname))
                then = set(e.get("skills") or [])
                if now - then:
                    drift = f"   {len(now - then)} added to the pack since - `pack apply`"
            print(f"    {pname:<16} {len(e.get('skills') or [])} skills   "
                  f"{e.get('mode')}   {str(e.get('applied_at'))[:10]}{drift}")
    if not found:
        print("  no pack has been applied here")
        return 2
    return 0


def _pack_publish(args, cfg, packs):
    if not packs:
        raise SyncError("no packs to publish")
    git_sync_in(cfg)
    with Lock():
        remote = read_manifest(cfg)
        merged = dict(remote.get("packs") or {})
        merged.update(packs)
        write_manifest(cfg, {}, packs=merged)
    git_sync_out(cfg, f"packs {', '.join(sorted(packs))}")
    print(f"published {len(packs)} pack(s) to {base_path(cfg)}: {', '.join(sorted(packs))}")
    return 0


def _pack_fetch(args, cfg, packs):
    git_sync_in(cfg)
    with Lock():
        remote = read_manifest(cfg)
    incoming = remote.get("packs") or {}
    if not incoming:
        print(f"{base_path(cfg)} has no packs yet - run `pack publish` on the machine "
              f"that defines them")
        return 2
    added, updated, kept = [], [], []
    for name, p in incoming.items():
        if name not in packs:
            packs[name] = p
            added.append(name)
        elif packs[name] == p:
            continue
        elif args.keep_local:
            kept.append(name)
        else:
            packs[name] = p
            updated.append(name)
    save_packs(cfg, packs)
    print(f"{len(added)} new, {len(updated)} updated"
          + (f", {len(kept)} local kept" if kept else ""))
    for label, names in (("new", added), ("updated", updated), ("kept local", kept)):
        if names:
            print(f"  {label}: {', '.join(sorted(names))}")
    return 0


PACK_ACTIONS = {
    "list": _pack_list,
    "show": _pack_show,
    "create": _pack_create,
    "add": lambda a, c, p: _pack_edit(a, c, p, adding=True),
    "rm": lambda a, c, p: _pack_edit(a, c, p, adding=False),
    "delete": _pack_delete,
    "apply": _pack_apply,
    "move": _pack_move,
    "remove": _pack_remove,
    "where": _pack_where,
    "publish": _pack_publish,
    "fetch": _pack_fetch,
}


def cmd_pack(args):
    action = getattr(args, "pack_action", None)
    if not action:
        parser = getattr(args, "_pack_parser", None)
        if parser:
            parser.print_help()
        return 2
    cfg = require_config()
    packs = load_packs(cfg)
    return PACK_ACTIONS[action](args, cfg, packs)
