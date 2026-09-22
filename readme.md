# better-auto-compact

> *"Are you tired of burning through your corporate token limits because there's no convenient time to `/compact` your Claude agents before your ADHD kicks in and you start burning tokens from another agent?"*

**Introducing better-auto-compact.**

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
3. **Status line** shows context usage and a live countdown once armed: `⏱ compact in 3m 22s`

### Activity detection — using Claude Code's own status

Claude Code natively writes a `status` field to its session JSON in real time:

| What you do | Claude Code status | Timer |
|---|---|---|
| You send a message | `"busy"` | ✅ Resets (idle clock restarts when it goes idle again) |
| You're typing (not yet submitted) | `"idle"` | Timer still running |
| Claude is responding | `"busy"` | ✅ Resets |
| Claude uses a tool | `"busy"` or `"shell"` | ✅ Resets |
| You switch to the tmux pane | `"idle"` | ✅ Resets (tmux `pane_last_used`) |

**The practically important case — did you actually send a message — is fully covered.** The "typing before submit" gap exists technically but can't cause an unwanted compact with any reasonable timeout (nobody types for 5+ minutes without submitting). The moment you hit Enter the status flips to `"busy"` and the idle clock restarts from zero.

In tmux, switching back to a Claude Code pane also resets the timer — no extra config needed.

---

## Requirements

- **Python 3.7+**
- **Node.js** (already there — it's Claude Code)
- **tmux** ← strongly recommended; without it you get a macOS notification instead of auto-compact
- **Claude Code** with `~/.claude/settings.json`

---

## Install

```bash
git clone https://github.com/jsvoboda/better-auto-compact.git
cd better-auto-compact
chmod +x install.sh uninstall.sh
./install.sh
```

You'll get two questions:

```
Inactivity timeout before auto-compact (minutes) [5]:
Minimum context usage to trigger compact (%) [50]:
```

Hit Enter twice to go with the defaults and you're done.

### What gets changed

1. `src/better_compact.py` + `src/statusline.js` → copied to `~/.claude/better-auto-compact/src/`
2. Your preferences → `~/.claude/better-auto-compact/config.json`
3. Two hooks added to `~/.claude/settings.json`:
   - **`Stop`** — records session info, shows compact status in Claude's context
   - **`PreToolUse`** — resets the inactivity clock on tool use
4. Your existing `statusLine` command is replaced with the extended version (your original is backed up under `_bc_statusline_backup` in `settings.json` and auto-restored on uninstall)

---

## Configuration

Edit `~/.claude/better-auto-compact/config.json` any time — changes apply within 10 seconds, no restart needed:

```json
{
  "inactivity_timeout_minutes": 5,
  "compact_threshold_percent": 50
}
```

| Key | Default | What it does |
|---|---|---|
| `inactivity_timeout_minutes` | `5` | How long the session must be idle before compact fires |
| `compact_threshold_percent` | `50` | Context % that has to be reached before the timer even arms |

---

## Status Line

After install, your Claude Code status bar looks like this:

**Below threshold** — monitoring, nothing to do yet:
```
Claude Sonnet 4.5 | context used 32.1% - (32,100/200,000) | cost: $0.04 | auto-compact at 50%
session: abc123...
```

**Armed** — context crossed the threshold, countdown running:
```
Claude Sonnet 4.5 | context used 54.3% - (543,000/1,000,000) | cost: $0.12 | ⏱ compact in 4m 22s
session: abc123...
```

**About to fire:**
```
Claude Sonnet 4.5 | context used 54.3% - (543,000/1,000,000) | cost: $0.12 | ⏱ compact now!
session: abc123...
```

- **Gray "auto-compact at X%"** → below threshold, timer disarmed
- **Yellow countdown** → armed, timer running
- **Red "compact now!"** → idle timeout reached, compact sent (or about to be)

The context window size is read directly from Claude Code — accurate for every model including 1M context variants.

---

## Check Status

```bash
python3 ~/.claude/better-auto-compact/src/better_compact.py status
```

```
Better Auto-Compact — Status
========================================
  Inactivity timeout : 5m
  Compact threshold  : 50%
  Daemon             : running

  PID 12345 | my-project
    Status   : idle | Idle: 2m 18s
    Context  : 54.3% | armed — compact in 2m 42s
    Tmux     : my-session:@1.%1
```

---

## Daemon

Starts automatically on your first Claude response of a session. It can also pick up sessions that were already running before install — no need to restart existing sessions. Exits itself after 10 minutes of no active sessions. Logs at `~/.claude/better-auto-compact/daemon.log`.

Force-stop it:
```bash
kill $(cat ~/.claude/better-auto-compact/daemon.pid)
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
- Check `~/.claude/better-auto-compact/daemon.log`
- Run `status` command above — if daemon is stopped, open a new Claude Code session to restart it
- Make sure `tmux` is available: `which tmux`

**Status line shows stale countdown**
- The daemon writes state every 10s — give it a moment
- Check `~/.claude/better-auto-compact/compact-state.json` exists and has your session ID

**`python3` not found**
- Install Python 3, or edit the hook commands in `~/.claude/settings.json` to use the full path (e.g. `/usr/local/bin/python3`)

**Status line broke**
```bash
cat ~/.claude/settings.json | python3 -c "import sys,json; s=json.load(sys.stdin); print(s.get('_bac_statusline_backup', 'no backup found'))"
```
Copy the output back into `"statusLine"` in `settings.json`.

---

## The Math

Context % is calculated using the **exact same logic** as Claude Code's built-in status bar: parses the session transcript JSONL, finds the newest non-synthetic assistant message, reads `message.usage` (`input_tokens + output_tokens + cache_read + cache_creation`). Not an estimate — the real number.

The context window size is read directly from Claude Code's own JSON (passed via the status line and Stop hook stdin), so it's accurate for every model — 200K, 1M, or whatever comes next.
