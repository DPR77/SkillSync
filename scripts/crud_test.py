#!/usr/bin/env python3
"""CRUD-lifecycle stress test for skill-sync, using a local folder as the rclone remote.

Separate from selftest.py (which is one long end-to-end story). This file is a big pile
of small, independent checks that each set up their own throwaway remote + machine(s),
exercise one CRUD/edge-case behaviour, and record PASS/FAIL. One test's failure never
blocks the rest from running.

Nothing outside a temporary directory is touched: the real ~/.claude/skills and
~/.claude/skill-sync are never read or written (both are redirected with env vars), and
the "remote" is always a plain local folder - never a real rclone cloud remote.

    python crud_test.py            # run
    python crud_test.py --keep     # keep the temp dir for inspection
"""

from __future__ import annotations

import argparse
import concurrent.futures
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


def remote_manifest(remote: Path) -> dict:
    """The remote index: one manifest.json holding every skill.

    A per-skill manifest.d/ layout is still read if one is present, so this helper keeps
    working against a remote written by the build that used it.
    """
    base = remote / "ClaudeSkills"
    aggregate = base / "manifest.json"
    if aggregate.is_file():
        data = json.loads(aggregate.read_text(encoding="utf-8"))
        data.setdefault("skills", {})
        return data
    skills = {}
    shards = base / "manifest.d"
    if shards.is_dir():
        for f in sorted(shards.glob("*.json")):
            skills[f.stem] = json.loads(f.read_text(encoding="utf-8"))
    return {"skills": skills}


def check(name: str, condition: bool, detail: str = ""):
    ok = bool(condition)
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" - {detail}" if detail and not ok else ""))
    return ok


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

    def run(self, *args, expect=None, timeout=300):
        proc = subprocess.run([sys.executable, str(SYNC), *args], env=self.env(),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        out = proc.stdout.decode("utf-8", "replace")
        if expect is not None and proc.returncode != expect:
            print(f"    (unexpected exit {proc.returncode}, wanted {expect}) args={args}\n{out}")
        return proc.returncode, out

    def make_skill(self, name: str, body: str = "hello", extra: dict | None = None):
        d = self.skills / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: test skill {name}\n---\n\n{body}\n", encoding="utf-8")
        for rel, content in (extra or {}).items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content, encoding="utf-8")
        return d

    def status_json(self):
        code, out = self.run("status", "--json")
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            print(out)
            raise


# --------------------------------------------------------------- fixtures

def fresh_single(root, tag, categories="work,school,personal", default="personal"):
    remote = root / f"remote-{tag}"
    remote.mkdir(parents=True, exist_ok=True)
    m = Machine(root, f"m-{tag}")
    code, out = m.run("setup", "--remote", str(remote), "--root", "ClaudeSkills",
                      "--categories", categories, "--default-category", default,
                      "--machine", f"M-{tag}")
    if code != 0:
        raise RuntimeError(f"setup failed for {tag}: {out}")
    return m, remote


def fresh_pair(root, tag, categories="work,school,personal"):
    remote = root / f"remote-{tag}"
    remote.mkdir(parents=True, exist_ok=True)
    a = Machine(root, f"a-{tag}")
    b = Machine(root, f"b-{tag}")
    for mach, label in ((a, "A"), (b, "B")):
        code, out = mach.run("setup", "--remote", str(remote), "--root", "ClaudeSkills",
                             "--categories", categories, "--default-category", "personal",
                             "--machine", f"{label}-{tag}")
        if code != 0:
            raise RuntimeError(f"setup failed for {label}-{tag}: {out}")
    return a, b, remote


def run_test(fn, root):
    tag = fn.__name__
    print(f"\n--- {tag} ---")
    try:
        fn(root)
    except Exception as e:
        check(f"{tag}: unhandled exception", False, repr(e))


# ============================================================ CREATE ======

def test_create_local_only(root):
    m, remote = fresh_single(root, "create-local-only")
    m.make_skill("newskill", "v1")
    st = m.status_json()
    check("new skill appears as local-only", st["skills"]["newskill"]["state"] == "local-only",
          st["skills"].get("newskill"))


