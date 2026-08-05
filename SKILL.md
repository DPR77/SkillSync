---
name: skill-sync
description: Sync Claude Code skills between computers through a cloud provider (Google Drive, Dropbox, OneDrive, S3, Box, WebDAV or any rclone remote), organised into categories such as work, school and personal. Use when the user wants to upload, back up, share or download their skills, set up skills on a new or second computer, keep skills in sync automatically, organise skills into folders or categories, or asks about skill-sync, "my skills on another machine", "back up my skills", or "get my skills here".
license: MIT
---

# skill-sync

> **Desarrollado por GTI Santander**

## On invocation: open the menu

When this skill is invoked without a specific request — `/skill-sync` with no arguments,
"abre skill-sync", "sync my skills" — **launch the menu in a new terminal window, and stop
there**. Claude Code's own `!` prefix and tool calls have no real TTY, so `menu.py` refuses
to run inside them — it needs an actual console window, which a spawned process gets.

Windows (PowerShell tool):
```
Start-Process powershell -ArgumentList '-NoExit','-Command','python $env:USERPROFILE\.claude\skills\skill-sync\scripts\menu.py'
```

macOS/Linux (Bash tool):
```
nohup x-terminal-emulator -e "python3 ~/.claude/skills/skill-sync/scripts/menu.py" >/dev/null 2>&1 &
```
(swap `x-terminal-emulator` for whatever terminal the user has — `gnome-terminal`, `konsole`,
`xterm`, or on macOS `open -a Terminal.app`.)

Do not run `doctor`, `status` or anything else first. The menu shows all of that, and it
is what the user came for. This holds even if `status`, `push`, or another subcommand ran
earlier in the same session — a bare `/skill-sync` always points back to the menu, it
never repeats the last subcommand. Run individual subcommands only when the current
message asks for that specific thing ("push my skills", "what's out of sync?"), or when
diagnosing a failure.

Skills live in standard skill locations across the PC (`~/.claude/skills/<name>/`, `~/.gemini/config/skills/<name>/`, `.agents/skills/<name>/`). This skill automatically discovers all skills installed anywhere on the system and keeps them synced to a cloud remote grouped by category:

```
<remote>:ClaudeSkills/
    manifest.json          which skill is in which category, plus content hashes
    work/<skill>/...
    school/<skill>/...
    personal/<skill>/...
```

Every command is `python scripts/sync.py <subcommand>` run from this skill's directory.
Use `python3` on macOS/Linux if `python` is not on PATH.

## Interactive menu

`python scripts/menu.py` opens a full-screen ASCII UI: checkbox skill lists, arrow-key
navigation, live filter, colour-coded states, and every action behind one screen.

**Claude cannot drive it** — tool calls and `!` have no interactive stdin. Launch it in a
spawned terminal window instead (see "On invocation" above) — that process gets a real
console, so the menu works there.

Keys: arrows move, `space` toggles, `a`/`n`/`i` select all/none/invert, `/` filters,
`enter` confirms, `esc` goes back, `q` quits, `r` refreshes. Flags: `--ascii` (no
box-drawing characters), `--no-color`. Everything the menu does is also available as the
non-interactive subcommands below — use those yourself.

## Commands

| Command | Purpose |
|---|---|
| `setup --remote <remote:> --categories work,school,personal` | one-time configuration on each computer |
| `status` (`--json`) | what is local-only, remote-only, newer, or conflicting across the PC |
| `push [skill...]` (`--json`) | upload changed skills (`--dry-run`, `--force`, `--no-scan`, `--json`) |
| `pull [category...]` | download; bare `pull` lists what the remote has (`--skills`, `--dest`, `--force`) |
| `categorize <skill> <category>` | assign a category, moving it on the remote too |
| `place <skill> <client...>` (`--dest`, `--force`) | copy a local skill into another AI client's skills folder (`claude`, `gemini`, `agents`/`cursor`/`antigravity`/`opencode`) — purely local, no remote involved |
| `merge <skill>` (`--json`) | inspect Markdown diff and merge conflicts between local and remote |
| `resolve <skill> --keep local\|remote` | settle a conflict, backing up the losing side |
| `prune [--yes] [--only ...]` | the only command that deletes on the remote |
| `doctor` (`--json`) | diagnose rclone, config, system skill directories and hooks |

