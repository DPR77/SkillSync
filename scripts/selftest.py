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


def menu_visible_len(line: str) -> int:
    """menu.py's own width calculation, so the UI checks measure what it measures."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_menu_for_test", HERE / "menu.py")
    module = sys.modules.get("_menu_for_test")
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules["_menu_for_test"] = module
        spec.loader.exec_module(module)
    return module.visible_len(line)

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name: str, condition: bool, detail: str = ""):
    results.append((PASS if condition else FAIL, name, detail))
    print(f"  [{PASS if condition else FAIL}] {name}" + (f" - {detail}" if detail and not condition else ""))
    return condition


class Machine:
    def __init__(self, root: Path, name: str, extra_skill_dirs: int = 0):
        self.name = name
        self.skills = root / name / "skills"
        self.state = root / name / "state"
        self.skills.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        # Real machines keep skills in several clients' folders (~/.claude, ~/.gemini,
        # .agents). Anything that writes a skill has to respect where it already lives.
        self.other_skills = []
        for n in range(extra_skill_dirs):
            d = root / name / f"skills{n + 2}"
            d.mkdir(parents=True, exist_ok=True)
            self.other_skills.append(d)

    def env(self):
        e = dict(os.environ)
        e["CLAUDE_SKILLS_DIR"] = os.pathsep.join(
            [str(self.skills)] + [str(d) for d in self.other_skills])
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

    def make_skill(self, name: str, body: str = "hello", extra: dict | None = None,
                   in_dir: Path | None = None):
        d = (in_dir or self.skills) / name
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
        manifest = json.loads(
            (remote / "ClaudeSkills" / "manifest.json").read_text(encoding="utf-8"))
        check("manifest lists both skills",
              {"alpha", "beta"} <= set(manifest["skills"]), list(manifest["skills"]))

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
        # Built at runtime so this literal is not itself a scannable secret. It has to look
        # like a real key: a sequential one (sk-abcdef...) is what documentation uses, and
        # the scanner deliberately lets those through.
        fake_key = "sk-" + "T3nQ7pXvR2mK9wZ4bF6hL8cJ5dY1gS0a"
        a.make_skill("leaky", "test", {"scripts/cfg.py": f'API_KEY = "{fake_key}"\n'})
        a.run("categorize", "leaky", "work", expect=0)
        code, out = a.run("push", "leaky", expect=1)
        check("push blocked by the credential scan", "Possible credentials" in out, out)
        check("the report names the file and line", "cfg.py:1" in out, out)
        check("nothing uploaded for the leaky skill",
              not (remote / "ClaudeSkills" / "work" / "leaky").exists())

        # A documented example must not trip the guard, or people learn to reach for
        # --no-scan and lose the check entirely.
        a.make_skill("docs-only", "test",
                     {"README.md": 'Set API_KEY = "sk-your-api-key-goes-here-example" first.\n'})
        a.run("categorize", "docs-only", "work", expect=0)
        code, out = a.run("push", "docs-only", expect=0)
        check("documentation placeholders do not block a push", "Uploaded 1" in out, out)

        # And a real-looking key the author has judged safe can be marked in place.
        (a.skills / "leaky" / "scripts" / "cfg.py").write_text(
            f'API_KEY = "{fake_key}"  # skill-sync: allow-secret\n', encoding="utf-8")
        code, out = a.run("push", "leaky", expect=0)
        check("allow-secret pragma unblocks one line", "Uploaded 1" in out, out)

        (a.skills / "leaky" / "scripts" / "cfg.py").write_text(
            f'API_KEY = "{fake_key}"\n', encoding="utf-8")
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
        manifest = json.loads(
            (remote / "ClaudeSkills" / "manifest.json").read_text(encoding="utf-8"))
        check("manifest no longer lists beta", "beta" not in manifest["skills"])
        check("pruning one skill leaves the others' entries alone",
              "alpha" in manifest["skills"], list(manifest["skills"]))

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
        rendered = {}
        for label, flags in (("unicode", ["--self-check", "--no-color"]),
                             ("ascii", ["--self-check", "--no-color", "--ascii"])):
            proc = subprocess.run([sys.executable, str(menu), *flags], env=a.env(),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
            out = proc.stdout.decode("utf-8", "replace")
            rendered[label] = out
            check(f"menu renders ({label})",
                  proc.returncode == 0 and "main menu" in out and "Upload skills" in out, out[-400:])
            check(f"menu draws checkboxes ({label})", "[x]" in out and "[ ]" in out)

        # Asserting "no box-drawing characters" let every emoji through, which is how the
        # menu ended up unreadable on consoles that cannot render them. Assert the actual
        # invariant instead: --ascii output is 7-bit, nothing else.
        stray = sorted({c for c in rendered["ascii"] if ord(c) > 127})
        check("ascii mode emits only 7-bit characters", not stray, f"found: {stray}")

        # Emoji stand in for text of the same printed width, so swapping them must not move
        # a column. Compare the two renders line for line.
        u_lines, a_lines = rendered["unicode"].splitlines(), rendered["ascii"].splitlines()
        mismatch = next((i for i, (u, v) in enumerate(zip(u_lines, a_lines))
                         if menu_visible_len(u.rstrip()) != menu_visible_len(v.rstrip())), None)
        check("ascii and unicode renders keep the same column widths",
              len(u_lines) == len(a_lines) and mismatch is None,
              "" if mismatch is None else f"line {mismatch}:\n{u_lines[mismatch]}\n{a_lines[mismatch]}")
        proc = subprocess.run([sys.executable, str(menu)], env=a.env(), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        out = proc.stdout.decode("utf-8", "replace")
        check("menu refuses to run without a terminal",
              proc.returncode == 2 and "real terminal" in out, out)

        print("\nskills outside the primary folder")
        d = Machine(root, "machineD", extra_skill_dirs=1)
        second = d.other_skills[0]
        d.make_skill("roamer", "v1 from the second folder", in_dir=second)
        d.run("setup", "--remote", str(remote), "--root", "ClaudeSkills",
              "--categories", "work", "--machine", "D", expect=0)
        d.run("categorize", "roamer", "work", expect=0)
        code, out = d.run("push", "roamer", expect=0)
        check("a skill outside the primary folder can be uploaded", "Uploaded 1" in out, out)

        e = Machine(root, "machineE")
        e.run("setup", "--remote", str(remote), "--root", "ClaudeSkills",
              "--categories", "work", "--machine", "E", expect=0)
        e.run("pull", "--skills", "roamer", expect=0)
        time.sleep(1.1)
        (e.skills / "roamer" / "SKILL.md").write_text(
            "---\nname: roamer\ndescription: test skill roamer\n---\n\nv2 from E\n",
            encoding="utf-8")
        e.run("push", "roamer", expect=0)

        code, out = d.run("pull", "--skills", "roamer", expect=0)
        check("download updates the copy where it already lives",
              "v2 from E" in (second / "roamer" / "SKILL.md").read_text(encoding="utf-8"), out)
        check("download does not duplicate it into the primary folder",
              not (d.skills / "roamer").exists())
        st = d.status_json()
        check("the updated skill reads as in-sync", st["skills"]["roamer"]["state"] == "in-sync",
              st["skills"]["roamer"]["state"])

        print("\na skill in more than one group")
        # Adding a group must not take the skill out of the ones it already has, and must
        # not move a single byte on the remote.
        a.run("categorize", "alpha", "personal", "--add", expect=0)
        st = a.status_json()
        check("adding a group keeps the existing one",
              sorted(st["skills"]["alpha"]["categories"]) == ["personal", "work"],
              st["skills"]["alpha"].get("categories"))
        check("the skill is still stored under its primary group only",
              (remote / "ClaudeSkills" / "work" / "alpha" / "SKILL.md").exists()
              and not (remote / "ClaudeSkills" / "personal" / "alpha").exists())

        code, out = b.run("pull", expect=2)
        listing = {line.split("(")[0].strip(): line for line in out.splitlines() if "):" in line}
        check("it is listed under both groups",
              "alpha" in listing.get("personal", "") and "alpha" in listing.get("work", ""),
              out)

        g = Machine(root, "machineG")
        g.run("setup", "--remote", str(remote), "--root", "ClaudeSkills",
              "--categories", "personal", "--machine", "G", expect=0)
        g.run("pull", "personal", expect=0)
        check("pulling the second group brings the skill down",
              (g.skills / "alpha" / "SKILL.md").exists())

        a.run("categorize", "alpha", "personal", "--remove", expect=0)
        st = a.status_json()
        check("removing one group leaves the other",
              st["skills"]["alpha"]["categories"] == ["work"],
              st["skills"]["alpha"].get("categories"))
        code, out = a.run("categorize", "alpha", "work", "--remove", expect=1)
        check("a skill cannot be left with no group at all", "no group at all" in out, out)

        print("\nsplit manifest.d consolidation")
        # A remote left in the per-skill layout by an older build must keep working and
        # fold itself back into the single index, without losing an entry.
        split_remote = root / "split-remote"
        base = split_remote / "ClaudeSkills"
        (base / "work" / "oldie").mkdir(parents=True, exist_ok=True)
        (base / "work" / "oldie" / "SKILL.md").write_text(
            "---\nname: oldie\ndescription: test skill oldie\n---\n\nfrom the split layout\n",
            encoding="utf-8")
        (base / "manifest.d").mkdir(parents=True, exist_ok=True)
        (base / "manifest.d" / "oldie.json").write_text(json.dumps(
            {"category": "work", "categories": ["work"], "hash": "deadbeef", "size": 10,
             "files": 1, "machine": "old", "updated_at": "2026-01-01T00:00:00+00:00"}),
            encoding="utf-8")
        f = Machine(root, "machineF")
        f.run("setup", "--remote", str(split_remote), "--root", "ClaudeSkills",
              "--categories", "work", "--machine", "F", expect=0)
        st = f.status_json()
        check("a split manifest is still readable", "oldie" in st["skills"], list(st["skills"]))
        f.make_skill("fresh", "new skill")
        f.run("categorize", "fresh", "work", expect=0)
        f.run("push", "fresh", expect=0)
        check("it consolidates into a single index",
              (base / "manifest.json").exists() and not (base / "manifest.d").exists())
        st = f.status_json()
        check("nothing is lost in the consolidation",
              {"oldie", "fresh"} <= set(st["skills"]), list(st["skills"]))
        check("reads no longer touch the per-skill layout",
              "oldie" in json.loads((base / "manifest.json").read_text(encoding="utf-8"))["skills"])

        print("\nhook installation")
        settings = root / "settings.json"
        mine = {"hooks": {"Stop": [{"hooks": [{"type": "command",
                                               "command": "echo skill-sync is great"}]}]}}
        settings.write_text(json.dumps(mine), encoding="utf-8")
        subprocess.run([sys.executable, str(HERE / "install_hooks.py"),
                        "--settings", str(settings)], stdout=subprocess.PIPE, timeout=60)
        data = json.loads(settings.read_text(encoding="utf-8"))
        commands = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
        check("installing keeps an unrelated hook that merely says 'skill-sync'",
              "echo skill-sync is great" in commands, commands)
        subprocess.run([sys.executable, str(HERE / "install_hooks.py"), "--uninstall",
                        "--settings", str(settings)], stdout=subprocess.PIPE, timeout=60)
        data = json.loads(settings.read_text(encoding="utf-8"))
        commands = [h["command"] for g in data.get("hooks", {}).get("Stop", []) for h in g["hooks"]]
        check("uninstalling removes only our own hook",
              commands == ["echo skill-sync is great"], commands)

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
