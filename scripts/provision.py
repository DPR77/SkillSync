#!/usr/bin/env python3
"""Installs the tools skill-sync needs, without administrator rights.

rclone is the only external binary skill-sync depends on. A package manager is the first
choice, but winget/Homebrew are not everywhere and `apt` needs root, which left locked-down
machines with no way forward at all. So there is a second path: fetch the official build
straight from downloads.rclone.org into a private folder under the state directory.

Everything that path does is deliberately narrow, because downloading and running a binary
is exactly the behaviour a security audit should be suspicious of:

  * HTTPS only, and only to downloads.rclone.org - any other host is refused outright.
  * The archive is checked against the SHA-256 published by the rclone project for that
    exact release. A mismatch aborts and installs nothing.
  * Only the rclone executable is unpacked, into ~/.claude/skill-sync/bin. Nothing is
    written outside the state directory, no PATH is edited, no service is registered.
  * Nothing here runs on its own. It happens when someone picks it in the menu, or runs
    this file, or runs install.py.

Usage:
    python provision.py rclone          install rclone if it is missing
    python provision.py rclone --force  reinstall even if one is already present
    python provision.py which           report what is installed and where
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOME = Path.home()
STATE_DIR = Path(os.environ.get("SKILL_SYNC_HOME") or (HOME / ".claude" / "skill-sync")).expanduser()
MANAGED_BIN = STATE_DIR / "bin"

RCLONE_HOST = "downloads.rclone.org"
RCLONE_BASE = f"https://{RCLONE_HOST}"
ALLOWED_HOSTS = {RCLONE_HOST}

MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
USER_AGENT = "skill-sync"


class ProvisionError(RuntimeError):
    pass


def rclone_exe_name() -> str:
    return "rclone.exe" if os.name == "nt" else "rclone"


def managed_rclone() -> Path:
    return MANAGED_BIN / rclone_exe_name()


# ------------------------------------------------------------------ downloading

def _check_url(url: str) -> None:
    from urllib.parse import urlparse

    parts = urlparse(url)
    if parts.scheme != "https":
        raise ProvisionError(f"refusing a non-HTTPS download: {url}")
    if parts.hostname not in ALLOWED_HOSTS:
        raise ProvisionError(f"refusing to download from {parts.hostname!r}; "
                             f"only {', '.join(sorted(ALLOWED_HOSTS))} is allowed")


def fetch(url: str, timeout: int = 60) -> bytes:
    """Small files (version.txt, SHA256SUMS) read straight into memory."""
    _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(MAX_DOWNLOAD_BYTES)


def fetch_to_file(url: str, dest: Path, timeout: int = 300, progress=None) -> str:
    """Stream a download to disk and return its SHA-256."""
    _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        size = int(resp.headers.get("Content-Length") or 0)
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ProvisionError("download exceeded the size limit; aborted")
                digest.update(chunk)
                fh.write(chunk)
                if progress:
                    progress(total, size)
    return digest.hexdigest()


# ---------------------------------------------------------------------- rclone

def rclone_asset() -> str:
    """The rclone build for this machine, as named in the project's own release folder."""
    system = platform.system().lower()
    os_part = {"windows": "windows", "darwin": "osx", "linux": "linux",
               "freebsd": "freebsd", "netbsd": "netbsd", "openbsd": "openbsd"}.get(system)
    if not os_part:
        raise ProvisionError(f"no rclone build is published for {platform.system()}")

    machine = (platform.machine() or "").lower()
    if machine in ("amd64", "x86_64", "x64"):
        arch = "amd64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("i386", "i686", "x86"):
        arch = "386"
    elif machine.startswith("armv7") or machine == "armv7l":
        arch = "arm-v7"
    elif machine.startswith("arm"):
        arch = "arm"
    else:
        raise ProvisionError(f"no rclone build is published for the {machine!r} architecture")
    return f"{os_part}-{arch}"


def latest_rclone_version() -> str:
    text = fetch(f"{RCLONE_BASE}/version.txt", timeout=30).decode("utf-8", "replace").strip()
    version = text.split()[-1] if text else ""
    if not version.startswith("v"):
        raise ProvisionError(f"could not read the current rclone version (got {text!r})")
    return version


def published_sha256(version: str, filename: str) -> str:
    """The hash rclone publishes for this exact file, from the release's SHA256SUMS."""
    sums = fetch(f"{RCLONE_BASE}/{version}/SHA256SUMS", timeout=60).decode("utf-8", "replace")
    for line in sums.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            return parts[0].lower()
    raise ProvisionError(f"{filename} is not listed in the SHA256SUMS for {version}")


