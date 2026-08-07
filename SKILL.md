---
name: skill-sync
description: Sync AI skills between computers and between AI clients (Claude Code, Gemini, Antigravity, Cursor, OpenCode) through a cloud provider (Google Drive, Dropbox, OneDrive, S3, Box, WebDAV or any rclone remote), organised into categories such as work, school and personal. Use when the user wants to upload, back up, share or download their skills, set up skills on a new or second computer, copy a skill into another AI client or tool, keep skills in sync automatically, organise skills into folders, groups or categories, or asks about skill-sync, "my skills on another machine", "back up my skills", or "get my skills here".
license: MIT
---

# skill-sync

Keeps the skills installed anywhere on this machine (`~/.claude/skills/<name>/`,
`~/.gemini/config/skills/<name>/`, `.agents/skills/<name>/`) synced to a cloud remote,
grouped by category.

Every command is `python scripts/sync.py <subcommand>`, run from this skill's directory
(`python3` on macOS/Linux if `python` is not on PATH). Add `--json` to `status`, `push`,
`doctor` or `merge` when reading the output programmatically.

## On invocation: open the menu

When invoked without a specific request — `/skill-sync` with no arguments, "open
skill-sync", "sync my skills" — **launch the menu in a new terminal window and stop
there**. Do not run `doctor`, `status` or anything else first; the menu shows all of that.
A bare `/skill-sync` always points back to the menu, even if a subcommand ran earlier in
the same session. Run individual subcommands only when the message asks for that specific
thing ("push my skills", "what's out of sync?"), or when diagnosing a failure.

Windows — **always cmd, never PowerShell** (`Start-Process` only spawns the window):

```
Start-Process cmd -ArgumentList '/K','python %USERPROFILE%\.claude\skills\skill-sync\scripts\menu.py'
```

macOS/Linux (Bash tool) — swap `x-terminal-emulator` for the terminal the user has
(`gnome-terminal`, `konsole`, `xterm`, or `open -a Terminal.app` on macOS):

```
nohup x-terminal-emulator -e "python3 ~/.claude/skills/skill-sync/scripts/menu.py" >/dev/null 2>&1 &
```

`menu.py` refuses to run inside tool calls and the `!` prefix, which have no real TTY; a
spawned process gets a real console. Claude cannot drive the menu — use the subcommands
below instead. Everything the menu does is available as one.

## Commands

| Command | Purpose |
|---|---|
| `setup --remote <remote:> --categories work,school,personal` | one-time configuration on each computer |
| `status` | what is local-only, remote-only, newer, or conflicting |
| `push [skill...]` | upload changed skills (`--dry-run`, `--force`, `--no-scan`, `--budget <seconds>`, `--threads <N>`) |
| `pull [category...]` | download; bare `pull` lists what the remote has (`--skills`, `--dest`, `--force`, `--threads <N>`) |
| `confirm-new` | y/n prompt per never-synced skill (see "New skills ask first") |
| `categorize <skill> <category...>` | set a skill's groups; `--add` / `--remove` change membership without touching the rest |
| `update` | update skill-sync itself from GitHub, backing up the current version (`--check`, `--force`) |
| `place <skill> <client...>` | copy or symlink a skill into another client's folder — `claude`, `gemini`, `agents`/`cursor`/`antigravity`/`opencode` (`--dest`, `--force`, `--symlink`) |
| `merge <skill>` | Markdown diff and merge conflicts between local and remote |
| `resolve <skill> --keep local\|remote` | settle a conflict, backing up the losing side |
| `prune [--yes] [--only ...]` | the only command that deletes on the remote |
| `doctor` | diagnose rclone, config, skill directories and hooks |

Exit codes: `0` fine, `1` error or blocked, `2` the user must choose something.

## First run on a computer

1. `python scripts/sync.py doctor` — confirms whether rclone and a config exist.
2. If rclone is missing, tell the user to install it and to run `rclone config`
   **themselves** — it is interactive, so Claude cannot drive it (in Claude Code they can
   prefix it with `!`). Per-provider steps: `references/providers.md`.
3. Ask which categories they want, then
   `python scripts/sync.py setup --remote <remote:> --categories work,school,personal`.
   Do not invent categories.
4. Recommended: `python scripts/install_hooks.py`, so changed skills upload automatically
   at the end of every session.

On a **second computer**: same steps, then `pull` to list the categories and
`pull <category>` to bring down what belongs on that machine. Restart Claude Code
afterwards so the new skills are discovered.

## Working rules

- **Ask, do not guess, about groups.** When `status` shows skills with no group, ask which
  one each belongs to (AskUserQuestion, offering the groups already in the config plus a
  new one), then run `categorize`. Pass `--assume-default` only when the user explicitly
  wants everything in the default group. Use `--add` for one more group, not a replacement.
