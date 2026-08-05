# Troubleshooting

Start with `python scripts/sync.py doctor`. It checks rclone, the config, whether the remote
is reachable, and whether the hooks are installed.

## States reported by `status`

| State | Meaning | Action |
|---|---|---|
| `in-sync` | local hash == remote hash | nothing |
| `local-only` | never uploaded | `categorize` then `push` |
| `remote-only` | exists on the remote, not here | `pull <category>` |
| `local-newer` | changed here since the last sync | `push` |
| `remote-newer` | another machine pushed it | `pull` |
| `conflict` | both sides changed, or they differ with no known common ancestor | `resolve` |

A skill that was already present locally *and* on the remote the very first time this machine
syncs shows as `conflict` on purpose: there is no record of a shared ancestor, so neither side
can be assumed correct.

## Common errors

**`rclone was not found on PATH`** — install it (see `providers.md`). After installing on
Windows, restart the terminal so PATH is refreshed.

**`didn't find section in config file`** — the remote name in `config.json` does not exist in
rclone. Check with `rclone listremotes`, then re-run `setup --remote <correct:>`.

**`remote manifest.json is corrupt`** — an interrupted write. Inspect it with
`rclone cat <remote>:ClaudeSkills/manifest.json`. If unrecoverable, delete it
(`rclone delete <remote>:ClaudeSkills/manifest.json`) and `push --force` from the machine with
the best copies; the manifest is rebuilt from what is uploaded.

**`another skill-sync run is in progress`** — a previous run was killed. Delete
`~/.claude/skill-sync/sync.lock`.

**OAuth token expired** (Drive/Dropbox after months idle) — `rclone config reconnect <name>:`.

**Push blocked by the credential scan** — the file and pattern are printed. Move the secret to
an environment variable, or add the file to a `.skillignore` inside that skill. `--no-scan`
uploads it anyway; only do that for genuine false positives, since the remote may be shared or
synced to other devices.

## Recovering overwritten files

Nothing is deleted outright:

- Remote side: replaced or removed files go to `<remote>:<root>/.trash/<timestamp>/<skill>/`.
  List with `rclone lsf <remote>:ClaudeSkills/.trash --dirs-only`.
- Local side: `~/.claude/skill-sync/trash/<timestamp>/<skill>/`.
- Conflict backups: `~/.claude/skill-sync/conflicts/<skill>-remote-<timestamp>/` (the remote
  copy when keeping local) or `...-local-<timestamp>/` (your copy when keeping remote).

`.trash` grows over time; delete old timestamps manually when convenient
(`rclone purge <remote>:ClaudeSkills/.trash/<timestamp>`).

## Hooks

- Not firing: `python scripts/install_hooks.py --dry-run` shows what would be written, and
  `/hooks` inside Claude Code shows what is registered. Hooks are read at session start, so
  restart after installing.
- Session end feels slow: the Stop hook uploads only changed skills, but a first push of large
  assets can take a while. Push manually beforehand, or `--uninstall` the hooks and sync by hand.
- A hook can never fail a session: both exit `0` even on rclone errors. Failures are recorded in
  `~/.claude/skill-sync/sync.log`.

## Menu display problems

- **Garbled frame characters** (`ÔòÉ`) or missing borders: run `python scripts/menu.py --ascii`.
  On the old Windows console, `chcp 65001` plus a TrueType font (Consolas, Cascadia Mono) fixes
  it; Windows Terminal renders it correctly out of the box.
- **Escape codes printed literally** (`[36m`): the terminal has no ANSI support. Use
  `--no-color`, or set `NO_COLOR=1`.
- **`skill-sync menu needs a real terminal`**: it was launched without interactive stdin, which
  is what happens when Claude runs it as a tool call. Launch it yourself with a leading `!`.
- **Arrow keys do nothing over SSH/tmux**: `TERM` is probably unset. Try `TERM=xterm-256color`.

## Verifying without touching real skills

```
python scripts/selftest.py
```

Runs the whole life cycle (two simulated machines, a local folder as the remote) in a temporary
directory. `CLAUDE_SKILLS_DIR` and `SKILL_SYNC_HOME` are redirected, so real skills and real
state are never read or written. Add `--keep` to inspect the temp directory afterwards.

## Uninstalling

```
python scripts/install_hooks.py --uninstall
```

Then delete `~/.claude/skill-sync/` (config, state, logs, backups) and, if wanted, the remote
folder with `rclone purge <remote>:ClaudeSkills`. Local skills are never touched.