def test_push_uploads_to_category_folder(root):
    m, remote = fresh_single(root, "push-category")
    m.make_skill("gamma", "v1")
    m.run("categorize", "gamma", "work", expect=0)
    code, out = m.run("push", expect=0)
    check("push reports upload", code == 0 and "Uploaded 1" in out, out)
    check("skill lands under the right category folder on the remote",
          (remote / "ClaudeSkills" / "work" / "gamma" / "SKILL.md").exists())
    check("skill is NOT under any other category folder",
          not (remote / "ClaudeSkills" / "personal" / "gamma").exists()
          and not (remote / "ClaudeSkills" / "school" / "gamma").exists())


def test_manifest_fields_after_push(root):
    m, remote = fresh_single(root, "manifest-fields")
    m.make_skill("delta", "hello world", {"scripts/run.py": "print(1)\n"})
    m.run("categorize", "delta", "school", expect=0)
    m.run("push", expect=0)
    manifest = remote_manifest(remote)
    entry = manifest.get("skills", {}).get("delta")
    check("manifest has an entry for the new skill", entry is not None, str(manifest))
    if entry:
        check("manifest category is correct", entry.get("category") == "school", str(entry))
        check("manifest hash looks like a sha256 hex digest",
              isinstance(entry.get("hash"), str) and len(entry["hash"]) == 64
              and all(c in "0123456789abcdef" for c in entry["hash"]), str(entry.get("hash")))
        check("manifest file count is 2 (SKILL.md + scripts/run.py)", entry.get("files") == 2,
              str(entry.get("files")))
        check("manifest size is > 0", (entry.get("size") or 0) > 0, str(entry.get("size")))
        check("manifest records the uploading machine", entry.get("machine") == "M-manifest-fields",
              str(entry.get("machine")))
        check("manifest records updated_at", bool(entry.get("updated_at")), str(entry))


# ============================================================== READ ======

def test_status_json_states_matrix(root):
    a, b, remote = fresh_pair(root, "status-matrix")
    a.make_skill("only_on_a", "v1")
    a.run("categorize", "only_on_a", "work", expect=0)
    a.run("push", expect=0)
    st_a = a.status_json()
    check("uploaded+unpulled skill is in-sync on the uploading machine",
          st_a["skills"]["only_on_a"]["state"] == "in-sync", st_a["skills"].get("only_on_a"))
    st_b = b.status_json()
    check("same skill shows remote-only on a machine that never pulled it",
          st_b["skills"]["only_on_a"]["state"] == "remote-only", st_b["skills"].get("only_on_a"))
    b.make_skill("only_on_b", "v1")
    st_b = b.status_json()
    check("a skill that exists only locally on B shows local-only",
          st_b["skills"]["only_on_b"]["state"] == "local-only", st_b["skills"].get("only_on_b"))
    check("only_on_b is invisible in A's status (never pushed)",
          "only_on_b" not in a.status_json()["skills"])


def test_pull_reproduces_files_byte_for_byte(root):
    a, b, remote = fresh_pair(root, "pull-byte-exact")
    payload_text = "line1\nline2 with unicode café ☃\n"
    payload_bin = bytes(range(256)) * 4
    a.make_skill("epsilon", "body text", {
        "notes.md": payload_text,
        "data/blob.bin": payload_bin,
    })
    a.run("categorize", "epsilon", "work", expect=0)
    a.run("push", expect=0)
    code, out = b.run("pull", "work", expect=0)
    check("pull onto second machine succeeds", code == 0, out)
    src_dir = a.skills / "epsilon"
    dst_dir = b.skills / "epsilon"
    all_match = True
    mismatches = []
    for rel in ("SKILL.md", "notes.md", "data/blob.bin"):
        s = (src_dir / rel).read_bytes()
        d_path = dst_dir / rel
        if not d_path.exists():
            all_match = False
            mismatches.append(f"{rel}: missing on B")
            continue
        d = d_path.read_bytes()
        if s != d:
            all_match = False
            mismatches.append(f"{rel}: content differs ({len(s)} vs {len(d)} bytes)")
    check("pulled files are byte-for-byte identical to the source", all_match, "; ".join(mismatches))


