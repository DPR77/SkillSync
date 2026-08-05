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
import fnmatch
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:  # keep non-ASCII output alive on Windows consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOME = Path.home()

def get_all_skill_dirs(cfg=None) -> list[Path]:
    """Find all skill directories on the PC (Claude Code, Gemini, workspace .agents,
    every skills/ folder nested under installed plugin marketplaces, and custom paths)."""
    dirs = []
    env_dirs = os.environ.get("CLAUDE_SKILLS_DIR")
    if env_dirs:
        for p in env_dirs.split(os.pathsep):
            if p.strip():
                path = Path(p.strip()).expanduser()
                if path not in dirs:
                    dirs.append(path)
        return dirs
    standard_roots = [
        HOME / ".claude" / "skills",
        HOME / ".gemini" / "config" / "skills",
        Path.cwd() / ".agents" / "skills",
        HOME / ".agents" / "skills",
    ]
    for sr in standard_roots:
        if sr.exists() and sr not in dirs:
            dirs.append(sr)
    plugins_marketplaces = HOME / ".claude" / "plugins" / "marketplaces"
    if plugins_marketplaces.exists():
        for skills_dir in sorted(plugins_marketplaces.glob("**/skills")):
            if not skills_dir.is_dir():
                continue
            if any(part in (".git", "node_modules") for part in skills_dir.parts):
                continue
            if skills_dir not in dirs:
                dirs.append(skills_dir)
    if cfg and "extra_skills_dirs" in cfg:
        for p in cfg.get("extra_skills_dirs", []):
            path = Path(p).expanduser()
            if path not in dirs:
                dirs.append(path)
    if not dirs:
        dirs.append(HOME / ".claude" / "skills")
    return dirs

def local_skills_map(cfg=None) -> dict[str, Path]:
    """Map skill_name -> Path for all skills discovered across the PC."""
    skills_map = {}
    for sdir in get_all_skill_dirs(cfg):
        if not sdir.exists():
            continue
        for d in sdir.iterdir():
            if d.is_dir() and (d / "SKILL.md").exists():
                if d.name not in skills_map:
                    skills_map[d.name] = d
    return skills_map

SKILLS_DIR = get_all_skill_dirs()[0]
STATE_DIR = Path(os.environ.get("SKILL_SYNC_HOME") or (HOME / ".claude" / "skill-sync")).expanduser()
CONFIG_FILE = STATE_DIR / "config.json"
STATE_FILE = STATE_DIR / "state.json"
CONFLICTS_DIR = STATE_DIR / "conflicts"
TRASH_DIR = STATE_DIR / "trash"
LOG_FILE = STATE_DIR / "sync.log"
LOCK_FILE = STATE_DIR / "sync.lock"

MANIFEST_NAME = "manifest.json"
NO_CATEGORY = "uncategorised"
REMOTE_CHECK_INTERVAL = 6 * 3600   # hook-session-start: at most one remote check per 6h
LOCK_STALE_SECONDS = 15 * 60
BIG_SKILL_BYTES = 20 * 1024 * 1024

IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
               ".pytest_cache", ".ruff_cache", ".idea", ".vscode"}
IGNORE_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}

SECRET_PATTERNS = [
    ("OpenAI key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("inline credential", re.compile(
        r"(?i)\b(api[_-]?key|secret|passwd|password|access[_-]?token)\s*[=:]\s*[\"'][^\"'\s]{16,}[\"']")),
]
SECRET_SCAN_EXT = {".py", ".js", ".mjs", ".ts", ".sh", ".bash", ".zsh", ".ps1", ".md", ".json",
                   ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".txt", ".env", ".xml"}
SECRET_SCAN_MAX_BYTES = 512 * 1024

# skill states
IN_SYNC = "in-sync"
LOCAL_NEW = "local-newer"
REMOTE_NEW = "remote-newer"
ONLY_LOCAL = "local-only"
ONLY_REMOTE = "remote-only"
CONFLICT = "conflict"


# --------------------------------------------------------------------- utils

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def log(msg: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} {msg}\n")
    except Exception:
        pass


class SyncError(Exception):
    """Recoverable failure: reported to the user, never a traceback."""


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_config():
    return load_json(CONFIG_FILE, None)


