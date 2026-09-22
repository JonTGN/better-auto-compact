# better-compact

> *"Are you tired of burning through your corporate token limits because there's no convenient time to `/compact` your Claude agents before your ADHD kicks in and you start burning tokens from another agent?"*

**Introducing better-compact.**

The smarter auto-compact for Claude Code that watches for inactivity and compacts *for you* — so you can bounce between agents, grab a coffee, fall down a rabbit hole, and come back to a fresh context window instead of a flaming token bill.

---

## The Problem

You're deep in it. Context is at 80%. You tell yourself *"I'll compact before starting the next thing."*

Reader, you did not compact before starting the next thing.

Two agents and one Slack rabbit hole later, both sessions are at 95% and your org's token meter is doing its best impression of a gas pump in 2022.

---

## The Fix

better-compact sits quietly in the background. The moment your session goes idle **and** your context crosses a threshold you pick, it fires `/compact` automatically — no babysitting required.

You walk away. It handles it. You come back to breathing room.

---

## How It Works

1. **Background daemon** polls every 10s → reads `~/.claude/sessions/*.json` (files Claude Code writes itself) to check every live session's native `status` field
2. When `status == "idle"` AND idle for longer than your timeout AND context above threshold → **`/compact` sent automatically** via the tmux pane ID that's also in that JSON
3. **Status line** shows a live countdown: `⏱ compact in 3m 22s` — yellow, then red as it closes in

### Activity detection — using Claude Code's own status

Claude Code natively writes a `status` field to its session JSON in real time:

| What you do | Claude Code status | Timer |
|---|---|---|
| You send a message | `"busy"` | ✅ Resets (idle clock restarts when it goes idle again) |
| You're typing (not yet submitted) | `"idle"` | Timer still running |
| Claude is responding | `"busy"` | ✅ Resets |
| Claude uses a tool | `"busy"` or `"shell"` | ✅ Resets |
| Window focus / scroll | `"idle"` | Timer still running |

**The practically important case — did you actually send a message — is fully covered.** The "typing before submit" gap exists technically but can't cause an unwanted compact with any reasonable timeout (nobody types for 5+ minutes without submitting). The moment you hit Enter the status flips to `"busy"` and the idle clock restarts from zero.

---

## Requirements

- **Python 3.7+**
- **Node.js** (already there — it's Claude Code)
- **tmux** ← strongly recommended; without it you get a macOS notification instead of auto-compact
- **Claude Code** with `~/.claude/settings.json`

---

## Install

```bash
git clone https://github.com/jsvoboda/claude-better-compact.git
cd claude-better-compact
chmod +x install.sh uninstall.sh
./install.sh
```

You'll get two questions:

```
Inactivity timeout before auto-compact (minutes) [5]:
Minimum context usage to trigger compact (%) [70]:
```

Hit Enter twice to go with the defaults and you're done.

### What gets changed

1. `src/better_compact.py` + `src/statusline.js` → copied to `~/.claude/better-compact/src/`
2. Your preferences → `~/.claude/better-compact/config.json`
3. Two hooks added to `~/.claude/settings.json`:
   - **`Stop`** — records session info, shows compact status in Claude's context
   - **`PreToolUse`** — resets the inactivity clock on tool use
4. Your existing `statusLine` command is replaced with the extended version (your original is backed up under `_bc_statusline_backup` in `settings.json` and auto-restored on uninstall)

### Optional: reset timer on tmux pane focus

Add this to `~/.tmux.conf` to reset the inactivity clock whenever you switch back to the Claude Code pane:

```tmux
set-hook -g pane-focus-in "run-shell 'touch ~/.claude/better-compact/sessions/$(cat ~/.claude/better-compact/current-session 2>/dev/null)/last_activity 2>/dev/null || true'"
```

---

## Configuration

Edit `~/.claude/better-compact/config.json` any time — changes apply within 10 seconds, no restart needed:

```json
{
  "inactivity_timeout_minutes": 5,
  "compact_threshold_percent": 70
}
```

| Key | Default | What it does |
|---|---|---|
| `inactivity_timeout_minutes` | `5` | How long the session must be idle before compact fires |
| `compact_threshold_percent` | `70` | Context % that has to be reached before the timer even arms |

---

## Status Line

After install, your Claude Code status bar looks like this:

```
Claude Opus 5 | context used 73.4% - (146,800/200,000) | cost: $0.12 | ⏱ compact in 4m 22s
session: abc123...
```

- **No countdown** → context below threshold, chill
- **Yellow countdown** → armed, timer running
- **Red countdown / "compact now!"** → about to fire (or already sent)

---

## Check Status

```bash
python3 ~/.claude/better-compact/src/better_compact.py status
```

```
Better Auto-Compact — Status
========================================
  Inactivity timeout : 5m
  Compact threshold  : 70%
  Daemon             : running

  Session abc12345...
    Context: 73.4%  |  Inactive: 2m 18s  |  armed — compact in 2m 42s
```

---

## Daemon

Starts automatically on your first Claude response of a session. Exits itself after 10 minutes of no active sessions. Logs at `~/.claude/better-compact/daemon.log`.

Force-stop it:
```bash
kill $(cat ~/.claude/better-compact/daemon.pid)
```

---

## Uninstall

```bash
./uninstall.sh
```

Stops the daemon, removes the hooks, restores your original status line, deletes `~/.claude/better-compact/`. Clean slate.

---

## Troubleshooting

**Compact isn't firing**
- Check `~/.claude/better-compact/daemon.log`
- Run `status` command above — if daemon is stopped, open a new Claude Code session to restart it
- Make sure `tmux` is available: `which tmux`
- If Claude Code runs in a wrapper that clears env, `TMUX_PANE` might not be set — check `~/.claude/better-compact/sessions/*/tmux_pane`

**Status line shows no countdown despite high context**
- Daemon writes state every 10s after the Stop hook fires — give it a moment
- Check `~/.claude/better-compact/compact-state.json` exists and has your session ID

**`python3` not found**
- Install Python 3, or edit the hook commands in `~/.claude/settings.json` to use the full path (e.g. `/usr/local/bin/python3`)

**Status line broke**
```bash
cat ~/.claude/settings.json | python3 -c "import sys,json; s=json.load(sys.stdin); print(s.get('_bc_statusline_backup', 'no backup found'))"
```
Copy the output back into `"statusLine"` in `settings.json`.

---

## The Math

Context % is calculated using the **exact same logic** as Claude Code's built-in status bar: parses the session transcript JSONL, finds the newest non-synthetic assistant message, reads `message.usage` (`input_tokens + output_tokens + cache_read + cache_creation`). Not an estimate — the real number, against a 200K token window.