def test_bare_pull_lists_categories(root):
    a, b, remote = fresh_pair(root, "bare-pull-list")
    a.make_skill("zeta", "v1")
    a.run("categorize", "zeta", "work", expect=0)
    a.make_skill("eta", "v1")
    a.run("categorize", "eta", "school", expect=0)
    a.run("push", expect=0)
    code, out = b.run("pull", expect=2)
    check("bare pull with no args exits 2 (needs user input)", code == 2, out)
    check("bare pull lists the work category with its count", "work (1)" in out, out)
    check("bare pull lists the school category with its count", "school (1)" in out, out)
    check("bare pull did not write anything to B's skills dir",
          not (b.skills / "zeta").exists() and not (b.skills / "eta").exists())


# ============================================================ UPDATE ======

def test_edit_marks_local_newer(root):
    m, remote = fresh_single(root, "edit-local-newer")
    m.make_skill("theta", "v1")
    m.run("categorize", "theta", "work", expect=0)
    m.run("push", expect=0)
    time.sleep(1.1)
    (m.skills / "theta" / "SKILL.md").write_text(
        "---\nname: theta\ndescription: test skill theta\n---\n\nv2\n", encoding="utf-8")
    st = m.status_json()
    check("editing a synced skill's file marks it local-newer",
          st["skills"]["theta"]["state"] == "local-newer", st["skills"]["theta"]["state"])


def test_repush_uploads_only_changed_skill(root):
    m, remote = fresh_single(root, "repush-selective")
    m.make_skill("iota", "v1")
    m.make_skill("kappa", "v1")
    m.run("categorize", "iota", "work", expect=0)
    m.run("categorize", "kappa", "work", expect=0)
    m.run("push", expect=0)
    manifest_before = remote_manifest(remote)
    kappa_updated_before = manifest_before["skills"]["kappa"]["updated_at"]
    time.sleep(1.1)
    (m.skills / "iota" / "SKILL.md").write_text(
        "---\nname: iota\ndescription: test skill iota\n---\n\nv2\n", encoding="utf-8")
    code, out = m.run("push", expect=0)
    check("re-push after editing one skill reports Uploaded 1", "Uploaded 1" in out, out)
    check("re-push output mentions the edited skill", "iota" in out, out)
    check("re-push output does NOT mention the untouched skill", "kappa ->" not in out, out)
    manifest_after = remote_manifest(remote)
    check("untouched skill's manifest entry (updated_at) is unchanged",
          manifest_after["skills"]["kappa"]["updated_at"] == kappa_updated_before,
          f"before={kappa_updated_before} after={manifest_after['skills']['kappa']['updated_at']}")


def test_rename_file_propagates(root):
    a, b, remote = fresh_pair(root, "rename-propagates")
    a.make_skill("lambda", "v1", {"old_name.txt": "content"})
    a.run("categorize", "lambda", "work", expect=0)
    a.run("push", expect=0)
    b.run("pull", "work", expect=0)
    check("baseline file present on B before rename", (b.skills / "lambda" / "old_name.txt").exists())

    (a.skills / "lambda" / "old_name.txt").rename(a.skills / "lambda" / "new_name.txt")
    code, out = a.run("push", expect=0)
    check("push after rename succeeds", code == 0, out)
    check("renamed file exists under new name on remote",
          (remote / "ClaudeSkills" / "work" / "lambda" / "new_name.txt").exists())
    check("old filename no longer exists on remote",
          not (remote / "ClaudeSkills" / "work" / "lambda" / "old_name.txt").exists())

    code, out = b.run("pull", "work", expect=0)
    check("pull after rename succeeds on B", code == 0, out)
    check("new filename propagates to B", (b.skills / "lambda" / "new_name.txt").exists())
    check("old filename is removed from B", not (b.skills / "lambda" / "old_name.txt").exists())


def test_delete_file_propagates(root):
    a, b, remote = fresh_pair(root, "delete-file-propagates")
    a.make_skill("mu", "v1", {"keep.txt": "keep", "remove_me.txt": "bye"})
    a.run("categorize", "mu", "work", expect=0)
    a.run("push", expect=0)
    b.run("pull", "work", expect=0)
    check("file to be deleted exists on remote before deletion",
          (remote / "ClaudeSkills" / "work" / "mu" / "remove_me.txt").exists())

    (a.skills / "mu" / "remove_me.txt").unlink()
    a.run("push", expect=0)
    check("deleted file is gone from the remote after push",
          not (remote / "ClaudeSkills" / "work" / "mu" / "remove_me.txt").exists())
    check("sibling file survives on the remote",
          (remote / "ClaudeSkills" / "work" / "mu" / "keep.txt").exists())

    b.run("pull", "work", expect=0)
    check("deleted file disappears on the second machine after pull",
          not (b.skills / "mu" / "remove_me.txt").exists())
    check("sibling file still present on the second machine",
          (b.skills / "mu" / "keep.txt").exists())