Exit codes: `0` fine, `1` error or blocked, `2` the user must choose something.

## First run on a computer

1. `python scripts/sync.py doctor` — confirms whether rclone and a config exist.
2. If rclone is missing, tell the user to install it and to run `rclone config` **themselves**
   (it is interactive, so it cannot be driven by Claude — in Claude Code they can prefix it
   with `!`). Provider-specific steps: `references/providers.md`.
3. `python scripts/sync.py setup --remote <remote:> --categories work,school,personal`.
   Ask the user which categories they want before running it; do not invent them.
4. Optional but recommended: `python scripts/install_hooks.py` so changed skills upload
   automatically at the end of every session.

On a **second computer** the same steps apply, then `python scripts/sync.py pull` to see
the categories and `pull <category>` to bring down only what belongs on that machine.
Restart Claude Code afterwards so new skills are discovered.

## Working rules

- **Ask, do not guess, about categories.** When `status` reports skills with category `-`,
  ask the user which category each belongs to (AskUserQuestion, offering the categories
  already in the config plus a new one), then run `categorize` for each. Only pass
  `--assume-default` when the user explicitly wants everything in the default category.
- **Never resolve a conflict on the user's behalf.** Report which skill conflicts, when and
  from which machine the remote copy came (`status --json` has `remote_updated` and
  `remote_machine`), and let them pick `--keep local` or `--keep remote`.
- **Never run `prune --yes` unprompted.** It deletes for every machine, including skills that
  only exist on a computer that is currently offline. Show the list from a bare `prune` first.
- **Do not bypass the credential scan.** If `push` reports possible credentials, show the
  file and line to the user and let them decide; `--no-scan` is theirs to ask for.
- Skills over ~20 MB upload slowly. Suggest a `.skillignore` inside the skill for large
  assets rather than excluding it globally.

## Automatic sync

`install_hooks.py` registers two hooks in `~/.claude/settings.json`:

- **Stop** → `hook-stop`: at the end of a session, compares a cheap mtime/file-count
  signature per skill and uploads only what changed. Silent when nothing did.
- **SessionStart** → `hook-session-start`: at most once every 6 hours, prints one line if
  the remote has skills this machine lacks or newer versions from another machine.

Both no-op until `setup` has run and never fail a session. `python scripts/install_hooks.py
--uninstall` removes them.

## Safety model

- Uploads use `rclone sync` **scoped to one skill folder**, guarded by hash comparison, with
  `--backup-dir` so replaced files land in `<remote>/.trash/<timestamp>/` instead of vanishing.
- Downloads back up replaced files to `~/.claude/skill-sync/trash/<timestamp>/`.
- A conflict (both sides changed since the last sync) blocks the transfer; nothing is
  overwritten until the user runs `resolve`.
- `manifest.json` is re-read and merged per skill before every write, so two machines
  syncing at once do not clobber each other's entries.
- Runtime state (`~/.claude/skill-sync/`) is deliberately outside `~/.claude/skills/` so it is
  never uploaded and never seen as a skill.

## Files

- `scripts/sync.py` — all sync logic.
- `scripts/menu.py` — interactive ASCII menu (needs a real terminal).
- `scripts/install_hooks.py` — installs/removes the hooks, with a settings.json backup.
- `scripts/selftest.py` — end-to-end test against a local folder used as the remote.
- `references/providers.md` — rclone setup per provider, bootstrapping a new machine.
- `references/troubleshooting.md` — errors, conflicts, recovery from `.trash`.
