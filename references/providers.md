# Providers and machine bootstrap

skill-sync talks to storage only through [rclone](https://rclone.org), so every backend
rclone supports works: Google Drive, Dropbox, OneDrive, Box, pCloud, Mega, S3, Backblaze B2,
WebDAV/Nextcloud, SFTP, or a plain local folder (handy for a NAS or a USB drive).

## 1. Install rclone

| OS | Command |
|---|---|
| Windows | `winget install Rclone.Rclone` (or `choco install rclone`) |
| macOS | `brew install rclone` |
| Linux | `sudo -v ; curl https://rclone.org/install.sh \| sudo bash` |

## 2. Create a remote — the user must do this

`rclone config` is an interactive wizard and often opens a browser for OAuth, so Claude
cannot run it. In Claude Code the user can run it in-session by prefixing it with `!`:

```
! rclone config
```

Wizard shortcuts:

- **Google Drive**: `n` → name it `gdrive` → storage `drive` → leave client_id/secret blank
  → scope `1` (full) or `3` (drive.file, only files rclone creates — enough here and less
  invasive) → `Edit advanced config? n` → `Use web browser to authenticate? y`.
- **Dropbox**: `n` → name `dropbox` → storage `dropbox` → blank app id/secret → browser auth.
- **OneDrive**: `n` → name `onedrive` → storage `onedrive` → browser auth → pick
  `OneDrive Personal or Business`.
- **S3 / B2 / WebDAV**: follow the prompts; these ask for keys instead of a browser login.

Headless machine (no browser): run `rclone authorize "drive"` on a desktop and paste the
token, as the wizard explains.

Verify: `rclone lsd <name>:`

## 3. Configure skill-sync

```
python scripts/sync.py setup --remote gdrive: --root ClaudeSkills \
    --categories work,school,personal --default-category personal
```

- `--root` is the folder created inside the remote. Use a subpath to nest it,
  e.g. `--remote gdrive:Backups --root ClaudeSkills`.
- `--machine` names this computer in the manifest (default: hostname). Useful to know which
  machine last touched a skill.
- Categories are free-form; add more later, `categorize` registers new ones automatically.

## 4. Bootstrapping a brand new computer

skill-sync itself lives in `~/.claude/skills/skill-sync/`, so a fresh machine does not have
it yet. Two options:

**A. Copy the skill down with rclone directly** (after installing rclone and configuring the
same remote):

```
rclone copy gdrive:ClaudeSkills/<category>/skill-sync ~/.claude/skills/skill-sync
python ~/.claude/skills/skill-sync/scripts/sync.py setup --remote gdrive: --categories work
python ~/.claude/skills/skill-sync/scripts/sync.py pull work
```

On Windows use `%USERPROFILE%\.claude\skills\skill-sync`.

**B. Keep this skill in git** and clone it, then run `setup`.

Either way, run `python scripts/install_hooks.py` on the new machine so it also uploads
automatically, and restart Claude Code so the pulled skills are discovered.

## Choosing categories

Categories exist so a work laptop does not pull personal skills. Typical split:

- `work` — client, employer, internal tooling skills.
- `school` — coursework, study helpers.
- `personal` — everything else.
- `shared` — skills wanted on every machine.

A machine subscribes to categories simply by pulling them; the set is remembered in
`~/.claude/skill-sync/config.json` and used by the SessionStart notice, so a work laptop is
never told about new personal skills.

## Environment overrides

| Variable | Effect |
|---|---|
| `CLAUDE_SKILLS_DIR` | use a different skills folder (testing, project-scoped skills) |
| `SKILL_SYNC_HOME` | move the runtime state out of `~/.claude/skill-sync` |