def require_config():
    cfg = load_config()
    if not cfg:
        raise SyncError(
            "skill-sync is not configured yet.\n"
            "  1. rclone config                 (create a remote: Drive, Dropbox, ...)\n"
            "  2. python sync.py setup --remote <remote:> --categories work,school,personal")
    cfg.setdefault("categories", [])
    cfg.setdefault("default_category", cfg["categories"][0] if cfg["categories"] else "personal")
    cfg.setdefault("machine", machine_name())
    return cfg


def load_state() -> dict:
    st = load_json(STATE_FILE, {})
    st.setdefault("skills", {})
    return st


def machine_name(cfg=None) -> str:
    if cfg and cfg.get("machine"):
        return cfg["machine"]
    try:
        return socket.gethostname() or "unknown"
    except Exception:
        return "unknown"


def human_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


class Lock:
    """Best-effort cross-process lock so a Stop hook never races a manual sync."""

    def __init__(self, quiet=False):
        self.quiet = quiet
        self.acquired = False

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if LOCK_FILE.exists():
            info = load_json(LOCK_FILE, {})
            age = time.time() - float(info.get("time") or 0)
            if age < LOCK_STALE_SECONDS:
                raise SyncError(f"another skill-sync run is in progress (pid {info.get('pid')}). "
                                f"Delete {LOCK_FILE} if that is wrong.")
            log(f"stale lock removed (age {age:.0f}s)")
        save_json(LOCK_FILE, {"pid": os.getpid(), "time": time.time()})
        self.acquired = True
        return self

    def __exit__(self, *exc):
        if self.acquired:
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass
        return False


# -------------------------------------------------------------------- rclone

def rclone_bin(required=True):
    exe = shutil.which("rclone")
    if not exe and required:
        raise SyncError(
            "rclone was not found on PATH. Install it, then run `rclone config`:\n"
            "  Windows: winget install Rclone.Rclone\n"
            "  macOS:   brew install rclone\n"
            "  Linux:   sudo -v ; curl https://rclone.org/install.sh | sudo bash")
    return exe


def rclone(args, check=True, stdin_data=None, timeout=300):
    cmd = [rclone_bin()] + list(args)
    log("rclone " + " ".join(str(a) for a in args))
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_data.encode("utf-8") if stdin_data is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SyncError(f"rclone timed out after {timeout}s: {' '.join(str(a) for a in args[:3])}")
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    if check and proc.returncode != 0:
        raise SyncError(f"rclone failed ({proc.returncode}): "
                        f"{' '.join(str(a) for a in args[:4])}\n{err.strip()[:600]}")
    return proc.returncode, out, err


def normalise_remote(remote: str) -> str:
    """Accept `gdrive`, `gdrive:`, `gdrive:sub/dir`, or a plain local path."""
    remote = remote.strip().rstrip("/\\")
    if re.match(r"^[A-Za-z]:[\\/]", remote) or remote.startswith(("/", "~", ".", "\\\\")):
        return str(Path(remote).expanduser())          # local filesystem "remote"
    return remote if ":" in remote else remote + ":"


def base_path(cfg) -> str:
    remote = normalise_remote(cfg["remote"])
    root = (cfg.get("root") or "").strip("/\\")
    if not root:
        return remote
    sep = "" if remote.endswith(":") else "/"
    return f"{remote}{sep}{root}"


def rpath(cfg, *parts) -> str:
    tail = [str(p).strip("/") for p in parts if p]
    base = base_path(cfg)
    if not tail:
        return base
    sep = "" if base.endswith(":") else "/"
    return base + sep + "/".join(tail)


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


def local_skills(cfg=None):
    return sorted(local_skills_map(cfg).keys())


# ------------------------------------------------------------------ manifest

def read_manifest(cfg):
    code, out, err = rclone(["cat", rpath(cfg, MANIFEST_NAME)], check=False, timeout=90)
    if code != 0 or not out.strip():
        return {"version": 1, "skills": {}}
    try:
        m = json.loads(out)
    except json.JSONDecodeError:
        raise SyncError(f"remote {MANIFEST_NAME} is corrupt at {rpath(cfg, MANIFEST_NAME)}; "
                        f"fix or delete it before syncing")
    m.setdefault("skills", {})
    return m


