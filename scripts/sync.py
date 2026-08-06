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
import difflib
import fnmatch
import hashlib
import itertools
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
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
FPCACHE_FILE = STATE_DIR / "fpcache.json"

# skill-sync does not sync itself. It would be uploading the tool that is mid-upload, and
# a pull could replace the running code underneath it - which is exactly how it ended up
# permanently in conflict with its own remote copy. It updates from GitHub instead.
SELF_NAME = "skill-sync"
REPO = "DPR77/SkillSync"
REPO_URL = f"https://github.com/{REPO}"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/main/VERSION"
ARCHIVE_URL = f"{REPO_URL}/archive/refs/heads/main.zip"
UPDATE_CHECK_INTERVAL = 24 * 3600
VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"

MANIFEST_NAME = "manifest.json"        # legacy single-file manifest, migrated on first write
MANIFEST_DIR = "manifest.d"            # one <skill>.json per skill: no cross-machine clobber
MANIFEST_LEGACY_NAME = "manifest.legacy.json"
NO_CATEGORY = "uncategorised"
REMOTE_CHECK_INTERVAL = 6 * 3600   # hook-session-start: at most one remote check per 6h
LOCK_STALE_SECONDS = 15 * 60
BIG_SKILL_BYTES = 20 * 1024 * 1024
KEEP_TRASH_DAYS = 30               # backups older than this are deleted, local and remote
REMOTE_TRASH_SWEEP_INTERVAL = 24 * 3600   # the remote sweep is housekeeping, not urgent
REMOTE_TRASH_SWEEP_MAX = 5                # folders per sweep, so no sync waits on cleanup
HOOK_BUDGET_SECONDS = 90           # Stop hook uploads within this, the rest goes next time

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
# Opt-out for a genuine false positive, so nobody reaches for --no-scan (which drops the
# check for the whole push) because of one documented example line.
SECRET_ALLOW_PRAGMA = "skill-sync: allow-secret"
# Documentation is full of fake keys. Flagging those trains people to ignore the warning,
# which is worse than not warning at all.
PLACEHOLDER_RE = re.compile(
    r"(?i)example|dummy|changeme|placeholder|redacted|your[_-]?(api[_-]?)?key|<[^>]+>|xxxx|"
    r"abcdef|123456|0123456789")

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
    # The scratch name carries this process's pid. A fixed ".tmp" is shared by every
    # process writing the same file, and on Windows the rename then fails outright
    # because the other one still holds the handle.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def load_config():
    return load_json(CONFIG_FILE, None)


def require_config():
    cfg = load_config()
    if not cfg:
        # Name the file it looked for. An inherited SKILL_SYNC_HOME pointing somewhere
        # else looks exactly like "never configured", and there is no way to tell the two
        # apart without being told where it searched.
        override = os.environ.get("SKILL_SYNC_HOME")
        where = f"No config at {CONFIG_FILE}"
        if override:
            where += f"\n  (SKILL_SYNC_HOME is set to {override} - unset it to use the default)"
        raise SyncError(
            f"skill-sync is not configured yet.\n  {where}\n"
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


# ------------------------------------------------------------------ progress

def _interactive() -> bool:
    """Whether anyone is watching. Silent for hooks, pipes, --json and the test suite."""
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def progress_bar(done: int, total: int, label: str = "", width: int = 24) -> None:
    """One redrawn line for a multi-step transfer."""
    if not _interactive() or total <= 0:
        return
    filled = int(width * done / total)
    bar = "#" * filled + "." * (width - filled)
    line = f"  [{bar}] {done}/{total}  {label}"
    # shutil, not os/sys: it falls back to 80x24 instead of raising when there is no
    # console attached, which is exactly the case this runs in under a hook.
    columns = shutil.get_terminal_size((80, 24)).columns
    sys.stdout.write("\r\x1b[K" + line[:max(20, columns - 1)])
    if done >= total:
        sys.stdout.write("\n")
    sys.stdout.flush()


class Spinner:
    """Movement while a single rclone call runs.

    One skill is one rclone invocation whose output we capture, so a large upload showed
    nothing at all until it finished - indistinguishable from a hang. This ticks in a
    daemon thread and erases itself on the way out.
    """

    FRAMES = "|/-\\"

    def __init__(self, label: str):
        self.label = label
        self.enabled = _interactive()
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        if self.enabled:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self):
        start = time.monotonic()
        for i in itertools.count():
            if self._stop.wait(0.12):
                return
            sys.stdout.write(f"\r\x1b[K  {self.FRAMES[i % 4]} {self.label} "
                             f"({time.monotonic() - start:.0f}s)")
            sys.stdout.flush()

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()
        return False


