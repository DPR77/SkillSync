# skill-sync internals

Detail behind `SKILL.md`. Read the section you need.

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
  the Stop hook. It is updated by reinstalling it, never by downloading code itself.

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

- The next `hook-stop` opens a separate console running
  `sync.py confirm-new`: one y/n prompt per new skill, with its description. Yes pushes it;
  no — or ignoring the window — means it will not ask again unless the skill changes.
- Run it by hand anytime: `python scripts/sync.py confirm-new`.

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
- `scripts/launch.cmd`, `scripts/launch.sh` — find or install Python, then run a script.
- `scripts/get_python.ps1` — pinned, checksum-verified Python download (Windows fallback).
- `scripts/provision.py` — installs rclone, package manager first, verified download second.
- `scripts/platform_scanner.py` — detects installed AI clients and their folders.
- `scripts/selftest.py`, `scripts/crud_test.py`, `scripts/edge_test.py` — test suites.
- `references/providers.md` — rclone setup per provider, bootstrapping a new machine.
- `references/troubleshooting.md` — errors, conflicts, recovery from `.trash`.
- `SECURITY.md` — every behaviour a scanner flags, the hosts contacted, and how to switch
