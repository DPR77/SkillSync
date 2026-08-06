---
name: skill-sync
description: Sync AI skills between computers and between AI clients (Claude Code, Gemini, Antigravity, Cursor, OpenCode) through a cloud provider (Google Drive, Dropbox, OneDrive, S3, Box, WebDAV or any rclone remote), organised into categories such as work, school and personal. Use when the user wants to upload, back up, share or download their skills, set up skills on a new or second computer, copy a skill into another AI client or tool, keep skills in sync automatically, organise skills into folders, groups or categories, or asks about skill-sync, "my skills on another machine", "back up my skills", or "get my skills here".
license: MIT
---

<div align="center">

<img src="https://raw.githubusercontent.com/DPR77/SkillSync/main/references/images/logo.png" alt="skill-sync" width="520">

**your skills, on every machine you work from**

Desarrollado por **[GTI Santander](https://gtisantander.com)**

Install a skill once. Find it on the laptop, the desktop, and in whichever AI client you open next — Claude Code, Gemini, Antigravity, Cursor, OpenCode.

[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-3776ab)](https://www.python.org/)
[![Storage](https://img.shields.io/badge/storage-rclone-4a90d9)](https://rclone.org)
[![Tests](https://img.shields.io/badge/tests-153%20checks-brightgreen)](scripts/selftest.py)

</div>

---

> **Desarrollado por [GTI Santander](https://gtisantander.com)**

## Before / After

<table>
<tr><th>By hand</th><th>With skill-sync</th></tr>
<tr valign="top"><td>

```text
1. remember which skills you changed
2. find them in ~/.claude/skills
3. zip, upload, download on the laptop
4. unzip over the top, hope nothing
   older overwrote something newer
5. repeat for ~/.gemini, .agents, …
```

No record of which side is newer.  
Overwrites are silent and permanent.

</td><td>

```console
$ python scripts/sync.py status

work (3)
  web-builder      in-sync
  seo-audit        local-newer
  grill-me         remote-only

$ python scripts/sync.py push
```

Content-hashed, so it knows which side moved.  
Nothing is overwritten without a backup.

</td></tr>
</table>

---

## On invocation: open the menu

When this skill is invoked without a specific request — `/skill-sync` with no arguments,
"abre skill-sync", "sync my skills" — **launch the menu in a new terminal window, and stop
there**. Claude Code's own `!` prefix and tool calls have no real TTY, so `menu.py` refuses
to run inside them — it needs an actual console window, which a spawned process gets.

Windows — **always cmd, never PowerShell**:
```
Start-Process cmd -ArgumentList '/K','python %USERPROFILE%\.claude\skills\skill-sync\scripts\menu.py'
```
(`Start-Process` is only how the window is spawned; the menu itself must run under `cmd`.)

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
    manifest.json          every skill's groups, content hash, size and machine
    work/<skill>/...
    school/<skill>/...
    personal/<skill>/...
```

**Groups are labels, not folders.** A skill can be in several at once - `caveman` can be
both `work` and `personal`. Only the first group decides which folder physically holds it
on the remote, so adding a group moves nothing. `manifest.json` is read once per command
and written with a read-merge-write, so a concurrent machine's entries survive.

A downloaded skill goes back to the folder where this machine already keeps it, so a skill
installed under `~/.gemini` is never duplicated into `~/.claude`.

**skill-sync does not sync itself.** It would be uploading the tool mid-upload, and a pull
could replace the running code underneath it. It is excluded from `push`, `pull` and the
Stop hook, and updates from GitHub instead - see `update` below.

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
| `push [skill...]` (`--json`) | upload changed skills (`--dry-run`, `--force`, `--no-scan`, `--budget <seconds>`, `--threads <N>`) |
| `pull [category...]` | download; bare `pull` lists what the remote has (`--skills`, `--dest`, `--force`, `--threads <N>`) |
| `categorize <skill> <category...>` | set a skill's groups; `--add` / `--remove` change membership without touching the rest |
| `update` (`--check`, `--force`) | update skill-sync itself from GitHub, backing up the current version |
| `place <skill> <client...>` (`--dest`, `--force`, `--symlink`) | copy or symlink a local skill into another AI client's skills folder (`claude`, `gemini`, `agents`/`cursor`/`antigravity`/`opencode`) |
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

- **Ask, do not guess, about groups.** When `status` shows skills with no group, ask which
  one each belongs to (AskUserQuestion, offering the groups already in the config plus a
  new one), then run `categorize` for each. Only pass `--assume-default` when the user
  explicitly wants everything in the default group. A skill may belong to several groups:
  use `--add` when they want one more, not a replacement.
- **Never resolve a conflict on the user's behalf.** Report which skill conflicts, when and
  from which machine the remote copy came (`status --json` has `remote_updated` and
  `remote_machine`), and let them pick `--keep local` or `--keep remote`.
- **Never run `prune --yes` unprompted.** It deletes for every machine, including skills that
  only exist on a computer that is currently offline. Show the list from a bare `prune` first.
- **Do not bypass the credential scan.** If `push` reports possible credentials, show the
  reported `file:line` and let the user decide. Documentation placeholders are already
  ignored, so a hit is worth reading. For a genuine false positive prefer a
  `skill-sync: allow-secret` comment on that line over `--no-scan`, which drops the check
  for the whole push.
- Skills over ~20 MB upload slowly. Suggest a `.skillignore` inside the skill for large
  assets rather than excluding it globally.

## Automatic sync

`install_hooks.py` registers two hooks in `~/.claude/settings.json`:

- **Stop** → `hook-stop`: at the end of a session, compares a cheap mtime/file-count
  signature per skill and uploads only what changed. Silent when nothing did. It works to
  a 90s budget, smallest skill first, so closing a session is never held up; whatever does
  not fit goes up next time. Raise it with `"hook_budget_seconds": 240` in
  `~/.claude/skill-sync/config.json`.
- **SessionStart** → `hook-session-start`: at most once every 6 hours, prints one line if
  the remote has skills this machine lacks or newer versions from another machine.

Both no-op until `setup` has run and never fail a session. `python scripts/install_hooks.py
--uninstall` removes them.

## Safety model

- Uploads use `rclone sync` **scoped to one skill folder**, guarded by hash comparison, with
  `--backup-dir` so replaced files land in `<remote>/.trash/<timestamp>/` instead of vanishing.
- Downloads back up replaced files to `~/.claude/skill-sync/trash/<timestamp>/`.
- Those backups, and the conflict copies, are deleted after 30 days (`KEEP_TRASH_DAYS`) so
  they cannot grow into the user's cloud quota forever. `doctor` reports how much is held.
- A conflict (both sides changed since the last sync) blocks the transfer; nothing is
  overwritten until the user runs `resolve`.
- `manifest.json` is re-read and merged per skill before every write, so a machine that
  synced in the meantime does not lose its entry.
- The remote `.trash` sweep runs once a day and clears a few folders at a time, so cleanup
  never holds up a sync.
- An interrupted push records whatever already reached the remote, so the next run resumes
  instead of re-uploading everything.
- Runtime state (`~/.claude/skill-sync/`) is deliberately outside `~/.claude/skills/` so it is
  never uploaded and never seen as a skill.

## Files

- `scripts/sync.py` — all sync logic.
- `scripts/menu.py` — interactive ASCII menu (needs a real terminal).
- `scripts/install.py` — one-shot installer: rclone, a default remote, categories, hooks.
- `scripts/install_hooks.py` — installs/removes the hooks, with a settings.json backup.
- `scripts/platform_scanner.py` — detects which AI clients are installed and their folders.
- `scripts/selftest.py` — end-to-end test against a local folder used as the remote.
- `references/providers.md` — rclone setup per provider, bootstrapping a new machine.
- `references/troubleshooting.md` — errors, conflicts, recovery from `.trash`.