def test_conflict_blocked_until_resolve(root):
    a, b, remote = fresh_pair(root, "conflict-block")
    a.make_skill("nu", "v1")
    a.run("categorize", "nu", "work", expect=0)
    a.run("push", expect=0)
    b.run("pull", "work", expect=0)

    time.sleep(1.1)
    (a.skills / "nu" / "SKILL.md").write_text(
        "---\nname: nu\ndescription: test skill nu\n---\n\nfrom A\n", encoding="utf-8")
    (b.skills / "nu" / "SKILL.md").write_text(
        "---\nname: nu\ndescription: test skill nu\n---\n\nfrom B\n", encoding="utf-8")
    b.run("push", expect=0)

    st = a.status_json()
    check("both-sides-edited skill is detected as a conflict",
          st["skills"]["nu"]["state"] == "conflict", st["skills"]["nu"]["state"])
    code, out = a.run("push", expect=1)
    check("push is blocked while a conflict is unresolved", code == 1 and "CONFLICT" in out, out)
    check("local file is untouched by the blocked push",
          "from A" in (a.skills / "nu" / "SKILL.md").read_text(encoding="utf-8"))

    code, out = a.run("resolve", "nu", "--keep", "local", expect=0)
    check("resolve --keep local succeeds", code == 0, out)
    st = a.status_json()
    check("skill is in-sync immediately after resolve", st["skills"]["nu"]["state"] == "in-sync",
          st["skills"]["nu"]["state"])
    code, out = a.run("push", expect=0)
    check("push works normally again after resolve", code == 0, out)


# ============================================================ DELETE ======

def test_remove_local_makes_remote_only(root):
    m, remote = fresh_single(root, "remove-local")
    m.make_skill("xi", "v1")
    m.run("categorize", "xi", "work", expect=0)
    m.run("push", expect=0)
    shutil.rmtree(m.skills / "xi")
    st = m.status_json()
    check("removing the local folder makes the skill remote-only",
          st["skills"]["xi"]["state"] == "remote-only", st["skills"].get("xi"))


def test_prune_requires_confirmation(root):
    m, remote = fresh_single(root, "prune-confirm")
    m.make_skill("omicron", "v1")
    m.run("categorize", "omicron", "work", expect=0)
    m.run("push", expect=0)
    shutil.rmtree(m.skills / "omicron")
    code, out = m.run("prune", expect=2)
    check("prune without --yes exits 2", code == 2, out)
    check("prune without --yes names the orphan skill", "omicron" in out, out)
    check("prune without --yes deletes nothing on the remote",
          (remote / "ClaudeSkills" / "work" / "omicron").exists())
    manifest = remote_manifest(remote)
    check("prune without --yes leaves the manifest entry intact", "omicron" in manifest["skills"])


def test_prune_yes_removes_remote_and_manifest(root):
    m, remote = fresh_single(root, "prune-yes")
    m.make_skill("pi", "v1")
    m.run("categorize", "pi", "work", expect=0)
    m.run("push", expect=0)
    shutil.rmtree(m.skills / "pi")
    code, out = m.run("prune", "--yes", expect=0)
    check("prune --yes succeeds", code == 0, out)
    check("prune --yes deletes the skill folder from the remote",
          not (remote / "ClaudeSkills" / "work" / "pi").exists())
    manifest = remote_manifest(remote)
    check("prune --yes removes the manifest entry", "pi" not in manifest["skills"])


# ============================================== CATEGORY / GROUP MOVES ====

def test_categorize_moves_remote_and_manifest(root):
    m, remote = fresh_single(root, "categorize-move")
    m.make_skill("rho", "v1")
    m.run("categorize", "rho", "work", expect=0)
    m.run("push", expect=0)
    check("skill starts under work/ on the remote",
          (remote / "ClaudeSkills" / "work" / "rho" / "SKILL.md").exists())
    code, out = m.run("categorize", "rho", "personal", expect=0)
    check("categorize (move) succeeds", code == 0, out)
    check("old category folder is gone from the remote",
          not (remote / "ClaudeSkills" / "work" / "rho").exists())
    check("skill now lives under the new category folder on the remote",
          (remote / "ClaudeSkills" / "personal" / "rho" / "SKILL.md").exists())
    manifest = remote_manifest(remote)
    check("manifest category is updated to the new category",
          manifest["skills"]["rho"]["category"] == "personal", str(manifest["skills"]["rho"]))