def write_manifest(cfg, entries: dict, drop=()):
    """Re-read then merge per skill entry, so a concurrent machine is not clobbered."""
    remote = read_manifest(cfg)
    for name in drop:
        remote["skills"].pop(name, None)
    for name, entry in entries.items():
        remote["skills"][name] = entry
    remote["version"] = 1
    remote["updated_at"] = now_iso()
    rclone(["rcat", rpath(cfg, MANIFEST_NAME)],
           stdin_data=json.dumps(remote, indent=2, ensure_ascii=False), timeout=120)
    return remote


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
    for name, skill_path in lmap.items():
        fp, mtime, count, size = fingerprint(skill_path)
        prev = state["skills"].get(name, {})
        rem = remote_skills.get(name)
        info = {
            "name": name, "local": True, "local_path": str(skill_path), "remote": bool(rem),
            "category": (rem or {}).get("category") or prev.get("category"),
            "local_hash": fp, "remote_hash": (rem or {}).get("hash"),
            "synced_hash": prev.get("hash"),
            "mtime": mtime, "files": count, "size": size,
            "remote_updated": (rem or {}).get("updated_at"),
            "remote_machine": (rem or {}).get("machine"),
        }
        info["state"] = classify(info)
        result[name] = info

    for name, rem in remote_skills.items():
        if name in result:
            continue
        result[name] = {
            "name": name, "local": False, "local_path": None, "remote": True,
            "category": rem.get("category"), "local_hash": None,
            "remote_hash": rem.get("hash"), "synced_hash": None,
            "mtime": 0, "files": rem.get("files", 0), "size": rem.get("size", 0),
            "remote_updated": rem.get("updated_at"), "remote_machine": rem.get("machine"),
            "state": ONLY_REMOTE,
        }
    return result


# -------------------------------------------------------------------- guards

def scan_secrets(skill_dir: Path):
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
        for label, rx in SECRET_PATTERNS:
            m = rx.search(text)
            if m:
                hits.append((rel, label, m.group(0)[:10] + "..."))
                break
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
    rclone(args, timeout=1800)
    return dst


def stash_remote_copy(cfg, name, category):
    dest = CONFLICTS_DIR / f"{name}-remote-{stamp()}"
    dest.mkdir(parents=True, exist_ok=True)
    rclone(["copy", rpath(cfg, category, name), str(dest), "--checksum"], timeout=1800)
    return dest


def record_synced(name, category, hash_value, mtime, files):
    st = load_state()
    st["skills"][name] = {"hash": hash_value, "category": category, "mtime": mtime,
                          "count": files, "synced_at": now_iso()}
    save_json(STATE_FILE, st)


def manifest_entry(cfg, category, fp, size, files):
    return {"category": category, "hash": fp, "size": size, "files": files,
            "updated_at": now_iso(), "machine": cfg["machine"]}


# ------------------------------------------------------------------ commands

def cmd_setup(args):
    rclone_bin()
    _c, out, _e = rclone(["listremotes"], check=False, timeout=60)
    remotes = [r.strip() for r in out.splitlines() if r.strip()]

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

    cats = [c.strip() for c in (args.categories or "personal").split(",") if c.strip()]
    cfg = {
        "remote": remote,
        "root": (args.root if args.root is not None else "ClaudeSkills").strip("/\\"),
        "categories": cats,
        "default_category": args.default_category or cats[0],
        "machine": args.machine or machine_name(),
        "created_at": now_iso(),
    }
    save_json(CONFIG_FILE, cfg)
    if not STATE_FILE.exists():
        save_json(STATE_FILE, {"skills": {}})

    code, _o, err = rclone(["lsf", base_path(cfg), "--max-depth", "1"], check=False, timeout=90)
    print(f"Remote      : {base_path(cfg)}")
    print(f"Categories  : {', '.join(cats)}   (default: {cfg['default_category']})")
    print(f"Machine     : {cfg['machine']}")
    print(f"Skills dir  : {SKILLS_DIR}")
    if code != 0:
        print(f"\nNote: could not list the remote yet (it will be created on first push).")
        print("  " + err.strip().splitlines()[-1][:200] if err.strip() else "")
    print("\nNext: python sync.py status")
    return 0


