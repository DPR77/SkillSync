#!/usr/bin/env python3
"""Multi-Platform AI Skills Scanner for skill-sync.

Detects skills across multiple AI platforms (Claude Code, Gemini/Antigravity, Cursor, Codex, OpenClaw, etc.)
and allows cross-platform synchronization.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, TypedDict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOME = Path.home()

# Platform paths definitions
# `root` is the app's own config directory - the honest signal that the tool is installed.
# `path` (its skills folder) is not: skill-sync creates that itself the first time it
# copies anything there, so an empty install would look present forever after.
PLATFORM_PATHS = {
    "Claude Code": {
        "icon": "💬",
        "badge": "[Claude]",
        "root": HOME / ".claude",
        "path": HOME / ".claude" / "skills",
        "description": "Claude Code CLI skills"
    },
    "Gemini & Antigravity": {
        "icon": "🤖",
        "badge": "[Gemini]",
        "root": HOME / ".gemini",
        "path": HOME / ".gemini" / "config" / "skills",
        "description": "Google Gemini & Antigravity IDE skills"
    },
    "Cursor IDE": {
        "icon": "⚡",
        "badge": "[Cursor]",
        "root": HOME / ".cursor",
        "path": HOME / ".cursor" / "skills",
        "description": "Cursor AI editor skills"
    },
    "OpenAI Codex": {
        "icon": "🧠",
        "badge": "[Codex]",
        "root": HOME / ".codex",
        "path": HOME / ".codex" / "skills",
        "description": "Codex AI assistant skills"
    },
    "OpenClaw / Hermes": {
        "icon": "🦅",
        "badge": "[OpenClaw]",
        "root": HOME / ".openclaw",
        "path": HOME / ".openclaw" / "skills",
        "description": "OpenClaw AI agent skills"
    },
    "Global Agents": {
        "icon": "🌐",
        "badge": "[Agents]",
        "root": HOME / ".agents",
        "path": HOME / ".agents" / "skills",
        "description": "Global user agents skills"
    }
}


def is_installed(name: str) -> bool:
    """Whether that tool is actually on this computer, judged by its own config dir."""
    info = PLATFORM_PATHS.get(name) or {}
    root = info.get("root")
    return bool(root and Path(root).exists())


class SkillItem(TypedDict):
    name: str
    platforms: List[str]
    has_skill_md: bool
    path: Path
    description: str


def scan_all_platforms() -> Dict[str, List[Dict[str, str]]]:
    """Scan all platform directories and return found skills per platform."""
    results = {}
    for platform_name, info in PLATFORM_PATHS.items():
        skills_dir = info["path"]
        found = []
        if skills_dir.exists() and skills_dir.is_dir():
            for entry in skills_dir.iterdir():
                if entry.name.startswith("."):
                    continue          # .system, .skill-lock.json - bookkeeping, not skills
                # A valid skill folder contains a SKILL.md or is a skill directory/file
                if entry.is_dir():
                    skill_md = entry / "SKILL.md"
                    desc = ""
                    if skill_md.exists():
                        try:
                            lines = skill_md.read_text(encoding="utf-8", errors="replace").splitlines()
                            for line in lines[:10]:
                                if line.startswith("description:"):
                                    desc = line.split("description:", 1)[1].strip()
                                    break
                        except Exception:
                            pass
                    found.append({
                        "name": entry.name,
                        "path": str(entry.resolve()),
                        "has_skill_md": skill_md.exists(),
                        "description": desc or "AI Skill folder"
                    })
                elif entry.is_file() and entry.suffix in (".md", ".json", ".py"):
                    found.append({
                        "name": entry.stem,
                        "path": str(entry.resolve()),
                        "has_skill_md": False,
                        "description": f"Skill file ({entry.suffix})"
                    })
        results[platform_name] = sorted(found, key=lambda x: x["name"].lower())
    return results


def get_aggregated_skills() -> Dict[str, List[str]]:
    """Get a map of skill_name -> list of platforms that have it."""
    all_scanned = scan_all_platforms()
    aggregated = {}
    for platform_name, skills in all_scanned.items():
        for skill in skills:
            s_name = skill["name"]
            aggregated.setdefault(s_name, []).append(platform_name)
    return aggregated


def sync_skills_between_platforms(source_platform: str = "Claude Code",
                                  target_platforms: List[str] = None,
                                  dry_run: bool = False,
                                  names: List[str] = None):
    """Copy skills from source platform to target platforms so all platforms share skills.

    This writes into other tools' directories, so callers are expected to run `dry_run`
    first and show the user what it would touch.

    dry_run=True  -> list of (source_path, destination_path, target_platform), nothing written
    dry_run=False -> (copied_labels, failures) where failures is [(destination, error), ...]
    """
    import shutil

    if target_platforms is None:
        target_platforms = [p for p in PLATFORM_PATHS.keys() if p != source_platform]

    source_dir = PLATFORM_PATHS.get(source_platform, {}).get("path")
    if not source_dir or not source_dir.exists():
        return [] if dry_run else ([], [])

    source_skills = sorted((e for e in source_dir.iterdir()
                            if e.is_dir() and not e.name.startswith(".")),
                           key=lambda p: p.name.lower())
    if names is not None:
        wanted = set(names)
        source_skills = [e for e in source_skills if e.name in wanted]

    planned = []
    for target_name in target_platforms:
        target_dir = PLATFORM_PATHS.get(target_name, {}).get("path")
        if not target_dir:
            continue
        for s_folder in source_skills:
            dest = target_dir / s_folder.name
            if not dest.exists():
                planned.append((s_folder, dest, target_name))

    if dry_run:
        return planned

    copied, failed = [], []
    for s_folder, dest, target_name in planned:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(s_folder, dest)
            copied.append(f"{s_folder.name} -> {target_name}")
        except Exception as e:
            failed.append((str(dest), str(e)))
    return copied, failed


if __name__ == "__main__":
    print("=== Multi-Platform AI Skills Scan ===")
    scanned = scan_all_platforms()
    for platform, skills in scanned.items():
        info = PLATFORM_PATHS[platform]
        print(f"\n{info['icon']} {platform} ({len(skills)} skills) - Path: {info['path']}")
        for s in skills:
            md_flag = "✓ SKILL.md" if s["has_skill_md"] else "  "
            print(f"   └─ {s['name']:<25} {md_flag} | {s['description']}")