def human_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def pid_alive(pid) -> bool:
    """Whether a process is still running, without signalling it.

    os.kill(pid, 0) is the usual trick, but on Windows os.kill does not implement signal
    0 - it calls TerminateProcess, so the "check" would kill the very process it asks
    about. Windows therefore goes through OpenProcess instead.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
        k = ctypes.windll.kernel32
        handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if k.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True                          # cannot tell: assume it is alive
        finally:
            k.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True                              # exists but not ours to probe
    return True


def lock_is_live() -> bool:
    """Whether a lock file represents a sync that is actually still running.

    A crashed run leaves the file behind. Treating that as "busy" forever silently
    disabled the Stop hook's auto-upload, so callers that only want to stay out of the
    way must ask this rather than testing for the file's existence.
    """
    if not LOCK_FILE.exists():
        return False
    info = load_json(LOCK_FILE, {}) or {}
    if time.time() - float(info.get("time") or 0) >= LOCK_STALE_SECONDS:
        return False
    pid = info.get("pid")
    if pid is not None and pid != os.getpid():
        return pid_alive(pid)
    return True


class Lock:
    """Best-effort cross-process lock so a Stop hook never races a manual sync."""

    def __init__(self, quiet=False):
        self.quiet = quiet
        self.acquired = False

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "time": time.time()})
        # O_CREAT|O_EXCL is the lock: the filesystem decides the winner in one atomic step.
        # Checking existence and then writing leaves a window where both processes think
        # they won, and on Windows the two writes collide on the rename instead.
        for attempt in (1, 2):
            try:
                fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                info = load_json(LOCK_FILE, {})
                if lock_is_live():
                    raise SyncError(
                        f"another skill-sync run is in progress (pid {info.get('pid')}). "
                        f"Delete {LOCK_FILE} if that is wrong.")
                age = time.time() - float(info.get("time") or 0)
                log(f"stale lock removed (age {age:.0f}s, pid {info.get('pid')} gone)")
                try:
                    LOCK_FILE.unlink()
                except OSError:
                    pass
                if attempt == 2:                 # someone else keeps winning the retry
                    raise SyncError(f"could not take the lock at {LOCK_FILE}; "
                                    f"delete it if no sync is running")
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
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
            "  Linux:   sudo apt install rclone | sudo dnf install rclone")
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


# ------------------------------------------------------------------ manifest

def read_legacy_manifest(cfg):
    """Entries from a manifest.legacy.json left by the manifest.d experiment."""
    code, out, _err = rclone(["cat", rpath(cfg, MANIFEST_LEGACY_NAME)], check=False, timeout=90)
    if code != 0 or not out.strip():
        return {}
    try:
        return json.loads(out).get("skills", {}) or {}
    except json.JSONDecodeError:
        return {}


def read_split_manifest(cfg):
    """Entries from a manifest.d/ directory, if a previous version left one behind."""
    tmp = Path(tempfile.mkdtemp(prefix="skill-sync-manifest-"))
    skills = {}
    try:
        code, _o, _e = rclone(["copy", rpath(cfg, MANIFEST_DIR), str(tmp), "--include", "*.json",
                               "--transfers", "24", "--checkers", "24"],
                              check=False, timeout=180)
        if code == 0:
            for f in sorted(tmp.glob("*.json")):
                try:
                    skills[f.stem] = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    log(f"ignoring corrupt manifest entry {f.name}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return skills


def read_manifest(cfg):
    """The whole index in one request.

    An earlier version split this into manifest.d/<skill>.json to stop two machines
    overwriting each other's entries. On an API-backed remote that turned every single
    command into one request per skill - 63 of them here, minutes of waiting - to avoid a
    race that needs two machines writing within the same few seconds. The aggregate file
    is read once and written with a read-merge-write, which is the right trade for this.
    manifest.d is still read when the aggregate is missing, so a remote left in the split
    layout keeps working and folds back on the next write.
    """
    with Spinner("reading the remote index"):
        code, out, _err = rclone(["cat", rpath(cfg, MANIFEST_NAME)], check=False, timeout=90)
        if code == 0 and out.strip():
            try:
                m = json.loads(out)
            except json.JSONDecodeError:
                raise SyncError(f"remote {MANIFEST_NAME} is corrupt at "
                                f"{rpath(cfg, MANIFEST_NAME)}; fix or delete it before syncing")
            m.setdefault("skills", {})
            return m
        # No aggregate: rebuild from whatever the older layouts left behind.
        skills = dict(read_legacy_manifest(cfg))
        skills.update(read_split_manifest(cfg))
    return {"version": 2, "skills": skills}


def write_manifest(cfg, entries: dict, drop=()):
    """Re-read, merge the changed entries, write the whole index back.

    The merge is what keeps a concurrent machine's entry alive: only the skills named in
    `entries` are replaced, everything else is carried over from whatever is on the remote
    right now rather than from the copy this process read minutes ago.
    """
    with Spinner("updating the remote index"):
        remote = read_manifest(cfg)
        for name in drop:
            remote["skills"].pop(name, None)
        remote["skills"].update(entries)
        remote["version"] = 2
        remote["updated_at"] = now_iso()
        rclone(["rcat", rpath(cfg, MANIFEST_NAME)],
               stdin_data=json.dumps(remote, indent=2, ensure_ascii=False), timeout=120)

        # Retire a split manifest.d once its contents are safely in the aggregate, so the
        # slow path is never taken again.
        code, _o, _e = rclone(["lsf", rpath(cfg, MANIFEST_DIR)], check=False, timeout=60)
        if code == 0:
            rclone(["purge", rpath(cfg, MANIFEST_DIR)], check=False, timeout=300)
            log(f"consolidated {MANIFEST_DIR}/ back into {MANIFEST_NAME}")
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
        info = {
            "name": name, "local": True, "local_path": str(skill_dir), "remote": bool(rem),
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


def manifest_entry(cfg, category, fp, size, files, categories=None):
    cats = [c for c in (categories or [category]) if c]
    if category and category not in cats:
        cats.insert(0, category)
    return {"category": category, "categories": cats, "hash": fp, "size": size,
            "files": files, "updated_at": now_iso(), "machine": cfg["machine"]}


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
        return "0"


def _version_key(v: str):
    """Compare 2.10.0 above 2.9.0, and never raise on something unparseable."""
    parts = []
    for chunk in re.split(r"[.\-+]", (v or "").strip().lstrip("vV")):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts + [0] * (4 - len(parts)))[:4]


def fetch_latest_version(timeout=4):
    """(version, reason). Version is None when it could not be read."""
    import urllib.error
    import urllib.request
    import base64
    import json

    # Try GitHub API first (zero CDN caching delay)
    api_url = f"https://api.github.com/repos/{REPO}/contents/VERSION"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "skill-sync", "Accept": "application/vnd.github.v3+json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
            content = data.get("content", "")
            text = base64.b64decode(content).decode("utf-8", "replace").strip()
            if text:
                return (text, None)
    except Exception:
        pass

    # Fallback to raw VERSION URL
    try:
        url = f"{VERSION_URL}?_={int(time.time())}"
        req = urllib.request.Request(url, headers={"User-Agent": "skill-sync", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace").strip()
        return (text, None) if text else (None, "the published VERSION file is empty")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, f"no VERSION file published at {VERSION_URL} yet"
        return None, f"GitHub answered {e.code}"
    except Exception as e:
        log(f"update check failed: {e!r}")
        return None, f"could not reach GitHub ({e.__class__.__name__})"


def update_available(force=False, timeout=4):
    """(latest, is_newer, reason) using a cached answer, checked at most once a day."""
    st = load_state()
    cached = st.get("update_check") or {}
    fresh = time.time() - float(cached.get("at") or 0) < UPDATE_CHECK_INTERVAL
    reason = None
    if not force and fresh and cached.get("latest"):
        latest = cached["latest"]
    else:
        latest, reason = fetch_latest_version(timeout=timeout)
        if latest:
            st = load_state()
            st["update_check"] = {"at": time.time(), "latest": latest}
            save_json(STATE_FILE, st)
        elif cached.get("latest"):
            latest = cached["latest"]                # stale, but better than nothing
    if not latest:
        return None, False, reason
    return latest, _version_key(latest) > _version_key(local_version()), reason


def cmd_update(args):
    """Replace this skill with the published version, keeping a backup."""
    import urllib.request
    import zipfile

    here = VERSION_FILE.parent
    latest, newer, reason = update_available(force=True, timeout=10)
    if latest is None:
        raise SyncError(f"cannot check for updates: {reason or 'unknown error'}")
    print(f"installed: {local_version()}    published: {latest}")
    if not newer and not args.force:
        print("Already up to date.")
        return 0
    if args.check:
        print(f"An update is available. Install it with: python sync.py update")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="skill-sync-update-"))
    try:
        archive = tmp / "main.zip"
        with Spinner(f"downloading {latest}"):
            req = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "skill-sync"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                archive.write_bytes(resp.read())
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(tmp)

        roots = [p for p in tmp.iterdir() if p.is_dir()]
        if len(roots) != 1:
            raise SyncError(f"unexpected archive layout from {ARCHIVE_URL}")
        source = roots[0]
        if not (source / "SKILL.md").exists():
            raise SyncError("the downloaded archive does not look like skill-sync")

        # Keep the whole current copy before writing over it: this is the one operation
        # that can break the tool doing the operating.
        backup = TRASH_DIR / f"{stamp()}-{SELF_NAME}-{local_version()}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(here, backup, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".git"))
        print(f"current version backed up to: {backup}")

        copied = 0
        for src in source.rglob("*"):
            if src.is_dir() or any(part in (".git", "__pycache__") for part in src.parts):
                continue
            dst = here / src.relative_to(source)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"Updated to {latest} ({copied} files).")
    print("Restart the menu, and Claude Code, so the new version is loaded.")
    log(f"updated {local_version()} -> {latest}")
    return 0


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
    with Lock():
        manifest = read_manifest(cfg)
        st = compute_status(cfg, manifest)
        targets = syncable(args.skills or [n for n, i in st.items() if i["local"]])

        to_push, skipped, conflicts, uncategorised = [], [], [], []
        if args.skills and any(is_self(n) for n in args.skills):
            skipped.append((SELF_NAME, f"managed from {REPO_URL}, not through the remote "
                                       f"(python sync.py update)"))
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
        purge_backups(cfg)
        return 0


def cmd_pull(args):
    cfg = require_config()
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
            for cat in sorted(by_cat):
                print(f"  {cat} ({len(by_cat[cat])}): {', '.join(sorted(by_cat[cat]))}")
            print("\nPull what you want on this machine:")
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
                print(f"skipped {n}: managed from {REPO_URL} (python sync.py update)")
            elif n not in remote_skills:
                print(f"skipped {n}: not on the remote")
            elif n not in wanted:
                wanted.append(n)
        for n, e in remote_skills.items():
            member_of = set(entry_categories(e)) or {NO_CATEGORY}
            if args.categories and member_of & set(args.categories) and n not in wanted:
                wanted.append(n)
        if args.categories:
            known = set()
            for e in remote_skills.values():
                known |= set(entry_categories(e)) or {NO_CATEGORY}
            unknown = set(args.categories) - known
            for c in sorted(unknown):
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

    Groups are membership, not a location: adding `work` to a skill that is already in
    `personal` leaves it in both. Only the primary group - the first one - decides which
    folder physically holds the skill on the remote, so a plain add never moves data.
    """
    cfg = require_config()
    name = args.skill
    asked = [c.strip() for c in args.categories if c and c.strip()]
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
    if not entry:
        print(f"not uploaded yet: python sync.py push {name}")
    return 0


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
        dst = dest_root / name
        is_link = dst.is_symlink() or (os.name == "nt" and os.path.islink(dst))
        if dst.resolve() == src.resolve():
            print(f"skip {label}: {name} is already there")
            continue
        dest_root.mkdir(parents=True, exist_ok=True)
        if dst.exists() or is_link:
            if not args.force:
                print(f"skip {label}: {name} already exists there (use --force to overwrite)")
                continue
            backup = TRASH_DIR / stamp() / label / name
            backup.parent.mkdir(parents=True, exist_ok=True)
            if is_link or dst.is_file():
                try:
                    dst.unlink()
                except OSError:
                    if os.name == "nt" and dst.is_dir():
                        os.rmdir(dst)
            elif dst.is_dir():
                shutil.move(str(dst), str(backup))

        if use_symlink:
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
            placed.append((label, dst))
            print(f"placed (symlink) {name} -> {label} ({dst})")
        else:
            shutil.copytree(src, dst)
            placed.append((label, dst))
            print(f"placed {name} -> {label} ({dst})")

    if placed:
        print("Restart the target client(s) so they discover the skill.")
    log(f"place {name} -> {[l for l, _ in placed]}")
    return 0 if placed or not targets else 1


