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
`doctor` or `merge` when reading the output programmatically. When Python itself may be
missing, go through the launcher instead: `scripts\launch.cmd sync.py <subcommand>` on
Windows, `scripts/launch.sh sync.py <subcommand>` elsewhere.

## On invocation: open the menu

When invoked without a specific request — `/skill-sync` with no arguments, "open
skill-sync", "sync my skills" — **launch the menu in a new terminal window and stop
there**. Do not run `doctor`, `status` or anything else first; the menu shows all of that.
A bare `/skill-sync` always points back to the menu, even if a subcommand ran earlier in
the same session. Run individual subcommands only when the message asks for that specific
thing ("push my skills", "what's out of sync?"), or when diagnosing a failure.

**Use this skill's own folder** — the directory this SKILL.md sits in — not a hardcoded
path. Depending on how it was installed the skill lives in `~/.claude/skills/skill-sync`,
`~/.agents/skills/skill-sync` (what `npx skills add` uses, symlinked into the clients) or
`~/.gemini/config/skills/skill-sync`, so a fixed path is wrong about as often as it is right.

**Spawn the launcher, not `python`.** `scripts/launch.cmd` and `scripts/launch.sh` find a
Python that actually runs and install one for the user if there is none. Calling `python
menu.py` directly is what used to fail silently: on a clean Windows `python` resolves to
the Microsoft Store stub, which opens the Store and runs nothing, so the console closed
with no message.

Windows — **always cmd, never PowerShell** (`Start-Process` only spawns the window):

```
Start-Process cmd -ArgumentList '/K','"<this skill folder>\scripts\launch.cmd"'
```

macOS/Linux (Bash tool) — swap `x-terminal-emulator` for the terminal the user has
(`gnome-terminal`, `konsole`, `xterm`, or `open -a Terminal.app` on macOS):

```
nohup x-terminal-emulator -e "sh '<this skill folder>/scripts/launch.sh'" >/dev/null 2>&1 &
```

`menu.py` refuses to run inside tool calls and the `!` prefix, which have no real TTY; a
spawned process gets a real console. Claude cannot drive the menu — use the subcommands
below instead. Everything the menu does is available as one.

## Commands

| Command | Purpose |
|---|---|
| `setup --remote <remote:> --categories work,school,personal` | one-time configuration on each computer |
| `setup --git <url> [--git-branch main]` | use a git repository instead of a cloud remote |
| `pack <action>` | bundles of skills deployed into one project or one client (see "Packs") |
| `usage scan` / `usage show` | which skills actually get used in which project (see "Usage") |
| `status` | what is local-only, remote-only, newer, or conflicting |
| `push [skill...]` | upload changed skills (`--dry-run`, `--force`, `--no-scan`, `--budget <seconds>`, `--threads <N>`) |
| `pull [category...]` | download; bare `pull` lists what the remote has (`--skills`, `--dest`, `--force`, `--threads <N>`) |
| `confirm-new` | y/n prompt per never-synced skill (see "New skills ask first") |
| `categorize <skill> <category...>` | set a skill's groups; `--add` / `--remove` change membership without touching the rest. A group may nest: `work/acme` |
| `update` | update skill-sync itself from GitHub, backing up the current version (`--check`, `--force`) |
| `place <skill> <client...>` | copy or symlink a skill into another client's folder — `claude`, `gemini`, `agents`/`cursor`/`antigravity`/`opencode` (`--dest`, `--force`, `--symlink`) |
| `merge <skill>` | Markdown diff and merge conflicts between local and remote |
| `resolve <skill> --keep local\|remote` | settle a conflict, backing up the losing side |
| `prune [--yes] [--only ...]` | the only command that deletes on the remote |
| `doctor` | diagnose rclone, config, skill directories and hooks |

Exit codes: `0` fine, `1` error or blocked, `2` the user must choose something.

