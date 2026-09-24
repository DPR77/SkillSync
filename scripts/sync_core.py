"""Constants, paths, config and state files, logging, locking and progress output.

Part of skill-sync; `sync.py` is the entry point and re-exports these names.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import socket
import sys
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


def load_packs(cfg) -> dict:
    packs = cfg.get("packs")
    return packs if isinstance(packs, dict) else {}