def test_categorize_new_category_registers_in_config(root):
    m, remote = fresh_single(root, "categorize-newcat")
    m.make_skill("sigma", "v1")
    code, out = m.run("categorize", "sigma", "hobby-projects", expect=0)
    check("categorize into a brand-new category succeeds", code == 0, out)
    cfg = json.loads((m.state / "config.json").read_text(encoding="utf-8"))
    check("brand-new category is registered in config.json",
          "hobby-projects" in cfg.get("categories", []), str(cfg.get("categories")))


# =================================================================== EDGE CASES ====

def test_skill_name_with_spaces(root):
    m, remote = fresh_single(root, "name-spaces")
    name = "my cool skill"
    m.make_skill(name, "v1")
    m.run("categorize", name, "work", expect=0)
    code, out = m.run("push", expect=0)
    check("skill name with spaces can be pushed", code == 0 and "Uploaded 1" in out, out)
    check("skill name with spaces lands correctly on the remote",
          (remote / "ClaudeSkills" / "work" / name / "SKILL.md").exists())
    st = m.status_json()
    check("skill name with spaces reports in-sync afterwards",
          st["skills"].get(name, {}).get("state") == "in-sync", st["skills"].get(name))


def test_skill_name_with_dots(root):
    m, remote = fresh_single(root, "name-dots")
    name = "v1.2.3-release"
    m.make_skill(name, "v1")
    m.run("categorize", name, "work", expect=0)
    code, out = m.run("push", expect=0)
    check("skill name with dots can be pushed", code == 0 and "Uploaded 1" in out, out)
    check("skill name with dots lands correctly on the remote",
          (remote / "ClaudeSkills" / "work" / name / "SKILL.md").exists())


def test_skill_name_unicode(root):
    m, remote = fresh_single(root, "name-unicode")
    name = "café-日本語"
    m.make_skill(name, "v1")
    m.run("categorize", name, "work", expect=0)
    code, out = m.run("push", expect=0)
    check("unicode skill name can be pushed", code == 0 and "Uploaded 1" in out, out)
    check("unicode skill name lands correctly on the remote",
          (remote / "ClaudeSkills" / "work" / name / "SKILL.md").exists())
    st = m.status_json()
    check("unicode skill name reports in-sync afterwards",
          st["skills"].get(name, {}).get("state") == "in-sync", str(list(st["skills"])))


def test_empty_skill_minimal(root):
    """A skill dir that has nothing but SKILL.md (no body files at all)."""
    m, remote = fresh_single(root, "empty-skill")
    d = m.skills / "barebones"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\nname: barebones\ndescription: x\n---\n", encoding="utf-8")
    m.run("categorize", "barebones", "work", expect=0)
    code, out = m.run("push", expect=0)
    check("a skill with only SKILL.md and nothing else can be pushed", code == 0, out)
    manifest = remote_manifest(remote)
    check("minimal skill's manifest reports exactly 1 file",
          manifest["skills"]["barebones"]["files"] == 1, str(manifest["skills"]["barebones"]))


def test_dir_without_skill_md_is_ignored(root):
    m, remote = fresh_single(root, "no-skillmd")
    d = m.skills / "not_a_skill"
    d.mkdir(parents=True, exist_ok=True)
    (d / "notes.txt").write_text("just some files, no SKILL.md", encoding="utf-8")
    st = m.status_json()
    check("a directory without SKILL.md is ignored entirely",
          "not_a_skill" not in st["skills"], str(list(st["skills"])))
    code, out = m.run("push", expect=0)
    check("push with nothing else to do says Nothing to upload and does not error",
          code == 0, out)