def cmd_status(args):
    cfg = require_config()
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
    w = max(len(n) for n in st)
    print(f"{'SKILL'.ljust(w)}  {'CATEGORY'.ljust(14)}  STATE")
    print("-" * (w + 34))
    for name in sorted(st):
        i = st[name]
        print(f"{name.ljust(w)}  {(i['category'] or '-').ljust(14)}  {i['state']}")

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
    with Lock():
        manifest = read_manifest(cfg)
        st = compute_status(cfg, manifest)
        targets = args.skills or [n for n, i in st.items() if i["local"]]

        to_push, skipped, conflicts, uncategorised = [], [], [], []
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
            category = i["category"] or (cfg["default_category"] if args.assume_default else None)
            if not category:
                uncategorised.append(name)
                continue
            to_push.append((name, category, i))

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
            for name, _cat, i in to_push:
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
                        for rel, label, sample in hits:
                            print(f"  {name}/{rel}: {label} ({sample})")
                    print("Remove the secret, or re-run with --no-scan if it is a false positive.")
                return 1

        entries = {}
        uploaded_names = []
        for name, category, i in to_push:
            sp = Path(i["local_path"]) if i.get("local_path") else (SKILLS_DIR / name)
            if i["size"] > BIG_SKILL_BYTES and not getattr(args, "json", False):
                print(f"note: {name} is {human_size(i['size'])} - upload may be slow")
            if not getattr(args, "json", False):
                print(f"push {name} -> {category}/ ({i['files']} files, {human_size(i['size'])})")
            push_skill(cfg, name, category, dry_run=args.dry_run, skill_dir=sp)
            if not args.dry_run:
                entries[name] = manifest_entry(cfg, category, i["local_hash"], i["size"], i["files"])
                uploaded_names.append(name)

        if args.dry_run:
            if getattr(args, "json", False):
                print(json.dumps({"status": "dry_run", "would_upload": [n for n, _, _ in to_push]}))
            else:
                print(f"(dry-run) {len(to_push)} skill(s) would be uploaded.")
            return 0

        write_manifest(cfg, entries)
        for name, category, i in to_push:
            record_synced(name, category, i["local_hash"], i["mtime"], i["files"])

        if getattr(args, "json", False):
            print(json.dumps({"status": "ok", "uploaded": uploaded_names}))
        else:
            print(f"Uploaded {len(entries)} skill(s) to {base_path(cfg)}")
        log(f"push {sorted(entries)}")
        return 0


def cmd_pull(args):
    cfg = require_config()
    with Lock():
        manifest = read_manifest(cfg)
        remote_skills = manifest.get("skills", {})
        if not remote_skills:
            print(f"The remote {base_path(cfg)} has no skills yet. Run `push` on a machine "
                  f"that has them.")
            return 0

        if not args.categories and not args.skills:
            print("Categories on the remote:\n")
            by_cat = {}
            for n, e in remote_skills.items():
                by_cat.setdefault(e.get("category") or NO_CATEGORY, []).append(n)
            for cat in sorted(by_cat):
                print(f"  {cat} ({len(by_cat[cat])}): {', '.join(sorted(by_cat[cat]))}")
            print("\nPull what you want on this machine:")
            print("    python sync.py pull <category> [<category>...]")
            print("    python sync.py pull --skills <skill> [...]")
            return 2

        dest_root = Path(args.dest).expanduser() if args.dest else SKILLS_DIR
        dest_root.mkdir(parents=True, exist_ok=True)
        into_skills_dir = dest_root.resolve() == SKILLS_DIR.resolve()
        st = compute_status(cfg, manifest)

        wanted = []
        for n in (args.skills or []):
            if n not in remote_skills:
                print(f"skipped {n}: not on the remote")
            elif n not in wanted:
                wanted.append(n)
        for n, e in remote_skills.items():
            if args.categories and (e.get("category") or NO_CATEGORY) in args.categories \
                    and n not in wanted:
                wanted.append(n)
        if args.categories:
            unknown = set(args.categories) - {(e.get("category") or NO_CATEGORY)
                                              for e in remote_skills.values()}
            for c in sorted(unknown):
                print(f"note: no category named '{c}' on the remote")

        pulled, conflicts = [], []
        for name in sorted(wanted):
            i = st.get(name, {})
            category = remote_skills[name].get("category") or NO_CATEGORY
            if into_skills_dir and not args.force:
                if i.get("state") == CONFLICT:
                    conflicts.append(name)
                    continue
                if i.get("state") == IN_SYNC:
                    continue
                if i.get("state") == LOCAL_NEW:
                    print(f"skipped {name}: your local copy is newer (push it, or pull --force)")
                    continue
            print(f"pull {category}/{name}")
            pull_skill(cfg, name, category, dest_root, dry_run=args.dry_run)
            pulled.append((name, category))

        for name in conflicts:
            print(f"CONFLICT: {name}  -> python sync.py resolve {name} --keep local|remote")

        if args.dry_run:
            print(f"(dry-run) {len(pulled)} skill(s) would be written to {dest_root}")
            return 0

        if into_skills_dir:
            for name, category in pulled:
                fp, mtime, files, _size = fingerprint(SKILLS_DIR / name)
                record_synced(name, category, fp, mtime, files)
            if args.categories:
                cfg["categories"] = sorted(set(cfg.get("categories", [])) | set(args.categories))
                save_json(CONFIG_FILE, cfg)

        print(f"{len(pulled)} skill(s) written to {dest_root}")
        if pulled and into_skills_dir:
            print("Restart Claude Code so it discovers the new skills.")
        log(f"pull {[n for n, _ in pulled]} -> {dest_root}")
        return 0 if not conflicts else 1