def cmd_resolve(args):
    cfg = require_config()
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


def cmd_hook_stop(args):
    """Auto-push on session end. Must be fast, silent and never break the session."""
    cfg = load_config()
    if not cfg or not shutil.which("rclone"):
        return 0
    if lock_is_live():
        return 0                                 # a real sync is running; it will cover this
    changed = changed_since_state()
    if not changed:
        return 0
    sizes = {n: (skill_path(n, cfg) and fingerprint_cached(skill_path(n, cfg))[3]) or 0
             for n in changed}
    changed.sort(key=lambda n: sizes[n])
    # Closing a session must not hang on a slow link. Smallest first, within a budget;
    # whatever does not fit is named in the log and goes up next time.
    budget = float(cfg.get("hook_budget_seconds") or HOOK_BUDGET_SECONDS)
    ns = argparse.Namespace(skills=changed, dry_run=False, force=False, no_scan=False,
                            json=False, deadline=time.monotonic() + budget,
                            assume_default=bool(cfg.get("auto_default_category", True)))
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


def cmd_update(args):
    """Update skill-sync from GitHub with strict ZIP path traversal protection & input sanitization."""
    current_ver = local_version()
    print(f"Current skill-sync version: {current_ver}")
    if getattr(args, "check", False):
        print(f"Skill-sync repository: {REPO_URL}")
        return 0

    import urllib.request
    import zipfile
    import io

    print(f"Fetching latest update from {ARCHIVE_URL}...")
    req = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "skill-sync-update/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise SyncError(f"HTTP error {resp.status} fetching update")
            content_bytes = resp.read()
    except Exception as err:
        raise SyncError(f"Failed to download update: {err}")

    # Maximum allowed package size guard (25MB)
    if len(content_bytes) > 25 * 1024 * 1024:
        raise SyncError("Update package exceeds maximum allowed size limit (25 MB)")

    target_dir = Path(__file__).resolve().parent.parent
    try:
        with zipfile.ZipFile(io.BytesIO(content_bytes)) as z:
            names = z.namelist()
            # Path Traversal Guard (prevent zip-slip / arbitrary code execution vulnerabilities)
            for name in names:
                parts = Path(name).parts
                if ".." in parts or any(p.startswith("/") or p.startswith("\\") for p in parts):
                    raise SyncError(f"Security error: invalid path traversal detected in archive entry '{name}'")
            
            with tempfile.TemporaryDirectory(prefix="skill-sync-update-") as tmp_extract:
                z.extractall(tmp_extract)
                extracted_items = list(Path(tmp_extract).iterdir())
                if not extracted_items:
                    raise SyncError("Update archive is empty")
                root_sub = extracted_items[0]
                if not (root_sub / "SKILL.md").exists():
                    raise SyncError("Update archive missing mandatory SKILL.md")
                
                for item in root_sub.iterdir():
                    dst = target_dir / item.name
                    if item.is_dir():
                        shutil.copytree(item, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, dst)
    except zipfile.BadZipFile:
        raise SyncError("Downloaded update is not a valid ZIP archive")

    print(f"Successfully updated skill-sync at {target_dir}")
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

    s = sub.add_parser("update", help=f"update skill-sync itself from {REPO_URL}")
    s.add_argument("--check", action="store_true", help="only report whether one is available")
    s.add_argument("--force", action="store_true", help="reinstall even if already current")
    s.set_defaults(func=cmd_update)

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