- **Never resolve a conflict on the user's behalf.** Report which skill conflicts, and when
  and from which machine the remote copy came (`status --json` carries `remote_updated` and
  `remote_machine`). Let them pick `--keep local` or `--keep remote`.
- **Never run `prune --yes` unprompted.** It deletes for every machine, including skills
  that only exist on a computer that is currently offline. Show a bare `prune` first.
- **Do not bypass the credential scan.** If `push` reports possible credentials, show the
  reported `file:line` and let the user decide. Documentation placeholders are already
  ignored, so a hit is worth reading. For a genuine false positive prefer a
  `skill-sync: allow-secret` comment on that line over `--no-scan`, which drops the check
  for the whole push.
- Skills over ~20 MB upload slowly. Suggest a `.skillignore` inside the skill rather than
  excluding it globally.

## Model

Remote layout:

```
<remote>:ClaudeSkills/
    manifest.json          every skill's groups, content hash, size and machine
    work/<skill>/...
    school/<skill>/...
    personal/<skill>/...
```

- **Groups are labels, not folders.** A skill can be in several at once. Only the first
  group decides which folder physically holds it on the remote, so adding a group moves
  nothing.
- A downloaded skill returns to the folder where this machine already keeps it, so a skill
  installed under `~/.gemini` is never duplicated into `~/.claude`.
- **skill-sync does not sync itself** — it would be uploading the tool mid-upload, and a
  pull could replace the running code underneath it. It is excluded from `push`, `pull` and
  the Stop hook, and updates from GitHub via `update`.

## Automatic sync

`install_hooks.py` registers two hooks in `~/.claude/settings.json`:

- **Stop** → `hook-stop`: compares a cheap mtime/file-count signature per skill. A skill
  that has synced before and changed gets pushed, to a 90s budget, smallest first, so
  closing a session is never held up; whatever does not fit goes up next time. Raise it
  with `"hook_budget_seconds": 240` in `~/.claude/skill-sync/config.json`.
- **SessionStart** → `hook-session-start`: at most once every 6 hours, prints one line if
  the remote has skills this machine lacks or newer versions from another machine.

Both no-op until `setup` has run and never fail a session. `--uninstall` removes them.

### New skills ask first

A skill with no entry in `state.json` has never synced anywhere, and is never uploaded
silently:

- `install_watch.py` starts `watch_new_skills.py` at login (Startup folder on Windows, a
  LaunchAgent on macOS, a systemd `--user` unit or XDG autostart entry on Linux — no admin
  or root anywhere). It polls every few seconds, waits for a new skill's files to stop
  changing, then runs the same check `hook-stop` does.
- That watcher, or the next `hook-stop`, opens a separate console running
  `sync.py confirm-new`: one y/n prompt per new skill, with its description. Yes pushes it;
  no — or ignoring the window — means it will not ask again unless the skill changes.
- Run it by hand anytime: `python scripts/sync.py confirm-new`.
- `python scripts/install_watch.py --uninstall` removes the watcher; the Stop hook still
  covers new skills on the next session either way.

## Safety model

- Uploads use `rclone sync` **scoped to one skill folder**, guarded by hash comparison,
  with `--backup-dir` so replaced files land in `<remote>/.trash/<timestamp>/`.
- Downloads back up replaced files to `~/.claude/skill-sync/trash/<timestamp>/`.
- Those backups and the conflict copies are deleted after 30 days (`KEEP_TRASH_DAYS`), so
  they cannot grow into the user's cloud quota. `doctor` reports how much is held.
- A conflict (both sides changed since the last sync) blocks the transfer; nothing is
  overwritten until the user runs `resolve`.
- `manifest.json` is re-read and merged per skill before every write, so a machine that
  synced in the meantime does not lose its entry.
- The remote `.trash` sweep runs once a day, a few folders at a time.
- An interrupted push records whatever already reached the remote and resumes next time.
- Runtime state (`~/.claude/skill-sync/`) sits outside `~/.claude/skills/`, so it is never
  uploaded and never seen as a skill.

## Files

- `scripts/sync.py` — all sync logic.
- `scripts/menu.py` — interactive terminal menu (needs a real TTY).
- `scripts/install.py` — one-shot installer: rclone, a default remote, categories, hooks.
- `scripts/install_hooks.py` — installs/removes the session hooks.
- `scripts/install_watch.py`, `scripts/watch_new_skills.py` — new-skill watcher.
- `scripts/platform_scanner.py` — detects installed AI clients and their folders.
- `scripts/selftest.py`, `scripts/crud_test.py`, `scripts/edge_test.py` — test suites.
- `references/providers.md` — rclone setup per provider, bootstrapping a new machine.
- `references/troubleshooting.md` — errors, conflicts, recovery from `.trash`.