def cmd_categorize(args):
    cfg = require_config()
    name, new_cat = args.skill, args.category.strip()
    if name not in local_skills_map(cfg) and not args.force:
        raise SyncError(f"skill '{name}' not found in any skills dir (use --force for a "
                        f"remote-only skill)")
    manifest = read_manifest(cfg)
    entry = manifest["skills"].get(name)
    old_cat = (entry or {}).get("category")

    if entry and old_cat and old_cat != new_cat:
        code, _o, _e = rclone(["lsf", rpath(cfg, old_cat, name), "--max-depth", "1"],
                              check=False, timeout=90)
        if code == 0:
            print(f"moving remote {old_cat}/{name} -> {new_cat}/{name}")
            rclone(["moveto", rpath(cfg, old_cat, name), rpath(cfg, new_cat, name)], timeout=1800)
        entry["category"] = new_cat
        entry["updated_at"] = now_iso()
        write_manifest(cfg, {name: entry})

    st = load_state()
    st["skills"].setdefault(name, {})["category"] = new_cat
    save_json(STATE_FILE, st)

    if new_cat not in cfg.get("categories", []):
        cfg["categories"] = sorted(cfg.get("categories", []) + [new_cat])
        save_json(CONFIG_FILE, cfg)

    print(f"{name} -> category '{new_cat}'")
    if not entry:
        print(f"not uploaded yet: python sync.py push {name}")
    return 0


def cmd_resolve(args):
    cfg = require_config()
    name = args.skill
    with Lock():
        manifest = read_manifest(cfg)
        entry = manifest["skills"].get(name)
        if not entry:
            raise SyncError(f"{name} is not on the remote - nothing to resolve")
        category = entry.get("category") or NO_CATEGORY

        if args.keep == "local":
            if not (SKILLS_DIR / name).exists():
                raise SyncError(f"{name} does not exist locally")
            backup = stash_remote_copy(cfg, name, category)
            print(f"remote version saved to: {backup}")
            fp, mtime, files, size = fingerprint(SKILLS_DIR / name)
            push_skill(cfg, name, category)
            write_manifest(cfg, {name: manifest_entry(cfg, category, fp, size, files)})
            record_synced(name, category, fp, mtime, files)
            print(f"resolved: LOCAL version of {name} is now on the remote")
        else:
            if (SKILLS_DIR / name).exists():
                backup = CONFLICTS_DIR / f"{name}-local-{stamp()}"
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(SKILLS_DIR / name, backup, dirs_exist_ok=True)
                print(f"local version saved to: {backup}")
            pull_skill(cfg, name, category, SKILLS_DIR)
            fp, mtime, files, _size = fingerprint(SKILLS_DIR / name)
            record_synced(name, category, fp, mtime, files)
            print(f"resolved: REMOTE version of {name} is now local")
        log(f"resolve {name} keep={args.keep}")
        return 0


