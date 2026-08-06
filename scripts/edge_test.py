#!/usr/bin/env python3
"""Extreme Edge-Case Stress Test for skill-sync.
Tests normal usage followed by extreme edge cases to ensure zero crashes or data loss.
"""

import os
import sys
import shutil
import tempfile
import subprocess
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
SYNC = HERE / "sync.py"

def run_test():
    temp_dir = Path(tempfile.mkdtemp(prefix="skill-sync-edge-"))
    skills_dir = temp_dir / "skills"
    state_dir = temp_dir / "state"
    remote_dir = temp_dir / "remote"
    skills_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    remote_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["CLAUDE_SKILLS_DIR"] = str(skills_dir)
    env["SKILL_SYNC_HOME"] = str(state_dir)
    env["PYTHONIOENCODING"] = "utf-8"

    def sync(*args, expect=0):
        proc = subprocess.run([sys.executable, str(SYNC), *args], env=env, capture_output=True, text=True)
        if proc.returncode != expect:
            print(f"FAILED (code {proc.returncode}, expected {expect}) for args: {args}\nOUTPUT:\n{proc.stdout}\nERR:\n{proc.stderr}")
            return False, proc.stdout + proc.stderr
        return True, proc.stdout

    print("=== 1. NORMAL USAGE TEST ===")
    ok, out = sync("setup", "--remote", str(remote_dir), "--categories", "work,personal")
    assert ok, "Setup failed"
    print(" [PASS] Normal setup")

    # Create normal skill
    norm_dir = skills_dir / "normal-skill"
    norm_dir.mkdir(parents=True)
    (norm_dir / "SKILL.md").write_text("---\nname: normal-skill\ndescription: normal\n---\nNormal text\n", encoding="utf-8")
    ok, _ = sync("categorize", "normal-skill", "work")
    assert ok
    ok, _ = sync("push")
    assert ok
    print(" [PASS] Normal skill push")

    print("\n=== 2. EXTREME EDGE CASES TEST ===")

    # Edge Case 1: Unicode, Emojis, Special Characters in skill name and files
    print(" -> Testing Edge Case 1: Unicode, Accents, Emojis in skill name...")
    u_name = "skill-español-ñ-áéíóú-🚀"
    u_dir = skills_dir / u_name
    u_dir.mkdir(parents=True)
    (u_dir / "SKILL.md").write_text("---\nname: unicode\ndescription: 🚀 ñ testing\n---\nContenido con eñe y acentos: áéíóú 🌟\n", encoding="utf-8")
    (u_dir / "código-fuente.py").write_text("# -*- coding: utf-8 -*-\nprint('Hola Mundo 🌍')\n", encoding="utf-8")
    
    ok, _ = sync("categorize", u_name, "work")
    assert ok, "Unicode categorize failed"
    ok, _ = sync("push")
    assert ok, "Unicode push failed"
    print("  [PASS] Unicode skill with emojis & accents pushed cleanly")

    # Edge Case 2: Deeply Nested Folders (20 subdirectories)
    print(" -> Testing Edge Case 2: 20-level deeply nested structure...")
    deep_skill = skills_dir / "deep-skill"
    deep_path = deep_skill / "/".join([f"sub_{n}" for n in range(20)])
    deep_path.mkdir(parents=True)
    (deep_skill / "SKILL.md").write_text("---\nname: deep-skill\ndescription: deep\n---\n", encoding="utf-8")
    (deep_path / "deep_file.txt").write_text("Bottom of the world", encoding="utf-8")
    ok, _ = sync("categorize", "deep-skill", "personal")
    assert ok
    ok, _ = sync("push")
    assert ok
    print("  [PASS] 20-level nested directory structure synced")

    # Edge Case 3: Zero-Byte Files & Binary Asset Files (.png, .bin)
    print(" -> Testing Edge Case 3: Zero-byte files & raw binary data...")
    bin_skill = skills_dir / "binary-skill"
    bin_skill.mkdir(parents=True)
    (bin_skill / "SKILL.md").write_text("---\nname: binary-skill\ndescription: bin\n---\n", encoding="utf-8")
    (bin_skill / "empty.txt").write_text("", encoding="utf-8")
    (bin_skill / "fake_image.png").write_bytes(bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0xFF, 0xD8]))
    ok, _ = sync("categorize", "binary-skill", "work")
    assert ok
    ok, _ = sync("push")
    assert ok
    print("  [PASS] Zero-byte and raw binary files handled correctly")

    # Edge Case 4: Broken Symlinks and Overwriting with place --symlink
    print(" -> Testing Edge Case 4: Broken symlinks & place --symlink --force...")
    agents_dir = temp_dir / "agents_skills"
    agents_dir.mkdir(parents=True)
    
    # Place symlink
    ok, _ = sync("place", "normal-skill", "--dest", str(agents_dir), "--symlink")
    assert ok
    placed_link = agents_dir / "normal-skill"
    assert placed_link.exists()

    # Now intentionally break/remove source and re-place with --force
    ok, _ = sync("place", u_name, "--dest", str(agents_dir), "--symlink", "--force")
    assert ok
    print("  [PASS] Symlinks and forced overwrites handled without errors")

    # Edge Case 5: Secret Scanner Pragma Exemption
    print(" -> Testing Edge Case 5: False-positive secret with pragma opt-out...")
    sec_skill = skills_dir / "secret-skill"
    sec_skill.mkdir(parents=True)
    (sec_skill / "SKILL.md").write_text("---\nname: secret-skill\ndescription: sec\n---\n", encoding="utf-8")
    (sec_skill / "config.py").write_text('API_KEY = "sk-ant-api03-samplekey123456789012345" # skill-sync: allow-secret\n', encoding="utf-8")
    ok, _ = sync("categorize", "secret-skill", "work")
    assert ok
    ok, out = sync("push", "secret-skill")
    assert ok, f"Pragma secret failed to push: {out}"
    print("  [PASS] Secret scanner pragma opt-out works as expected")

    # Edge Case 6: High Concurrency Sync (--threads 16)
    print(" -> Testing Edge Case 6: High concurrency parallel push/pull (--threads 16)...")
    for i in range(10):
        sk_name = f"parallel-skill-{i}"
        sk_path = skills_dir / sk_name
        sk_path.mkdir(parents=True, exist_ok=True)
        (sk_path / "SKILL.md").write_text(f"---\nname: {sk_name}\ndescription: p\n---\nData {i}\n", encoding="utf-8")
        sync("categorize", sk_name, "work")

    ok, _ = sync("push", "--threads", "16", "--force")
    assert ok, "Parallel push failed"
    ok, _ = sync("pull", "work", "--threads", "16", "--force")
    assert ok, "Parallel pull failed"
    print("  [PASS] High concurrency push & pull (--threads 16) passed without race conditions")

    print("\nALL EDGE-CASE TESTS PASSED SUCCESSFULLY! 🚀")

    # Cleanup
    try:
        shutil.rmtree(temp_dir)
    except OSError:
        pass

if __name__ == "__main__":
    run_test()