Two helpers live outside `sync.py`:
`python scripts/provision.py rclone` installs rclone without admin rights (`which` reports
what is present), and `scripts/launch.cmd` / `scripts/launch.sh` do the same for Python.

## First run on a computer

**Open the menu and let the user drive it.** With nothing configured it goes straight into
Setup, which lists the providers, offers to install rclone if it is missing, creates the
groups and does the first upload. That is one screen instead of the four steps below, and it
needs no credentials in a tool call.

`scripts/install.py` does the same thing unattended, but it configures cloud storage and
registers hooks, so do not run it on the user's behalf without being asked to.

By hand, when the menu is not an option:

1. `python scripts/sync.py doctor` — confirms whether rclone and a config exist.
2. If rclone is missing, run `python scripts/provision.py rclone`. It tries winget or
   Homebrew, and falls back to the official checksum-verified build from
   `downloads.rclone.org` into `~/.claude/skill-sync/bin` — so a machine with no admin
   rights and no package manager still works. Then the user runs `rclone config`
   **themselves** — it is interactive, so Claude cannot drive it (in Claude Code they can
   prefix it with `!`). Per-provider steps: `references/providers.md`. If they say rclone
   is already installed, they are probably right: a terminal opened before the install
   keeps the old PATH, and `doctor` reports the path it found off-PATH.
3. Ask which categories they want, then
   `python scripts/sync.py setup --remote <remote:> --categories work,school,personal`.
   Do not invent categories. For a git repo instead: `setup --git <url>`.
4. Recommended: `python scripts/install_hooks.py`, so changed skills upload automatically
   at the end of every session.

On a **second computer**: same steps, then `pull` to list the categories and
`pull <category>` to bring down what belongs on that machine. Restart Claude Code
afterwards so the new skills are discovered.

## Packs

A pack is a **named list of skill names** — no folders, nothing moved on the remote — that
gets deployed into one project or one client. It is the answer to "set this new project up
with everything I use for web work", and to "give Claude this bundle but not Cursor".

```
pack list                          every pack, its size, what it inherits
pack show web                      the skills it resolves to, and which are missing here
pack create web --skills web-builder impeccable dataviz [--group work]
pack create acme --extends web --skills mapa_proyecto     # inherits web's skills
pack create acme --from-project C:\dev\acme               # seed from what is already there
pack add|rm web <skill>...         edit membership          pack delete web
pack apply web --project C:\dev\newsite [--client claude] [--link] [--force]
pack apply web --client gemini     machine-wide, no project
pack move web --from cursor --to claude --project C:\dev\acme
pack remove web --project C:\dev\acme          uninstall (backs up, does not erase)
pack where [--project P]           which packs are deployed here
pack publish / pack fetch          share the definitions through the remote
```

- **Destination = root x client.** `--project` picks the root (omit it for machine-wide),
  `--client` picks the subfolder: `claude` → `.claude/skills`, `gemini` → `.gemini/skills`,
  `agents`/`cursor`/`antigravity`/`opencode` → `.agents/skills`. That is what makes "move
  this bundle to Claude only" possible instead of syncing everything everywhere.
- **`extends`** lets project packs share a base: change `web` once, `pack apply` propagates
  it to every project that inherits from it. Circular inheritance is rejected.
- A skill in the pack that is **not on this machine is downloaded from the remote** straight
  into the destination (`--no-pull` disables that).
- `--copy` (default) leaves a self-contained copy that can be committed with the project;
  `--link` makes a junction/symlink to the master copy, so the project follows later edits.
  Junctions need no admin rights on Windows.
- Every deployment writes `<dest>/.skill-pack.json`, which is what `where`, `remove` and a
  later re-`apply` read. `remove` leaves alone anything another applied pack still needs.
- Pack definitions live in `~/.claude/skill-sync/config.json` and travel through the
  remote's `manifest.json` via `publish` / `fetch`.