def cmd_prune(args):
    cfg = require_config()
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
            category = manifest["skills"][n].get("category") or NO_CATEGORY
            print(f"deleting remote {category}/{n}")
            rclone(["purge", rpath(cfg, category, n)], check=False, timeout=900)
        write_manifest(cfg, {}, drop=targets)
        st = load_state()
        for n in targets:
            st["skills"].pop(n, None)
        save_json(STATE_FILE, st)
        print(f"Removed {len(targets)} skill(s) from the remote.")
        log(f"prune {targets}")
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
            "skills_dirs": [str(d) for d in s_dirs],
            "total_skills": len(local_skills(cfg)),
            "state_dir": str(STATE_DIR),
            "rclone": exe,
            "has_config": bool(cfg),
            "stop_hook": "hook-stop" in hooks,
            "session_start_hook": "hook-session-start" in hooks,
            "lock_exists": LOCK_FILE.exists()
        }, indent=2, ensure_ascii=False))
        return 0

    print(f"skills dirs    : {dirs_str} "
          f"({len(local_skills(cfg))} total skills)")
    print(f"state dir      : {STATE_DIR}")
    if exe:
        _c, out, _e = rclone(["version"], check=False, timeout=30)
        print(f"rclone         : {exe} ({out.splitlines()[0] if out else 'unknown'})")
    else:
        ok = False
        print("rclone         : NOT FOUND  -> winget install Rclone.Rclone | brew install rclone")

    if not cfg:
        ok = False
        print("config         : missing  -> python sync.py setup --remote <remote:>")
    else:
        print(f"config         : {CONFIG_FILE}")
        print(f"remote         : {base_path(cfg)}")
        print(f"categories     : {', '.join(cfg.get('categories', [])) or '(none)'}")
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
        print(f"lock           : present ({LOCK_FILE}) - delete it if no sync is running")
    print(f"log            : {LOG_FILE}")
    return 0 if ok else 1


import difflib

def cmd_merge(args):
    cfg = require_config()
    name = args.skill
    manifest = read_manifest(cfg)
    entry = manifest.get("skills", {}).get(name)
    if not entry:
        raise SyncError(f"skill '{name}' is not present on the remote")

    category = entry.get("category") or NO_CATEGORY
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


# --------------------------------------------------------------------- hooks

def changed_since_state():
    """Skills whose cheap signature differs from the last recorded sync."""
    st = load_state()
    changed = []
    lmap = local_skills_map()
    for name, skill_path in lmap.items():
        prev = st["skills"].get(name)
        mtime, count = quick_sig(skill_path)
        if not prev:
            changed.append(name)
        elif count != prev.get("count") or mtime > float(prev.get("mtime") or 0) + 0.001:
            changed.append(name)
    return changed


def cmd_hook_stop(args):
    """Auto-push on session end. Must be fast, silent and never break the session."""
    cfg = load_config()
    if not cfg or not shutil.which("rclone"):
        return 0
    if LOCK_FILE.exists():
        return 0
    changed = changed_since_state()
    if not changed:
        return 0
    ns = argparse.Namespace(skills=changed, dry_run=False, force=False, no_scan=False,
                            json=False, assume_default=bool(cfg.get("auto_default_category", True)))
    try:
        cmd_push(ns)
    except SyncError as e:
        print(f"[skill-sync] auto-upload skipped: {str(e).splitlines()[0]}", file=sys.stderr)
        log(f"hook-stop error: {e}")
    except Exception as e:                                   # never break the session
        log(f"hook-stop unexpected error: {e!r}")
    return 0


def cmd_hook_session_start(args):
    """One-line notice when the remote has skills this machine does not."""
    cfg = load_config()
    if not cfg or not shutil.which("rclone"):
        return 0
    st = load_state()
    if not args.force and time.time() - float(st.get("last_remote_check") or 0) < REMOTE_CHECK_INTERVAL:
        return 0
    try:
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
        if subscribed and e.get("category") not in subscribed:
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


# ---------------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(
        prog="sync.py",
        description="Sync Claude Code skills across machines with rclone.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="configure remote, root folder and categories")
    s.add_argument("--remote", help="rclone remote, e.g. gdrive: or dropbox:, or a local path")
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
    s.set_defaults(func=cmd_push)

    s = sub.add_parser("pull", help="download skills by category")
    s.add_argument("categories", nargs="*")
    s.add_argument("--skills", nargs="+", help="download specific skills by name")
    s.add_argument("--dest", help="alternative destination folder (for testing)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true", help="overwrite the local copy")
    s.set_defaults(func=cmd_pull)

    s = sub.add_parser("categorize", help="set or move a skill's category")
    s.add_argument("skill")
    s.add_argument("category")
    s.add_argument("--force", action="store_true", help="allow a remote-only skill")
    s.set_defaults(func=cmd_categorize)

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
