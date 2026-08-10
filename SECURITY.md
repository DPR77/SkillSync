# Security

skill-sync copies files between your machine and your own cloud storage, and it installs
the one binary it needs to do that. Automated scanners rate it **medium risk**, and they
are not wrong to: the same behaviours — start at login, reach the network, run an
installer, edit an agent's settings file — are what a credential stealer does. The rating
is about the *shape* of the code, not about evidence of anything malicious.

This file is the honest inventory, so you can decide for yourself instead of trusting a
badge. Everything below is greppable in `scripts/`.

## What it does that scanners flag

### 1. It can start itself at login

`scripts/install_watch.py` registers a login item so a newly added skill gets noticed in
seconds rather than at the end of your next Claude Code session:

| OS | What it creates |
|---|---|
| Windows | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\skill-sync-watch.vbs` |
| macOS | `~/Library/LaunchAgents/com.skill-sync-watch.plist` |
| Linux | `~/.config/systemd/user/skill-sync-watch.service`, or an XDG autostart entry |

Persistence is the single most malware-shaped thing here, so it is **opt-in**. It is not
installed by `npx skills add`, and since v2.4.0 `install.py` only does it when you pass
`--watch`. Nothing needs root or administrator rights, and the watcher only ever runs
`sync.py`'s own new-skill check.

Remove it: `python scripts/install_watch.py --uninstall`. The Stop hook still covers new
skills on your next session.

### 2. It writes to Claude Code's settings

`scripts/install_hooks.py` adds two entries to `~/.claude/settings.json` — a `Stop` hook
that uploads changed skills, and a `SessionStart` hook that prints one line when the
remote is ahead. It edits only the `hooks` key and leaves the rest of the file alone.

Remove them: `python scripts/install_hooks.py --uninstall`.

### 3. It reaches the network

Every host skill-sync itself contacts, and why:

| Host | Why | Where |
|---|---|---|
| `api.github.com`, `raw.githubusercontent.com` | reads `VERSION` to tell you an update exists (once a day, cached) | `sync.py: fetch_latest_version` |
| `github.com` | downloads `main.zip` when you run `update` | `sync.py: cmd_update` |
| `downloads.rclone.org` | downloads rclone when you have no package manager | `provision.py` |
| `www.python.org` | downloads Python when the launcher finds none | `scripts/get_python.ps1` |
| your cloud provider | the actual sync, performed by rclone under credentials **you** configured with `rclone config` | `sync.py: rclone()` |

skill-sync never sees, stores or transmits your cloud credentials. They live in rclone's
own config file, and rclone is what talks to the provider.

The update check is the only unprompted network call. Turn it off with
`"update_check": false` in `~/.claude/skill-sync/config.json`.

### 4. It downloads and installs binaries

Two of them, both only when the tool is missing and you asked for it:

- **rclone** (`scripts/provision.py`) — a package manager first (`winget`, `brew`, both
  unprivileged). If there is none, the official build is fetched over HTTPS from
  `downloads.rclone.org`, and **the archive is checked against the SHA-256 the rclone
  project publishes for that release**. A mismatch aborts and installs nothing. Only the
  `rclone` executable is unpacked, into `~/.claude/skill-sync/bin`. No PATH is edited.
- **Python** (`scripts/get_python.ps1`, Windows only) — `winget --scope user` first. If
  there is no winget, the official embeddable build is fetched from `python.org` and
  checked against a **SHA-256 hard-coded in the script**, so the accepted build is fixed
  in source and reviewable in the diff. It is unpacked into
  `~/.claude/skill-sync/python` and used only to run skill-sync.

Neither ever calls `sudo`. Anything that needs root is printed for you to run yourself.

### 5. It runs subprocesses

`rclone`, `git`, and the package managers above. All are invoked as **argument lists,
never through a shell** — there is no `shell=True` anywhere in the codebase, and no
`eval`, `exec`, `pickle` or `__import__` of anything dynamic.

## What it deliberately does not do

- No telemetry, analytics or crash reporting. Nothing is sent anywhere except your remote.
- No credential prompts. `rclone config` and `git` authentication are yours to run;
  `GIT_TERMINAL_PROMPT=0` is set so nothing can silently block waiting for a password.
- No writes outside `~/.claude/skill-sync/`, your skills folders, and the paths listed
  above.
- No deletion on the remote except by explicit `prune`.

## Guards already in the code

- **Credential scan before upload** — `push` refuses to send a skill containing what looks
  like a key or token, and reports `file:line`. Override per line with a
  `skill-sync: allow-secret` comment; `--no-scan` drops the check for the whole push.
- **Zip-slip protection** — `update` and `provision` validate every archive member's path
  and extract them one at a time, resolved against the destination.
- **Download size caps** — 25 MB for updates, 128 MB for tool downloads.
- **HTTPS and host allowlist** — `provision.py` refuses any URL that is not HTTPS to
  `downloads.rclone.org`.
- **Backups instead of overwrites** — replaced files go to `.trash/<timestamp>/` locally
  and on the remote, swept after 30 days.
- **Conflicts block** — when both sides changed, nothing is transferred until you run
  `resolve`.

## Reducing the surface

If you want the smallest possible footprint:

```
python scripts/install_watch.py --uninstall     # no login item
python scripts/install_hooks.py --uninstall     # no automatic sync, run push/pull yourself
```

and set `"update_check": false` in `~/.claude/skill-sync/config.json`. skill-sync then
does nothing at all until you run a command.

## Reporting a problem

Open an issue at <https://github.com/DPR77/SkillSync/issues>. If it is a vulnerability
rather than a bug, say so in the title and leave the details out of the public thread.
