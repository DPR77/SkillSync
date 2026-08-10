#!/usr/bin/env python3
"""Tests for the per-project usage counter.

The counter reads Claude Code's transcripts incrementally, so what matters is not the
counting itself but the bookkeeping around it: offsets that must not re-read, a session
that is still being written, a transcript that gets truncated, and the byte budget that
keeps the Stop hook fast.

Nothing outside a temporary directory is touched: the real ~/.claude/projects,
~/.claude/skills and ~/.claude/skill-sync are all redirected.

    python usage_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

ROOT = Path(tempfile.mkdtemp(prefix="skill-sync-usage-"))
os.environ["SKILL_SYNC_HOME"] = str(ROOT / "home")
SKILLS = ROOT / "skills"
for _name in ("alfa", "beta", "gamma"):
    (SKILLS / _name).mkdir(parents=True)
    (SKILLS / _name / "SKILL.md").write_text(f"---\nname: {_name}\n---\n", encoding="utf-8")
os.environ["CLAUDE_SKILLS_DIR"] = str(SKILLS)

sys.path.insert(0, str(HERE))
import sync  # noqa: E402  (local module, env and path set above)

PROJECTS = ROOT / "projects"
PROJECT_DIR = PROJECTS / "C--dev-demo"
PROJECT_DIR.mkdir(parents=True)
sync.CLAUDE_PROJECTS_DIR = PROJECTS
sync.STATE_DIR.mkdir(parents=True, exist_ok=True)
sync.save_json(sync.CONFIG_FILE, {"remote": str(ROOT / "remote"), "root": "R",
                                  "categories": ["work"], "default_category": "work",
                                  "machine": "usage-test"})
CFG = sync.load_config()
CWD = r"C:\dev\demo" if os.name == "nt" else "/dev/demo"

passed = failed = 0


def check(label, condition) -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}")


def line(payload) -> str:
    return json.dumps(payload) + "\n"


def counts():
    return dict(sync.usage_for_project(CWD, CFG))


def main() -> int:
    t1 = PROJECT_DIR / "s1.jsonl"
    t1.write_text(
        line({"cwd": CWD, "type": "user"})
        + line({"tool": {"name": "Skill", "input": {"skill": "alfa"}}})
        + line({"content": "<command-name>/beta</command-name>"})
        + line({"content": "<command-name>/model</command-name>"}),
        encoding="utf-8")

    print("\ncounting")
    sync.scan_usage(CFG)
    check("a Skill tool call is counted", counts().get("alfa") == 1)
    check("a slash command is counted", counts().get("beta") == 1)
    check("a CLI command that is not an installed skill is ignored", "model" not in counts())
    check("the project key comes from the transcript's cwd, not the folder slug",
          CWD in sync.usage_state()[1]["projects"])

    print("\nincremental reads")
    result = sync.scan_usage(CFG)
    check("a second scan reads nothing", result["bytes"] == 0)
    check("and does not double count", counts().get("alfa") == 1)

    with open(t1, "a", encoding="utf-8") as fh:
        fh.write(line({"tool": {"name": "Skill", "input": {"skill": "alfa"}}}))
    result = sync.scan_usage(CFG)
    check("an appended line is picked up", result["bytes"] > 0)
    check("and counted", counts().get("alfa") == 2)

    print("\na session still being written")
    with open(t1, "a", encoding="utf-8") as fh:
        fh.write('{"tool": {"name": "Skill", "input": {"skill": "gamma"}}}')
    sync.scan_usage(CFG)
    check("a half-written line is not counted yet", "gamma" not in counts())
    with open(t1, "a", encoding="utf-8") as fh:
        fh.write("\n")
    sync.scan_usage(CFG)
    check("it is counted once the line is complete", counts().get("gamma") == 1)

    print("\ntruncation and the byte budget")
    before = counts()
    t1.write_text(line({"cwd": CWD}), encoding="utf-8")
    sync.scan_usage(CFG)
    check("a rewritten transcript restarts instead of being skipped forever",
          counts() == before)

    (PROJECT_DIR / "s2.jsonl").write_text(
        line({"cwd": CWD})
        + line({"tool": {"name": "Skill", "input": {"skill": "beta"}}}), encoding="utf-8")
    (PROJECT_DIR / "s3.jsonl").write_text(line({"cwd": CWD + "-other"}) * 400,
                                          encoding="utf-8")
    result = sync.scan_usage(CFG, budget_bytes=200)
    check("the budget defers work instead of reading everything", result["pending"] >= 1)
    result = sync.scan_usage(CFG, budget_bytes=10 * 1024 * 1024)
    check("the deferred bytes are read on the next run", result["bytes"] > 0)
    check("nothing is lost across the two runs", counts().get("beta") == 2)

    print("\ncase-insensitive project keys")
    _st, u = sync.usage_state()
    other = (CWD[0].swapcase() + CWD[1:]) if os.name == "nt" else CWD + "-CASE"
    u["projects"][other] = {"skills": {"alfa": 5}, "sessions": 1}
    sync.save_json(sync.STATE_FILE, _st)
    _st, u = sync.usage_state()
    if os.name == "nt":
        check("the same folder spelled with a different drive case is one bucket",
              other not in u["projects"] and counts().get("alfa") == 2 + 5)
    else:
        check("a genuinely different path stays its own bucket", other in u["projects"])
        u["projects"].pop(other)
        sync.save_json(sync.STATE_FILE, _st)

    print("\nfull rebuild")
    result = sync.scan_usage(CFG, full=True)
    check("--full re-reads every transcript", result["bytes"] > 0)
    # s1 was truncated above, so its historical hit is no longer on disk to be re-read:
    # --full rebuilds from what the transcripts contain now rather than doubling totals.
    check("the counts are rebuilt from what is on disk, not doubled",
          counts().get("beta") == 1)

    print(f"\n{passed}/{passed + failed} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
    sys.exit(code)
