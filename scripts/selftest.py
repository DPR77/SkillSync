#!/usr/bin/env python3
"""End-to-end self test for skill-sync, using a local folder as the rclone remote.

Simulates two machines (A and B) sharing one remote and checks the whole life cycle:
setup, categorize, push, pull on a fresh machine, edit + re-push, conflict detection,
conflict resolution, and the prune safety guard.

Nothing outside a temporary directory is touched: the real ~/.claude/skills and
~/.claude/skill-sync are never read or written (both are redirected with env vars).

    python selftest.py            # run
    python selftest.py --keep     # keep the temp dir for inspection
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYNC = HERE / "sync.py"

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name: str, condition: bool, detail: str = ""):
    results.append((PASS if condition else FAIL, name, detail))
    print(f"  [{PASS if condition else FAIL}] {name}" + (f" - {detail}" if detail and not condition else ""))
    return condition


class Machine:
    def __init__(self, root: Path, name: str):
        self.name = name
        self.skills = root / name / "skills"
        self.state = root / name / "state"
        self.skills.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)

    def env(self):
        e = dict(os.environ)
        e["CLAUDE_SKILLS_DIR"] = str(self.skills)
        e["SKILL_SYNC_HOME"] = str(self.state)
        e["PYTHONIOENCODING"] = "utf-8"
        return e

    def run(self, *args, expect=None):
        proc = subprocess.run([sys.executable, str(SYNC), *args], env=self.env(),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
        out = proc.stdout.decode("utf-8", "replace")
        if expect is not None and proc.returncode != expect:
            print(f"    (unexpected exit {proc.returncode}, wanted {expect})\n{out}")
        return proc.returncode, out

    def make_skill(self, name: str, body: str = "hello", extra: dict | None = None):
        d = self.skills / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: test skill {name}\n---\n\n{body}\n", encoding="utf-8")
        for rel, content in (extra or {}).items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return d

    def status_json(self):
        code, out = self.run("status", "--json")
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            print(out)
            raise


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = ap.parse_args()

    if not shutil.which("rclone"):
        print("SKIP: rclone is not installed, the end-to-end test cannot run.")
        print("  Windows: winget install Rclone.Rclone")
        print("  macOS:   brew install rclone")
        return 2

    root = Path(tempfile.mkdtemp(prefix="skill-sync-selftest-"))
    remote = root / "remote"
    remote.mkdir()
    print(f"temp dir: {root}\n")

    try:
        a = Machine(root, "machineA")
        b = Machine(root, "machineB")

        print("machine A: setup + first push")
        a.make_skill("alpha", "alpha v1")
        a.make_skill("beta", "beta v1", {"scripts/run.py": "print('beta')\n"})
        code, out = a.run("setup", "--remote", str(remote), "--root", "ClaudeSkills",
                          "--categories", "work,personal", "--default-category", "personal",
                          "--machine", "A", expect=0)
        check("setup succeeds", code == 0, out)

        code, out = a.run("push", expect=1)
        check("push refuses skills without a category", "no category" in out, out)

        a.run("categorize", "alpha", "work", expect=0)
        a.run("categorize", "beta", "personal", expect=0)
        code, out = a.run("push", expect=0)
        check("push uploads both skills", code == 0 and "Uploaded 2" in out, out)
        check("remote layout has categories",
              (remote / "ClaudeSkills" / "work" / "alpha" / "SKILL.md").exists()
              and (remote / "ClaudeSkills" / "personal" / "beta" / "scripts" / "run.py").exists())
        check("manifest written", (remote / "ClaudeSkills" / "manifest.json").exists())

        code, out = a.run("push", expect=0)
        check("second push is a no-op", "Nothing to upload" in out, out)

        print("\nmachine B: selective pull")
        b.run("setup", "--remote", str(remote), "--root", "ClaudeSkills",
              "--categories", "work", "--machine", "B", expect=0)
        code, out = b.run("pull", expect=2)
        check("bare pull lists categories", "work (1)" in out and "personal (1)" in out, out)
        code, out = b.run("pull", "work", expect=0)
        check("pull work only brings alpha",
              (b.skills / "alpha" / "SKILL.md").exists() and not (b.skills / "beta").exists(), out)
        st = b.status_json()
        check("pulled skill is in-sync on B", st["skills"]["alpha"]["state"] == "in-sync",
              st["skills"]["alpha"]["state"])
        check("beta shows as remote-only on B", st["skills"]["beta"]["state"] == "remote-only")

        print("\nmachine B: edit and push back")
        time.sleep(1.1)
        (b.skills / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: test skill alpha\n---\n\nalpha v2 from B\n",
            encoding="utf-8")
        st = b.status_json()
        check("edit detected as local-newer", st["skills"]["alpha"]["state"] == "local-newer",
              st["skills"]["alpha"]["state"])
        code, out = b.run("push", expect=0)
        check("B uploads the edit", "Uploaded 1" in out, out)

        st = a.status_json()
        check("A sees remote-newer", st["skills"]["alpha"]["state"] == "remote-newer",
              st["skills"]["alpha"]["state"])
        code, out = a.run("push", expect=0)
        check("A refuses to overwrite a newer remote", "remote is newer" in out, out)
        code, out = a.run("pull", "work", expect=0)
        check("A pulls B's version",
              "alpha v2 from B" in (a.skills / "alpha" / "SKILL.md").read_text(encoding="utf-8"), out)

        print("\nremoved-file propagation")
        (b.skills / "alpha" / "extra.md").write_text("temporary\n", encoding="utf-8")
        b.run("push", expect=0)
        a.run("pull", "work", expect=0)
        check("added file reaches A", (a.skills / "alpha" / "extra.md").exists())
        (b.skills / "alpha" / "extra.md").unlink()
        b.run("push", expect=0)
        a.run("pull", "work", expect=0)
        check("deleted file disappears on A", not (a.skills / "alpha" / "extra.md").exists())

        print("\nconflict handling")
        time.sleep(1.1)
        (a.skills / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: test skill alpha\n---\n\nalpha from A\n", encoding="utf-8")
        (b.skills / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: test skill alpha\n---\n\nalpha from B\n", encoding="utf-8")
        b.run("push", expect=0)
        st = a.status_json()
        check("A detects a conflict", st["skills"]["alpha"]["state"] == "conflict",
              st["skills"]["alpha"]["state"])
        code, out = a.run("push", expect=1)
        check("conflict blocks push", "CONFLICT" in out, out)
        check("local file untouched by the blocked push",
              "alpha from A" in (a.skills / "alpha" / "SKILL.md").read_text(encoding="utf-8"))
        code, out = a.run("resolve", "alpha", "--keep", "local", expect=0)
        conflicts = list((a.state / "conflicts").glob("alpha-remote-*"))
        check("resolve keeps a backup of the remote side", bool(conflicts), out)
        check("backup holds B's version",
              any("alpha from B" in (p / "SKILL.md").read_text(encoding="utf-8")
                  for p in conflicts if (p / "SKILL.md").exists()))
        st = a.status_json()
        check("A is in sync after resolve", st["skills"]["alpha"]["state"] == "in-sync",
              st["skills"]["alpha"]["state"])

        print("\ncredential guard")
        a.make_skill("leaky", "test", {"scripts/cfg.py": 'API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123"\n'})
        a.run("categorize", "leaky", "work", expect=0)
        code, out = a.run("push", "leaky", expect=1)
        check("push blocked by the credential scan", "Possible credentials" in out, out)
        check("nothing uploaded for the leaky skill",
              not (remote / "ClaudeSkills" / "work" / "leaky").exists())
        code, out = a.run("push", "leaky", "--no-scan", expect=0)
        check("--no-scan overrides the guard", "Uploaded 1" in out, out)

        print("\nprune safety")
        shutil.rmtree(a.skills / "beta", ignore_errors=True)
        code, out = a.run("prune", expect=2)
        check("prune asks for confirmation", "--yes" in out and "beta" in out, out)
        check("prune did not delete anything yet",
              (remote / "ClaudeSkills" / "personal" / "beta").exists())
        code, out = a.run("prune", "--yes", "--only", "beta", expect=0)
        check("prune --yes deletes the orphan",
              not (remote / "ClaudeSkills" / "personal" / "beta").exists(), out)
        manifest = json.loads((remote / "ClaudeSkills" / "manifest.json").read_text(encoding="utf-8"))
        check("manifest no longer lists beta", "beta" not in manifest["skills"])

        print("\nhooks")
        time.sleep(1.1)
        (a.skills / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: test skill alpha\n---\n\nalpha v3\n", encoding="utf-8")
        code, out = a.run("hook-stop", "--quiet", expect=0)
        st = a.status_json()
        check("Stop hook auto-uploads the change", st["skills"]["alpha"]["state"] == "in-sync", out)
        code, out = a.run("hook-stop", "--quiet", expect=0)
        check("Stop hook is silent when nothing changed", out.strip() == "", out)
        code, out = b.run("hook-session-start", "--quiet", "--force", expect=0)
        check("SessionStart reports remote updates", "skill-sync" in out and "pull" in out, out)

        unconfigured = Machine(root, "machineC")
        code, out = unconfigured.run("hook-stop", "--quiet", expect=0)
        check("hooks are a no-op without config", code == 0 and out.strip() == "", out)

        print("\nmenu UI")
        menu = HERE / "menu.py"
        for label, flags in (("unicode", ["--self-check", "--no-color"]),
                             ("ascii", ["--self-check", "--no-color", "--ascii"])):
            proc = subprocess.run([sys.executable, str(menu), *flags], env=a.env(),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
            out = proc.stdout.decode("utf-8", "replace")
            check(f"menu renders ({label})",
                  proc.returncode == 0 and "main menu" in out and "Upload skills" in out, out[-400:])
            check(f"menu draws checkboxes ({label})", "[x]" in out and "[ ]" in out)
        check("ascii mode avoids box-drawing characters",
              "╔" not in subprocess.run(
                  [sys.executable, str(menu), "--self-check", "--ascii", "--no-color"],
                  env=a.env(), stdout=subprocess.PIPE, timeout=120
              ).stdout.decode("utf-8", "replace"))
        proc = subprocess.run([sys.executable, str(menu)], env=a.env(), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        out = proc.stdout.decode("utf-8", "replace")
        check("menu refuses to run without a terminal",
              proc.returncode == 2 and "real terminal" in out, out)

    finally:
        failed = [r for r in results if r[0] == FAIL]
        print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
        for _s, name, detail in failed:
            print(f"  FAIL {name} {detail[:200]}")
        if args.keep:
            print(f"temp dir kept: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)

    return 1 if any(r[0] == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
