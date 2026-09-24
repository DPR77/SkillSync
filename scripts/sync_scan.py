"""Local side: skill discovery, ignore rules, hashing, status and pre-push guards.

Part of skill-sync; `sync.py` is the entry point and re-exports these names.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

from sync_core import (  # noqa: F401
    CONFLICT,
    CONFLICTS_DIR,
    FPCACHE_FILE,
    IGNORE_DIRS,
    IGNORE_FILES,
    IN_SYNC,
    KEEP_TRASH_DAYS,
    LOCAL_NEW,
    ONLY_LOCAL,
    ONLY_REMOTE,
    PLACEHOLDER_RE,
    REMOTE_NEW,
    REMOTE_TRASH_SWEEP_INTERVAL,
    REMOTE_TRASH_SWEEP_MAX,
    SECRET_ALLOW_PRAGMA,
    SECRET_PATTERNS,
    SECRET_SCAN_EXT,
    SECRET_SCAN_MAX_BYTES,
    SELF_NAME,
    SKILLS_DIR,
    STATE_FILE,
    Spinner,
    SyncError,
    TRASH_DIR,
    VERSION_FILE,
    load_json,
    load_state,
    local_skills_map,
    log,
    now_iso,
    progress_bar,
    save_json,
    stamp,
)
from sync_remote import (  # noqa: F401
    rclone,
    read_manifest,
    rpath,
)


# ---------------------------------------------------------------- filtering

def skillignore_patterns(skill_dir: Path):
    f = skill_dir / ".skillignore"
    pats = []
    if f.exists():
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("!"):
                pats.append(line.rstrip("/"))
    return pats


def is_ignored(rel: str, patterns) -> bool:
    name = rel.rsplit("/", 1)[-1]
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat) or rel.startswith(pat + "/"):
            return True
    return False


def rclone_filter_file(skill_dir: Path):
    """Build one --exclude-from file combining defaults + the skill's .skillignore.

    Returned path must be deleted by the caller. Mixing --exclude with --filter-from
    is fragile in rclone, so everything goes through a single exclude file.
    """
    lines = [f"{d}/**" for d in sorted(IGNORE_DIRS)]
    lines += [f"**/{d}/**" for d in sorted(IGNORE_DIRS)]
    lines += sorted(IGNORE_FILES)
    for pat in skillignore_patterns(skill_dir):
        lines.append(pat)
        if "/" not in pat:
            lines.append(f"**/{pat}")
        lines.append(f"{pat}/**")
        lines.append(f"**/{pat}/**")
    fd, path = tempfile.mkstemp(prefix="skill-sync-filter-", suffix=".txt", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(dict.fromkeys(lines)) + "\n")
    return path


def iter_skill_files(skill_dir: Path):
    patterns = skillignore_patterns(skill_dir)
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS)
        rel_root = Path(root).relative_to(skill_dir)
        for fname in sorted(files):
            if fname in IGNORE_FILES:
                continue
            rel = (rel_root / fname).as_posix()
            rel = rel[2:] if rel.startswith("./") else rel
            if is_ignored(rel, patterns):
                continue
            yield rel, Path(root) / fname


def fingerprint(skill_dir: Path):
    """Content hash of a skill -> (sha256, max_mtime, file_count, total_bytes)."""
    h = hashlib.sha256()
    mtime_max, count, total = 0.0, 0, 0
    for rel, path in iter_skill_files(skill_dir):
        try:
            st = path.stat()
            data = path.read_bytes()
        except OSError:
            continue
        h.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
        mtime_max = max(mtime_max, st.st_mtime)
        count += 1
        total += st.st_size
    return h.hexdigest(), mtime_max, count, total


def quick_sig(skill_dir: Path):
    """Cheap signature (no file reads) used by the Stop hook."""
    mtime_max, count = 0.0, 0
    for _rel, path in iter_skill_files(skill_dir):
        try:
            mtime_max = max(mtime_max, path.stat().st_mtime)
        except OSError:
            continue
        count += 1
    return round(mtime_max, 3), count


_FP_CACHE = None


def fingerprint_cached(skill_dir: Path):
    """fingerprint(), skipping the full read when nothing changed.

    `status` re-hashed every byte of every skill on every call, and the menu calls it on
    each refresh. The cheap signature is the same one the Stop hook already trusts to
    decide whether a skill changed, so reusing it here costs no extra accuracy.
    """
    global _FP_CACHE
    if _FP_CACHE is None:
        _FP_CACHE = load_json(FPCACHE_FILE, {}) or {}
    key = str(skill_dir)
    mtime, count = quick_sig(skill_dir)
    hit = _FP_CACHE.get(key)
    if hit and hit.get("mtime") == mtime and hit.get("count") == count:
        return hit["hash"], mtime, count, hit["size"]
    fp, mtime, count, size = fingerprint(skill_dir)
    _FP_CACHE[key] = {"hash": fp, "mtime": mtime, "count": count, "size": size}
    return fp, mtime, count, size


def save_fp_cache():
    if _FP_CACHE is not None:
        try:
            save_json(FPCACHE_FILE, _FP_CACHE)
        except Exception:
            pass


def detect_native_origin(path: Path | str | None) -> tuple[str, str]:
    """Detect the native platform origin of a skill based on its file path."""
    if not path:
        return ("custom", "ðŸ‘¤ [Custom]")
    p_str = str(path).replace("\\", "/").lower()
    if "/.claude/plugins/marketplaces/" in p_str:
        return ("claude-code-plugin", "ðŸ’¬ [Claude Plugin]")
    if "/.claude/skills/" in p_str:
        return ("claude-code", "ðŸ’¬ [Claude Native]")
    if "/.gemini/" in p_str:
        return ("gemini", "ðŸ¤– [Gemini Native]")
    if "/.cursor/" in p_str:
        return ("cursor", "âš¡ [Cursor Native]")
    if "/.openclaw/" in p_str:
        return ("openclaw", "ðŸ¦… [OpenClaw Native]")
    if "/.codex/" in p_str:
        return ("codex", "ðŸ§  [Codex Native]")
    if "/.agents/" in p_str:
        return ("agents", "ðŸŒ [Global Agents]")
    return ("custom", "ðŸ‘¤ [Custom]")


def is_self(name: str) -> bool:
    return name == SELF_NAME


def syncable(names):
    """Drop skill-sync from anything that uploads or downloads."""
    return [n for n in names if not is_self(n)]


def local_skills(cfg=None):
    return sorted(local_skills_map(cfg).keys())


def skill_path(name, cfg=None):
    """Where a skill actually lives, or None.

    Skills are discovered across several clients' folders (~/.claude, ~/.gemini,
    .agents, plugin marketplaces), so anything that touches a skill on disk must ask
    here. Assuming SKILLS_DIR/<name> silently misses skills installed in another client
    and, on download, writes a second copy of one that already exists elsewhere.
    """
    return local_skills_map(cfg).get(name)


def skill_dest_dir(name, cfg=None, default=None):
    """Folder a downloaded skill belongs in: next to the copy already on this machine,
    otherwise the primary skills dir."""
    existing = skill_path(name, cfg)
    if existing is not None:
        return existing.parent
    return default or SKILLS_DIR


# -------------------------------------------------------------------- status

def classify(i) -> str:
    if not i["remote"]:
        return ONLY_LOCAL
    if not i["local"]:
        return ONLY_REMOTE
    lh, rh, sh = i["local_hash"], i["remote_hash"], i["synced_hash"]
    if lh == rh:
        return IN_SYNC
    if sh is None:
        return CONFLICT                       # both sides exist, differ, no known ancestor
    if sh == rh:
        return LOCAL_NEW
    if sh == lh:
        return REMOTE_NEW
    return CONFLICT                           # both moved since the last sync


def compute_status(cfg, manifest=None):
    state = load_state()
    manifest = read_manifest(cfg) if manifest is None else manifest
    remote_skills = manifest.get("skills", {})
    result = {}

    lmap = local_skills_map(cfg)
    scanned = 0
    for name, skill_dir in lmap.items():
        if is_self(name):
            continue
        # Hashing every skill takes a visible moment the first time, before the cache is
        # warm. Say what is happening rather than freezing on a blank screen.
        scanned += 1
        progress_bar(scanned, len(lmap), f"scanning {name}")
        fp, mtime, count, size = fingerprint_cached(skill_dir)
        prev = state["skills"].get(name, {})
        rem = remote_skills.get(name)
        cats = entry_categories(rem) or list(prev.get("categories") or [])
        if not cats and prev.get("category"):
            cats = [prev["category"]]
        origin, badge = detect_native_origin(skill_dir)
        info = {
            "name": name, "local": True, "local_path": str(skill_dir), "remote": bool(rem),
            "native_origin": origin, "origin_badge": badge,
            "category": (rem or {}).get("category") or prev.get("category"),
            "categories": cats,
            "local_hash": fp, "remote_hash": (rem or {}).get("hash"),
            "synced_hash": prev.get("hash"),
            "mtime": mtime, "files": count, "size": size,
            "remote_updated": (rem or {}).get("updated_at"),
            "remote_machine": (rem or {}).get("machine"),
        }
        info["state"] = classify(info)
        result[name] = info

    for name, rem in remote_skills.items():
        if is_self(name) or name in result:
            continue
        result[name] = {
            "name": name, "local": False, "local_path": None, "remote": True,
            "category": primary_category(rem), "categories": entry_categories(rem),
            "local_hash": None,
            "remote_hash": rem.get("hash"), "synced_hash": None,
            "mtime": 0, "files": rem.get("files", 0), "size": rem.get("size", 0),
            "remote_updated": rem.get("updated_at"), "remote_machine": rem.get("machine"),
            "state": ONLY_REMOTE,
        }
    save_fp_cache()
    return result


# -------------------------------------------------------------------- guards

def scan_secrets(skill_dir: Path):
    """Possible credentials as (relative_path, label, sample, line_number).

    Reports the line so the user can look at it instead of taking the tool's word for it,
    skips obvious documentation placeholders, and honours an inline pragma - otherwise one
    fake key in a README pushes people towards --no-scan, which disables the check
    entirely.
    """
    hits = []
    for rel, path in iter_skill_files(skill_dir):
        if path.suffix.lower() not in SECRET_SCAN_EXT and path.name != ".env":
            continue
        try:
            if path.stat().st_size > SECRET_SCAN_MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = None
        for lineno, line in enumerate(text.splitlines(), 1):
            if SECRET_ALLOW_PRAGMA in line:
                continue
            for label, rx in SECRET_PATTERNS:
                m = rx.search(line)
                if not m:
                    continue
                if PLACEHOLDER_RE.search(m.group(0)):
                    continue
                found = (rel, label, m.group(0)[:10] + "...", lineno)
                break
            if found:
                break
        if found:
            hits.append(found)
    return hits


def push_skill(cfg, name, category, dry_run=False, skill_dir=None):
    if skill_dir is None:
        lmap = local_skills_map(cfg)
        src = lmap.get(name) or (SKILLS_DIR / name)
    else:
        src = Path(skill_dir)
    dst = rpath(cfg, category, name)
    filt = rclone_filter_file(src)
    try:
        args = ["sync", str(src), dst, "--checksum", "--exclude-from", filt,
                "--backup-dir", rpath(cfg, ".trash", stamp(), name)]
        if dry_run:
            args.append("--dry-run")
        with Spinner(f"uploading {name} to {category}/"):
            rclone(args, timeout=1800)
    finally:
        try:
            os.unlink(filt)
        except OSError:
            pass
    return dst


def pull_skill(cfg, name, category, dest_root: Path, dry_run=False):
    src = rpath(cfg, category, name)
    dst = dest_root / name
    args = ["sync", src, str(dst), "--checksum",
            "--backup-dir", str(TRASH_DIR / stamp() / name)]
    if dry_run:
        args.append("--dry-run")
    with Spinner(f"downloading {name}"):
        code, out, err = rclone(args, timeout=1800, check=False)
        if code != 0:
            if "directory not found" in err.lower() or "directory not found" in out.lower():
                raise SyncError("directory not found on remote")
            raise SyncError(f"rclone failed ({code}): {err.strip() or out.strip()}")
    return dst


def stash_remote_copy(cfg, name, category):
    dest = CONFLICTS_DIR / f"{name}-remote-{stamp()}"
    dest.mkdir(parents=True, exist_ok=True)
    with Spinner(f"saving the remote copy of {name}"):
        rclone(["copy", rpath(cfg, category, name), str(dest), "--checksum"], timeout=1800)
    return dest


def record_synced(name, category, hash_value, mtime, files):
    st = load_state()
    st["skills"][name] = {"hash": hash_value, "category": category, "mtime": mtime,
                          "count": files, "synced_at": now_iso()}
    save_json(STATE_FILE, st)


def entry_categories(entry) -> list:
    """Every group a skill belongs to.

    A skill can sit in more than one group - caveman is reasonably both `work` and
    `personal`. Membership is the list; `category` remains the single group whose folder
    physically holds the skill on the remote, so nothing has to be stored twice.
    Entries written before this existed carry only `category`.
    """
    if not entry:
        return []
    cats = entry.get("categories")
    if isinstance(cats, list) and cats:
        return [c for c in cats if c]
    return [entry["category"]] if entry.get("category") else []


def primary_category(entry, fallback=None):
    """The group whose folder holds the skill on the remote."""
    if entry and entry.get("category"):
        return entry["category"]
    cats = entry_categories(entry)
    return cats[0] if cats else fallback


CATEGORY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")
MAX_CATEGORY_DEPTH = 4


def normalise_category(raw: str) -> str:
    """Accept a nested group name such as `work/acme`, one folder per segment.

    Each segment is validated on its own because the name is pasted straight into an
    rclone path: a `..` or a backslash slipping through would write outside the root.
    """
    parts = [p.strip() for p in str(raw).replace("\\", "/").split("/") if p.strip()]
    if not parts:
        raise SyncError("empty group name")
    for p in parts:
        if p in (".", "..") or not CATEGORY_SEGMENT_RE.match(p):
            raise SyncError(f"invalid group '{raw}': the part '{p}' has to start with a "
                            f"letter or digit and may only contain letters, digits, "
                            f"spaces, dots, dashes and underscores")
    if len(parts) > MAX_CATEGORY_DEPTH:
        raise SyncError(f"group '{raw}' is {len(parts)} levels deep, the limit is "
                        f"{MAX_CATEGORY_DEPTH}")
    return "/".join(parts)


def category_matches(member: str, wanted: str) -> bool:
    """`work` selects `work` and everything nested under it; `work/acme` only itself."""
    wanted = wanted.rstrip("/")
    return member == wanted or member.startswith(wanted + "/")


def category_tree_lines(by_cat: dict, indent="  ") -> list:
    """Nested groups printed as a tree instead of a flat list of slash-separated names."""
    lines = []
    for cat in sorted(by_cat):
        depth = cat.count("/")
        leaf = cat.rsplit("/", 1)[-1]
        names = sorted(by_cat[cat])
        lines.append(f"{indent}{'  ' * depth}{leaf} ({len(names)}): {', '.join(names)}")
    return lines


def manifest_entry(cfg, category, fp, size, files, categories=None, native_origin=None):
    cats = [c for c in (categories or [category]) if c]
    if category and category not in cats:
        cats.insert(0, category)
    entry = {"category": category, "categories": cats, "hash": fp, "size": size,
             "files": files, "updated_at": now_iso(), "machine": cfg["machine"]}
    if native_origin:
        entry["native_origin"] = native_origin
    return entry


STAMP_RE = re.compile(r"(\d{8}-\d{6})")


def _stamp_age_days(name: str):
    """Age of a backup folder, whose stamp sits at the start (trash) or the end
    (conflicts, named `<skill>-remote-<stamp>`). None when there is no stamp to read -
    those are left alone rather than guessed at."""
    m = STAMP_RE.search(name or "")
    if not m:
        return None
    try:
        return (datetime.now() - datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")).days
    except ValueError:
        return None


def purge_backups(cfg=None):
    """Delete replaced-file backups older than KEEP_TRASH_DAYS.

    Local cleanup is filesystem-cheap and runs every time. The remote side is not: each
    stale folder costs a listing and a recursive delete against the provider's API, and
    running that after every push made a one-file upload take minutes. So the remote sweep
    happens once a day at most, and only clears a few folders per run - they are not
    urgent, and the next sync picks up where this one stopped.
    """
    for root in (TRASH_DIR, CONFLICTS_DIR):
        if not root.exists():
            continue
        for entry in root.iterdir():
            age = _stamp_age_days(entry.name)
            if age is not None and age > KEEP_TRASH_DAYS:
                shutil.rmtree(entry, ignore_errors=True)
    if not cfg:
        return

    st = load_state()
    if time.time() - float(st.get("trash_swept_at") or 0) < REMOTE_TRASH_SWEEP_INTERVAL:
        return
    st = load_state()
    st["trash_swept_at"] = time.time()
    save_json(STATE_FILE, st)

    code, out, _e = rclone(["lsf", rpath(cfg, ".trash"), "--dirs-only"], check=False, timeout=60)
    if code != 0:
        return
    stale = [line.strip().strip("/") for line in out.splitlines()]
    stale = [n for n in stale
             if (_stamp_age_days(n) or 0) > KEEP_TRASH_DAYS]
    for name in sorted(stale)[:REMOTE_TRASH_SWEEP_MAX]:
        rclone(["purge", rpath(cfg, ".trash", name)], check=False, timeout=120)
    if len(stale) > REMOTE_TRASH_SWEEP_MAX:
        log(f"trash sweep: {len(stale) - REMOTE_TRASH_SWEEP_MAX} folders left for next time")


def trash_size_bytes():
    total = 0
    for root in (TRASH_DIR, CONFLICTS_DIR):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


# ------------------------------------------------------------------- updates

def local_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0"
    except OSError:
        pass
    try:  # pip-installed: no VERSION file next to the package, read the metadata
        from importlib.metadata import version as _pkg_version
        return _pkg_version("skill-sync")
    except Exception:
        return "0"