def test_deep_nested_path(root):
    a, b, remote = fresh_pair(root, "deep-nested")
    depth_rel = "/".join(f"lvl{i}" for i in range(1, 16)) + "/leaf.txt"
    a.make_skill("tau", "v1", {depth_rel: "deep content"})
    a.run("categorize", "tau", "work", expect=0)
    code, out = a.run("push", expect=0)
    check("push succeeds with a very deep nested path (15 levels)", code == 0, out)
    remote_leaf = remote / "ClaudeSkills" / "work" / "tau"
    for part in depth_rel.split("/"):
        remote_leaf = remote_leaf / part
    check("deeply nested file exists on the remote", remote_leaf.exists())
    code, out = b.run("pull", "work", expect=0)
    check("pull succeeds with a very deep nested path", code == 0, out)
    local_leaf = b.skills / "tau"
    for part in depth_rel.split("/"):
        local_leaf = local_leaf / part
    check("deeply nested file reaches the second machine intact",
          local_leaf.exists() and local_leaf.read_text(encoding="utf-8") == "deep content")


def test_skillignore_excludes_files(root):
    a, b, remote = fresh_pair(root, "skillignore")
    a.make_skill("upsilon", "v1", {
        "keep.txt": "keep me",
        "secret.local": "should never leave this machine",
        ".skillignore": "secret.local\n",
    })
    a.run("categorize", "upsilon", "work", expect=0)
    code, out = a.run("push", expect=0)
    check("push with a .skillignore succeeds", code == 0, out)
    check(".skillignore-excluded file is absent on the remote",
          not (remote / "ClaudeSkills" / "work" / "upsilon" / "secret.local").exists())
    check("non-ignored sibling file is present on the remote",
          (remote / "ClaudeSkills" / "work" / "upsilon" / "keep.txt").exists())
    b.run("pull", "work", expect=0)
    check(".skillignore-excluded file never reaches the second machine",
          not (b.skills / "upsilon" / "secret.local").exists())


def test_size_boundary(root):
    """BIG_SKILL_BYTES = 20 MiB in sync.py; push() prints a 'may be slow' note only when a
    skill's total size is strictly greater than that. Exercise both sides of the boundary."""
    BIG = 20 * 1024 * 1024
    m, remote = fresh_single(root, "size-boundary")

    # write_bytes (not write_text) is deliberate: write_text opens in text mode, and on
    # Windows that translates every \n to \r\n, silently inflating the on-disk byte count
    # past what len(skill_md.encode("utf-8")) measured - which would poison this exact
    # boundary math. write_bytes gives byte-for-byte control.
    skill_md = "---\nname: atboundary\ndescription: x\n---\n\nbody\n"
    skill_md_bytes = len(skill_md.encode("utf-8"))
    d = m.skills / "atboundary"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_bytes(skill_md.encode("utf-8"))
    with (d / "pad.bin").open("wb") as f:
        f.truncate(BIG - skill_md_bytes)   # total size == BIG exactly
    m.run("categorize", "atboundary", "work", expect=0)
    code, out = m.run("push", expect=0, timeout=600)
    check("skill exactly at the BIG_SKILL_BYTES boundary pushes fine", code == 0, out)
    check("no 'may be slow' note at exactly the boundary (condition is strictly >)",
          "may be slow" not in out, out)

    d2 = m.skills / "overboundary"
    d2.mkdir(parents=True, exist_ok=True)
    (d2 / "SKILL.md").write_bytes(skill_md.replace("atboundary", "overboundary").encode("utf-8"))
    with (d2 / "pad.bin").open("wb") as f:
        f.truncate(BIG - skill_md_bytes + 1)   # total size == BIG + 1
    m.run("categorize", "overboundary", "work", expect=0)
    code, out = m.run("push", expect=0, timeout=600)
    check("skill one byte over the boundary pushes fine too", code == 0, out)
    check("'may be slow' note appears once over the boundary", "may be slow" in out, out)


def test_repush_no_changes_is_noop(root):
    m, remote = fresh_single(root, "noop-repush")
    m.make_skill("phi", "v1")
    m.run("categorize", "phi", "work", expect=0)
    m.run("push", expect=0)
    code, out = m.run("push", expect=0)
    check("re-pushing with no changes reports Nothing to upload", "Nothing to upload" in out, out)
    code, out = m.run("push", expect=0)
    check("a third consecutive no-op push is still clean", code == 0 and "Nothing to upload" in out, out)