- Applying into a `.agents/skills` folder under the current directory puts the copies in a
  folder `push` scans, so `confirm-new` may offer to upload them; the command warns when
  that is the case. `.claude/skills` inside a project is never scanned.

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
- **A group can nest**, up to four levels: `work/acme`, `work/quimera`. Each segment is one
  folder on the remote, and `pull work` brings down everything under `work/` while
  `pull work/acme` brings only that one. Changing a skill's *primary* nested group moves its
  folder on the remote, so prefer packs for per-project bundles and keep nesting for
  storage. Groups are for where a skill lives; packs are for what a project needs.
- A downloaded skill returns to the folder where this machine already keeps it, so a skill
  installed under `~/.gemini` is never duplicated into `~/.claude`.
- **skill-sync does not sync itself** — it would be uploading the tool mid-upload, and a
  pull could replace the running code underneath it. It is excluded from `push`, `pull` and
  the Stop hook, and updates from GitHub via `update`.

## Usage

`usage` records which skills are actually invoked in which project, so a pack can be built
from evidence rather than from memory:

```
usage scan [--full] [--budget-mb 40]     record what is new (the Stop hook does this)
usage show [--project <folder>] [--top N]
pack create acme --from-usage --from-project C:\dev\acme [--top 8]
```

- The signal is mined from Claude Code's own transcripts in `~/.claude/projects/`, reading
  only the bytes appended since the last scan. State lives in `state.json` under `usage`.
- Only skills installed on this machine (or named in a pack) are counted, which is what
  drops `/model`, `/clear` and the rest of the CLI commands without a denylist.
- Projects are keyed by the `cwd` recorded in the transcript, not the folder slug.
- The Stop hook scans within an 8 MB budget and never blocks a session; a scan stops at the
  last complete line, so the session being closed is counted next time.
- `usage scan --full` rebuilds the counts from every transcript still on disk. History whose
  transcript is gone is not recovered, so it is a repair tool, not a routine one.
- **Expect thin data at first.** It only counts explicit invocations from the moment
  scanning starts, so treat a fresh `usage show` as a floor, not a verdict.

## A git repository as the provider

`setup --git <url>` keeps the skills in an ordinary git repo instead of a cloud remote —
one commit per sync, full history, any host. git is not an rclone backend, so it works the
other way round: the repo is cloned to `~/.claude/skill-sync/gitremote/<repo>/`, that clone
*is* the rclone remote, and every command that reads the remote pulls first while every
command that writes it commits and pushes. The clone gets a `.gitignore` for `.trash/`.

- **Credentials are never prompted for** (`GIT_TERMINAL_PROMPT=0`), so nothing can hang a
  session. Clone the repo by hand once to set them up; until then setup reports the failure.
- A failed push is not data loss: the commit is already local, and the command prints the
  exact `git -C <clone> push` to rerun.
- `doctor` reports the repo, the branch, uncommitted changes and unpushed commits.

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
  changing, then runs the same check `hook-stop` does. It is **opt-in**: nothing installs
  it unless the user asks, including `install.py`, which needs `--watch`. An autostart
  entry is the most malware-shaped thing here, so it never appears as a side effect.
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
- `scripts/launch.cmd`, `scripts/launch.sh` — find or install Python, then run a script.
- `scripts/get_python.ps1` — pinned, checksum-verified Python download (Windows fallback).
- `scripts/provision.py` — installs rclone, package manager first, verified download second.
- `scripts/platform_scanner.py` — detects installed AI clients and their folders.
- `scripts/selftest.py`, `scripts/crud_test.py`, `scripts/edge_test.py` — test suites.
- `references/providers.md` — rclone setup per provider, bootstrapping a new machine.
- `references/troubleshooting.md` — errors, conflicts, recovery from `.trash`.
- `SECURITY.md` — every behaviour a scanner flags, the hosts contacted, and how to switch
  each one off. Point users here when they ask about the risk rating.
