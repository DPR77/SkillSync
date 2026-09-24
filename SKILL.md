---
name: skill-sync
description: Sync AI skills between computers and between AI clients (Claude Code, Gemini, Antigravity, Cursor, OpenCode) through a cloud provider (Google Drive, Dropbox, OneDrive, S3, Box, WebDAV or any rclone remote), organised into categories such as work, school and personal. Use when the user wants to upload, back up, share or download their skills, set up skills on a new or second computer, copy a skill into another AI client or tool, keep skills in sync automatically, organise skills into folders, groups or categories, or asks about skill-sync, "my skills on another machine", "back up my skills", or "get my skills here".
license: MIT
---

# skill-sync

Keeps the skills installed on this machine (`~/.claude/skills/`, `~/.gemini/config/skills/`,
`.agents/skills/`) synced to a cloud remote, grouped by category.

Every command is `python scripts/sync.py <subcommand>` (`python3` on macOS/Linux), run from
**this skill's own folder** — the directory this SKILL.md sits in. It may be
`~/.claude/skills/skill-sync`, `~/.agents/skills/skill-sync` or
`~/.gemini/config/skills/skill-sync`, so never hardcode the path. Add `--json` to `status`,
`push`, `doctor` or `merge` to read output programmatically. If Python may be missing, go
through the launcher: `scripts\launch.cmd sync.py <subcommand>` (Windows) or
`scripts/launch.sh sync.py <subcommand>`.

## Bare invocation: open the menu and stop

`/skill-sync` with no request, "open skill-sync", "sync my skills" → launch the menu in a
new terminal window and do nothing else (no `doctor`, no `status`: the menu shows them).
Run subcommands only when the user asks for that specific thing or while diagnosing.

Spawn the launcher, never `python menu.py`: on a clean Windows `python` is the Store stub
and the window closes silently. The menu needs a real TTY, so Claude cannot drive it.

- Windows, always through cmd:
  `Start-Process cmd -ArgumentList '/K','"<skill folder>\scripts\launch.cmd"'`
- macOS/Linux (use the user's terminal: `gnome-terminal`, `konsole`, `xterm`, or
  `open -a Terminal.app`):
  `nohup x-terminal-emulator -e "sh '<skill folder>/scripts/launch.sh'" >/dev/null 2>&1 &`

## Commands

| Command | Purpose |
|---|---|
| `setup --remote <remote:> --categories a,b` | one-time configuration per computer |
| `setup --git <url> [--git-branch main]` | a git repository instead of a cloud remote |
| `status` | local-only, remote-only, newer, conflicting |
| `push [skill...]` | upload changes (`--dry-run`, `--force`, `--no-scan`, `--budget <s>`, `--threads <N>`) |
| `pull [category...]` | download; bare `pull` lists the remote (`--skills`, `--dest`, `--force`, `--threads <N>`) |
| `categorize <skill> <group...>` | set groups; `--add` / `--remove` change membership. Groups nest: `work/acme` |
| `confirm-new` | y/n prompt for each never-synced skill |
| `place <skill> <client...>` | copy or link into `claude`, `gemini`, `agents`/`cursor`/`antigravity`/`opencode` (`--dest`, `--force`, `--symlink`) |
| `merge <skill>` / `resolve <skill> --keep local\|remote` | inspect / settle a conflict (loser is backed up) |
| `pack <action>` | skill bundles deployed into one project or client |
| `usage scan` / `usage show` | which skills are actually used in which project |
| `prune [--yes] [--only ...]` | the only command that deletes on the remote |
| `doctor` | diagnose rclone, config, skill folders, hooks |

Exit codes: `0` ok, `1` error or blocked, `2` the user must choose something.

## First run on a computer

Open the menu: with nothing configured it goes straight into Setup (provider list, rclone
install, groups, first upload) and needs no credentials in a tool call. Do not run
`scripts/install.py` for the user unless asked — it configures storage and registers hooks.

By hand:

1. `sync.py doctor`.
2. No rclone → `python scripts/provision.py rclone` (winget/Homebrew, else a
   checksum-verified download into `~/.claude/skill-sync/bin`, no admin). The user then runs
   `rclone config` **themselves** (interactive; `!` prefix in Claude Code). Per provider:
   `references/providers.md`. "Already installed" is usually true: an old terminal keeps the
   old PATH, and `doctor` reports the off-PATH copy it found.
3. Ask which categories they want — never invent them — then `setup --remote ...`
   (or `setup --git <url>`).
4. Recommended: `python scripts/install_hooks.py` to push changed skills at session end.

Second computer: same steps, then `pull` to list and `pull <category>`; restart the client.

## Working rules

- **Ask about groups.** For ungrouped skills in `status`, ask (AskUserQuestion with the
  existing groups plus a new one), then `categorize`. `--assume-default` only when the user
  explicitly wants everything in the default group.
- **Never resolve a conflict for the user.** Report the skill and when/which machine wrote
  the remote copy (`status --json`: `remote_updated`, `remote_machine`); they pick the side.
- **Never run `prune --yes` unprompted.** It deletes for every machine, including offline
  ones. Show a bare `prune` first.
- **Never bypass the credential scan.** Show the reported `file:line` and let the user
  decide. For a real false positive prefer a `skill-sync: allow-secret` comment on that
  line over `--no-scan`.
- Skills over ~20 MB upload slowly: suggest a `.skillignore` inside that skill.
- skill-sync never syncs itself. To update it, reinstall it the way it was installed
  (`npx skills add DPR77/SkillSync` or `git pull` in its folder).

## More detail

`references/internals.md` — packs, remote layout and groups, usage counter, git provider,
hooks, safety model, file map.
`references/troubleshooting.md` — errors, conflicts, recovery from `.trash`.
`SECURITY.md` — everything a scanner flags and how to switch it off; point users here when
they ask about the risk rating.
