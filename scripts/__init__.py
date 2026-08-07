"""skill-sync — sync AI agent skills across machines and clients.

The scripts in this folder are meant to be run directly from the skill directory
(`python scripts/sync.py ...`), which is how the AI clients load them. This file only
exists so the same folder can also be installed as the `skill_sync` package, giving the
`skill-sync*` console commands declared in pyproject.toml.
"""

__all__ = ["sync", "menu", "install", "install_hooks", "install_watch", "platform_scanner"]
