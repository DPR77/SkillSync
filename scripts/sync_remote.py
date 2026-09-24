"""Everything that touches the remote: rclone, the git provider, manifest.json, status, push/pull of one skill and backup housekeeping.

Part of skill-sync; `sync.py` is the entry point and re-exports these names.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from sync_core import (  # noqa: F401
    CONFLICT,
    CONFLICTS_DIR,
    HOME,
    IN_SYNC,
    KEEP_TRASH_DAYS,
    LOCAL_NEW,
    MANIFEST_DIR,
    MANIFEST_LEGACY_NAME,
    MANIFEST_NAME,
    ONLY_LOCAL,
    ONLY_REMOTE,
    REMOTE_NEW,
    REMOTE_TRASH_SWEEP_INTERVAL,
    REMOTE_TRASH_SWEEP_MAX,
    SKILLS_DIR,
    STATE_DIR,
    STATE_FILE,
    Spinner,
    SyncError,
    TRASH_DIR,
    load_state,
    local_skills_map,
    log,
    machine_name,
    now_iso,
    progress_bar,
    save_json,
    stamp,
)
from sync_scan import (  # noqa: F401
    STAMP_RE,
    detect_native_origin,
    entry_categories,
    fingerprint_cached,
    is_self,
    primary_category,
    rclone_filter_file,
    save_fp_cache,
)


# -------------------------------------------------------------------- rclone

def rclone_candidates():
    """Where rclone ends up when it is installed but not on this shell's PATH.

    winget, Homebrew and Scoop all extend PATH for *future* shells, so a terminal that was
    already open when rclone was installed reports it missing - and the menu then sent
    people off to install something they already had. The state dir comes first: that is
    where provision.py puts the copy it downloads when no package manager is available.
    """
    exe = "rclone.exe" if os.name == "nt" else "rclone"
    paths = [STATE_DIR / "bin" / exe, HOME / ".claude" / "skill-sync" / "bin" / exe]
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA") or (HOME / "AppData" / "Local"))
        paths.append(local / "Microsoft" / "WinGet" / "Links" / exe)
        packages = local / "Microsoft" / "WinGet" / "Packages"
        if packages.is_dir():
            for pkg in sorted(packages.glob("Rclone.Rclone_*")):
                paths.extend(sorted(pkg.glob(f"**/{exe}")))
        paths += [Path(r"C:\ProgramData\chocolatey\bin") / exe,
                  HOME / "scoop" / "shims" / exe,
                  Path(os.environ.get("ProgramFiles") or r"C:\Program Files") / "rclone" / exe]
    else:
        paths += [Path("/opt/homebrew/bin") / exe, Path("/usr/local/bin") / exe,
                  Path("/usr/bin") / exe, Path("/snap/bin") / exe,
                  HOME / ".local" / "bin" / exe, HOME / "bin" / exe]
    return paths


def rclone_bin(required=True, _cache={}):
    exe = shutil.which("rclone")
    if not exe:
        exe = _cache.get("exe")
        if exe and not Path(exe).exists():
            exe = None
        if not exe:
            for candidate in rclone_candidates():
                try:
                    if candidate.is_file():
                        exe = str(candidate)
                        _cache["exe"] = exe
                        log(f"rclone found off PATH at {exe}")
                        break
                except OSError:
                    continue
    if not exe and required:
        raise SyncError(
            "rclone was not found. skill-sync can install it for you, no admin needed:\n"
            "  python scripts/provision.py rclone\n"
            "Or install it yourself, then run `rclone config`:\n"
            "  Windows: winget install Rclone.Rclone\n"
            "  macOS:   brew install rclone\n"
            "  Linux:   sudo apt install rclone | sudo dnf install rclone\n"
            "  Already installed? Open a new terminal so PATH picks it up, or run the\n"
            "  skill-sync menu, which can install it for you.")
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


# ---------------------------------------------------------------------- git

# A git repository is not an rclone backend, so it is used the other way round: the remote
# is a normal local folder that happens to be a clone, and every command pulls before
# reading it and commits/pushes after writing it. Everything downstream - rclone sync,
# the manifest, conflicts, .trash - keeps working untouched.
GIT_CLONE_DIR = "gitremote"
GIT_IGNORE = ".trash/\n"
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


def git_cfg(cfg):
    g = (cfg or {}).get("git")
    return g if isinstance(g, dict) and g.get("url") else None


def git_bin(required=True):
    exe = shutil.which("git")
    if not exe and required:
        raise SyncError("git is not installed, or not on PATH")
    return exe


def git_run(clone, args, check=True, timeout=600):
    env = dict(os.environ, **GIT_ENV)
    res = subprocess.run([git_bin(), "-C", str(clone)] + list(args),
                         capture_output=True, text=True, timeout=timeout, env=env,
                         encoding="utf-8", errors="replace")
    if check and res.returncode != 0:
        raise SyncError(f"git {' '.join(args[:2])} failed: "
                        f"{(res.stderr or res.stdout).strip()[:400]}")
    return res.returncode, res.stdout or "", res.stderr or ""


def git_clone_path(cfg) -> Path:
    g = git_cfg(cfg)
    return Path(g["clone"]).expanduser() if g and g.get("clone") else (
        STATE_DIR / GIT_CLONE_DIR)


def ensure_git_clone(cfg) -> Path:
    """Clone on first use; an empty repository is normal for a brand-new one."""
    g = git_cfg(cfg)
    clone = git_clone_path(cfg)
    if (clone / ".git").exists():
        return clone
    branch = g.get("branch") or "main"
    clone.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, **GIT_ENV)
    with Spinner(f"cloning {g['url']}"):
        res = subprocess.run([git_bin(), "clone", "--branch", branch, g["url"], str(clone)],
                             capture_output=True, text=True, timeout=900, env=env,
                             encoding="utf-8", errors="replace")
        if res.returncode != 0:
            # A repository with no commits yet has no branch to ask for.
            res = subprocess.run([git_bin(), "clone", g["url"], str(clone)],
                                 capture_output=True, text=True, timeout=900, env=env,
                                 encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise SyncError(f"could not clone {g['url']}:\n  "
                        f"{(res.stderr or res.stdout).strip()[:500]}\n"
                        f"  If it is asking for credentials, set them up yourself first "
                        f"(git clone the repo once by hand), then rerun this.")
    code, out, _e = git_run(clone, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    if code != 0 or out.strip() != branch:
        git_run(clone, ["checkout", "-B", branch], check=False)
    gitignore = clone / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GIT_IGNORE, encoding="utf-8")
    return clone


def git_sync_in(cfg, quiet=False) -> None:
    """Bring the clone up to date before anything reads the remote."""
    g = git_cfg(cfg)
    if not g:
        return
    clone = ensure_git_clone(cfg)
    branch = g.get("branch") or "main"
    with Spinner("git pull"):
        code, _out, err = git_run(clone, ["pull", "--ff-only", "origin", branch],
                                  check=False)
    if code != 0 and not quiet:
        detail = err.strip().splitlines()[-1][:160] if err.strip() else ""
        if "couldn't find remote ref" in err or "no such ref" in err.lower():
            return                      # empty repo: nothing published yet
        print(f"note: git pull did not run cleanly ({detail}); using the local clone. "
              f"Resolve it in {clone} if this repeats.")


def git_sync_out(cfg, message: str) -> None:
    """Commit and push whatever the command just wrote into the clone."""
    g = git_cfg(cfg)
    if not g:
        return
    clone = git_clone_path(cfg)
    if not (clone / ".git").exists():
        return
    branch = g.get("branch") or "main"
    git_run(clone, ["add", "-A"], check=False)
    _c, out, _e = git_run(clone, ["status", "--porcelain"], check=False)
    if not out.strip():
        return
    git_run(clone, ["-c", "user.name=skill-sync", "-c", "user.email=skill-sync@local",
                    "commit", "-m", f"skill-sync: {message} [{machine_name(cfg)}]"],
            check=False)
    with Spinner("git push"):
        code, _o, err = git_run(clone, ["push", "origin", f"HEAD:{branch}"], check=False)
    if code != 0:
        detail = err.strip().splitlines()[-1][:200] if err.strip() else ""
        print(f"\nwarning: committed locally but the push failed ({detail}).\n"
              f"  Nothing is lost - fix the credentials and run:\n"
              f"      git -C {clone} push origin HEAD:{branch}")
    else:
        log(f"git push {message}")


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


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
MANIFEST_TEXT_MAX = 2000


def _clean_remote_value(v, depth=0):
    """Remote-authored data, reduced to plain values: no control or bidi characters, no
    runaway strings or nesting. The manifest is written by other machines and read back
    into terminal output and agent context, so it is treated as untrusted input."""
    if depth > 6:
        return None
    if isinstance(v, str):
        return _CONTROL_RE.sub("", v)[:MANIFEST_TEXT_MAX]
    if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
        return v
    if isinstance(v, list):
        return [_clean_remote_value(x, depth + 1) for x in v[:500]]
    if isinstance(v, dict):
        return {str(k)[:200]: _clean_remote_value(x, depth + 1) for k, x in list(v.items())[:500]}
    return None


def safe_remote_skill_name(name) -> bool:
    return (isinstance(name, str) and 0 < len(name) <= 200 and name not in (".", "..")
            and not any(c in name for c in "/\\:") and not _CONTROL_RE.search(name))


def sanitize_manifest(m):
    if not isinstance(m, dict):
        raise SyncError(f"remote {MANIFEST_NAME} is not a JSON object; fix or delete it")
    m = _clean_remote_value(m)
    skills = {}
    for name, entry in (m.get("skills") or {}).items() if isinstance(m.get("skills"), dict) else []:
        if safe_remote_skill_name(name) and isinstance(entry, dict):
            skills[name] = entry
        else:
            log(f"ignoring unsafe manifest entry {name!r}")
    m["skills"] = skills
    return m


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
            return sanitize_manifest(m)
        # No aggregate: rebuild from whatever the older layouts left behind.
        skills = dict(read_legacy_manifest(cfg))
        skills.update(read_split_manifest(cfg))
    return sanitize_manifest({"version": 2, "skills": skills})


def write_manifest(cfg, entries: dict, drop=(), packs=None):
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
        if packs is not None:
            remote["packs"] = packs
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


def manifest_entry(cfg, category, fp, size, files, categories=None, native_origin=None):
    cats = [c for c in (categories or [category]) if c]
    if category and category not in cats:
        cats.insert(0, category)
    entry = {"category": category, "categories": cats, "hash": fp, "size": size,
             "files": files, "updated_at": now_iso(), "machine": cfg["machine"]}
    if native_origin:
        entry["native_origin"] = native_origin
    return entry


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