def install_rclone(log=print, force: bool = False) -> Path:
    """Download the official rclone build, verify it, and unpack it into the state dir."""
    target = managed_rclone()
    if target.is_file() and not force:
        log(f"rclone already installed at {target}")
        return target

    version = latest_rclone_version()
    asset = rclone_asset()
    stem = f"rclone-{version}-{asset}"
    filename = f"{stem}.zip"
    url = f"{RCLONE_BASE}/{version}/{filename}"

    log(f"downloading {filename} from {RCLONE_HOST}")
    expected = published_sha256(version, filename)

    tmpdir = Path(tempfile.mkdtemp(prefix="skill-sync-rclone-"))
    try:
        archive = tmpdir / filename

        last = [-1]

        def progress(done, size):
            if not size:
                return
            pct = int(done * 100 / size)
            if pct != last[0] and pct % 10 == 0:
                last[0] = pct
                log(f"  {pct}%")

        actual = fetch_to_file(url, archive, progress=progress)
        if actual != expected:
            raise ProvisionError(
                f"checksum mismatch for {filename}.\n"
                f"  expected {expected}\n  got      {actual}\n"
                "Nothing was installed.")
        log("checksum verified against the rclone project's SHA256SUMS")

        exe = rclone_exe_name()
        with zipfile.ZipFile(archive) as zf:
            member = next((m for m in zf.namelist()
                           if m.rsplit("/", 1)[-1] == exe and not m.endswith("/")), None)
            if not member:
                raise ProvisionError(f"{filename} did not contain {exe}")
            # Only one member is ever unpacked, and only after its path is checked: a
            # zip that resolves outside the temp dir is rejected rather than sanitised.
            extracted = (tmpdir / "unpacked").resolve()
            src = (extracted / member).resolve()
            if extracted not in src.parents:
                raise ProvisionError(f"{filename} contains an unsafe path: {member!r}")

            MANAGED_BIN.mkdir(parents=True, exist_ok=True)
            zf.extract(member, extracted)
            # Replacing a running binary fails on Windows; move the old one aside first.
            if target.exists():
                try:
                    target.unlink()
                except OSError:
                    target.rename(target.with_suffix(target.suffix + ".old"))
            shutil.move(str(src), str(target))

        if os.name != "nt":
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        log(f"rclone {version} installed at {target}")
        return target
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- orchestration

def find_rclone() -> str | None:
    """PATH, then the package-manager folders, then skill-sync's own copy."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import sync
        return sync.rclone_bin(required=False)
    except Exception:
        exe = shutil.which("rclone")
        if exe:
            return exe
        managed = managed_rclone()
        return str(managed) if managed.is_file() else None


def package_manager_argv():
    """An installer that runs as the user. Anything needing root is left to the user.

    A tool that quietly calls sudo is both a smell auditors flag and a hang, because the
    password prompt has nowhere to appear when the process was spawned by an agent.
    """
    if os.name == "nt" and shutil.which("winget"):
        return (["winget", "install", "-e", "--id", "Rclone.Rclone",
                 "--accept-source-agreements", "--accept-package-agreements"],
                "winget install Rclone.Rclone")
    if sys.platform == "darwin" and shutil.which("brew"):
        return ["brew", "install", "rclone"], "brew install rclone"
    return None, None


def ensure_rclone(log=print, allow_manager: bool = True, force: bool = False) -> str | None:
    if not force:
        exe = find_rclone()
        if exe:
            log(f"rclone found: {exe}")
            return exe

    if allow_manager:
        argv, shown = package_manager_argv()
        if argv:
            log(f"installing rclone with: {shown}")
            try:
                subprocess.run(argv, check=False)
            except Exception as e:
                log(f"{shown} failed: {e}")
            exe = find_rclone()
            if exe:
                log(f"rclone installed: {exe}")
                return exe
            log("the package manager did not leave a usable rclone; downloading it instead")

    try:
        return str(install_rclone(log=log, force=force))
    except ProvisionError as e:
        log(str(e))
    except Exception as e:
        log(f"could not download rclone: {e}")
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rclone = sub.add_parser("rclone", help="install rclone if it is missing")
    p_rclone.add_argument("--force", action="store_true",
                          help="reinstall even if rclone is already present")
    p_rclone.add_argument("--download-only", action="store_true",
                          help="skip winget/Homebrew and fetch the official build directly")

    sub.add_parser("which", help="report what is installed and where")

    args = parser.parse_args(argv)

    if args.cmd == "which":
        exe = find_rclone()
        print(f"python : {sys.executable} ({platform.python_version()})")
        print(f"rclone : {exe or 'NOT FOUND'}")
        print(f"managed bin : {MANAGED_BIN}")
        return 0 if exe else 1

    exe = ensure_rclone(allow_manager=not args.download_only, force=args.force)
    if not exe:
        print("\nrclone could not be installed automatically. Install it yourself:")
        print("  Windows: winget install Rclone.Rclone")
        print("  macOS:   brew install rclone")
        print("  Linux:   sudo apt install rclone | sudo dnf install rclone")
        return 1
    print(f"\nrclone is ready: {exe}")
    print("Next: run `rclone config` to connect your cloud account, or open the skill-sync menu.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