def test_lock_stale_taken_over_and_live_blocks(root):
    m, remote = fresh_single(root, "lock-manual")
    m.make_skill("chi", "v1")
    m.run("categorize", "chi", "work", expect=0)

    lock = m.state / "sync.lock"
    lock.write_text(json.dumps({"pid": 999999, "time": time.time()}), encoding="utf-8")
    code, out = m.run("status", expect=0)
    check("a lock held by a dead pid is taken over (does not block)", code == 0, out)

    lock.write_text(json.dumps({"pid": os.getpid(), "time": time.time()}), encoding="utf-8")
    code, out = m.run("push", "chi", expect=1)
    check("a lock held by a live pid blocks a new run", code == 1 and "in progress" in out, out)
    lock.unlink()
    code, out = m.run("push", "chi", expect=0)
    check("push works again once the live lock is released", code == 0, out)


def test_concurrent_pushes_lock_exercised(root):
    """Real two-process concurrency (not a simulated lock file): fire two `push` runs at
    the same moment against the same state dir, on a skill big enough that the first
    process is still holding the lock when the second tries to acquire it."""
    m, remote = fresh_single(root, "lock-concurrent")
    d = m.make_skill("psi", "v1")
    with (d / "pad.bin").open("wb") as f:
        f.truncate(30 * 1024 * 1024)
    m.run("categorize", "psi", "work", expect=0)

    outcomes = {}

    def worker(key):
        outcomes[key] = m.run("push", "psi", "--force")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(worker, "first"), ex.submit(worker, "second")]
        concurrent.futures.wait(futs)

    codes = [outcomes["first"][0], outcomes["second"][0]]
    outs = [outcomes["first"][1], outcomes["second"][1]]
    blocked_seen = any("in progress" in o for o in outs)
    check("at least one of the two concurrent pushes succeeded", codes.count(0) >= 1, str(codes))
    check("the lock rejects the loser cleanly (SyncError 'in progress'), never a raw crash",
          all("Traceback" not in o for o in outs),
          f"codes={codes}; a concurrent Lock.__enter__/save_json race raised an unhandled "
          f"exception instead of the intended SyncError - see full output below\n"
          + "\n---\n".join(outs))

    try:
        manifest = remote_manifest(remote)
        manifest_ok = "psi" in manifest.get("skills", {})
    except (json.JSONDecodeError, OSError) as e:
        manifest_ok = False
        manifest = str(e)
    check("the manifest is valid JSON and not corrupted after the race",
          manifest_ok, str(manifest)[:300])

    code, out = m.run("push", "psi", expect=0)
    st = m.status_json()
    check("skill converges to in-sync after the race settles",
          st["skills"]["psi"]["state"] == "in-sync", st["skills"]["psi"]["state"])


# ======================================================================== main ====

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = ap.parse_args()

    if not shutil.which("rclone"):
        print("SKIP: rclone is not installed, this test cannot run.")
        print("  Windows: winget install Rclone.Rclone")
        return 2

    root = Path(tempfile.mkdtemp(prefix="skill-sync-crudtest-"))
    print(f"temp dir: {root}\n")

    tests = [
        # CREATE
        test_create_local_only,
        test_push_uploads_to_category_folder,
        test_manifest_fields_after_push,
        # READ
        test_status_json_states_matrix,
        test_pull_reproduces_files_byte_for_byte,
        test_bare_pull_lists_categories,
        # UPDATE
        test_edit_marks_local_newer,
        test_repush_uploads_only_changed_skill,
        test_rename_file_propagates,
        test_delete_file_propagates,
        test_conflict_blocked_until_resolve,
        # DELETE
        test_remove_local_makes_remote_only,
        test_prune_requires_confirmation,
        test_prune_yes_removes_remote_and_manifest,
        # CATEGORY / GROUP MOVES
        test_categorize_moves_remote_and_manifest,
        test_categorize_new_category_registers_in_config,
        # EDGE CASES
        test_skill_name_with_spaces,
        test_skill_name_with_dots,
        test_skill_name_unicode,
        test_empty_skill_minimal,
        test_dir_without_skill_md_is_ignored,
        test_deep_nested_path,
        test_skillignore_excludes_files,
        test_size_boundary,
        test_repush_no_changes_is_noop,
        test_lock_stale_taken_over_and_live_blocks,
        test_concurrent_pushes_lock_exercised,
    ]

    try:
        for fn in tests:
            run_test(fn, root)
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
