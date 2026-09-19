# better-compact

Smarter auto-compact for Claude Code. Instead of compacting at a fixed context percentage, this plugin watches for **session inactivity** — only compacting when you're genuinely away, not mid-thought.

## How it works

1. A **Stop hook** fires every time Claude finishes responding. It records your session info (tmux pane, transcript path) and starts the background daemon.
2. The **background daemon** checks every 10 seconds whether your context usage exceeds the configured threshold **and** the session has been idle for longer than the configured timeout.
3. When both conditions are met the daemon sends `/compact` directly to your Claude Code session via `tmux send-keys`.
4. The **status line** (bottom of your Claude Code terminal) shows a live countdown: `⏱ compact in 3m 22s` in yellow/red depending on urgency.

### Activity signals that reset the timer

| Signal | Detected? |
|---|---|
| User submits a new message | ✅ Yes — transcript file updates immediately |
| Claude responds to a tool call | ✅ Yes — transcript updates |
| tmux pane focus / activity | ✅ Yes — optional tmux hook (see below) |
| Typing before pressing Enter | ❌ No — transcript only updates on submit |
| Window scroll | ❌ No — terminal scroll isn't observable |

The most important signal — **did you actually send a message?** — is always captured. The "typing before submit" gap is small in practice: if you're composing a long message, the inactivity timer resets the moment you hit Enter.

---

## Requirements

- **Python 3.7+** (`python3` on PATH)
- **Node.js** (already required by Claude Code)
- **tmux** (strongly recommended — required for automatic compact; without it you get a macOS notification instead)
- **Claude Code** with a `settings.json` at `~/.claude/settings.json`

---

## Installation

```bash
git clone https://github.com/jsvoboda/claude-better-compact.git
cd claude-better-compact
chmod +x install.sh uninstall.sh
./install.sh
```

The installer will ask two questions:

```
Inactivity timeout before auto-compact (minutes) [5]:
Minimum context usage to trigger compact (%) [70]:
```

Press Enter to accept the defaults, or type your preferred values.

### What the installer does

1. Copies `src/better_compact.py` and `src/statusline.js` to `~/.claude/better-compact/src/`
2. Writes your preferences to `~/.claude/better-compact/config.json`
3. Adds two hooks to `~/.claude/settings.json`:
   - **`Stop`** — records session info, injects compact status into Claude's context
   - **`PreToolUse`** — resets the inactivity clock when a tool runs
4. Replaces your `statusLine` command with the extended version (your original is backed up in `settings.json` as `_bc_statusline_backup` and restored on uninstall)

### Optional: tmux focus detection

If you use tmux and want the inactivity timer to reset when you **focus** the Claude Code pane (e.g. switching back from another window), add this to your `~/.tmux.conf`:

```tmux
set-hook -g pane-focus-in "run-shell 'touch ~/.claude/better-compact/sessions/$(cat ~/.claude/better-compact/current-session 2>/dev/null)/last_activity 2>/dev/null || true'"
```

Then add a `current-session` file writer to your Stop hook by running a one-liner after install:

```bash
# Write current session ID on each Stop hook (for tmux focus integration)
# Already handled automatically if TMUX_PANE is set in the Stop hook env.
```

The daemon also reads `last_activity` alongside the transcript mtime — whichever is newer wins.

---

## Configuration

Edit `~/.claude/better-compact/config.json` at any time:

```json
{
  "inactivity_timeout_minutes": 5,
  "compact_threshold_percent": 70
}
```

| Key | Default | Description |
|---|---|---|
| `inactivity_timeout_minutes` | `5` | Minutes of inactivity before compact fires |
| `compact_threshold_percent` | `70` | Context usage % that must be reached before the timer arms |

Changes take effect on the next daemon tick (within 10 seconds) — no restart needed.

---

## Status line

After installation your Claude Code status bar shows:

```
Claude Opus 5 | context used 73.4% - (146,800/200,000) | cost: $0.12 | ⏱ compact in 4m 22s
session: abc123...
```

- **Yellow** countdown: session is armed, timer running
- **Red** countdown / "compact now!": timer expired, compact is about to fire (or already sent)
- No countdown: context below threshold

---

## Checking status

```bash
python3 ~/.claude/better-compact/src/better_compact.py status
```

Output:
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

## Daemon lifecycle

The daemon starts automatically when you open a Claude Code session (triggered by the Stop hook after Claude's first response). It runs silently in the background and exits itself after 10 minutes with no active sessions.

Logs are at `~/.claude/better-compact/daemon.log`.

To force-stop the daemon:
```bash
kill $(cat ~/.claude/better-compact/daemon.pid)
```

---

## Uninstalling

```bash
./uninstall.sh
```

This:
1. Stops the daemon
2. Removes the hooks from `~/.claude/settings.json`
3. Restores your original `statusLine` command
4. Deletes `~/.claude/better-compact/`

---

## Troubleshooting

### Compact not firing

- Check `~/.claude/better-compact/daemon.log` for errors
- Run `python3 ~/.claude/better-compact/src/better_compact.py status` — if daemon shows "stopped", open a new Claude Code session to restart it
- Confirm `tmux` is available: `which tmux`
- Check the tmux pane ID is correct: the Stop hook records `$TMUX_PANE` from the environment — if Claude Code runs in a wrapper script that clears the environment, `TMUX_PANE` might not be set

### Status line shows no countdown despite high context

- The daemon takes up to 10 seconds after the Stop hook fires to write state
- Check `~/.claude/better-compact/compact-state.json` exists and has your session

### "command not found: python3"

Install Python 3 or change the hook commands in `~/.claude/settings.json` to use the full path to your Python interpreter (e.g. `/usr/local/bin/python3`).

### Status line broke / showing errors

Restore your original status line:
```bash
# Find the backup in settings.json:
cat ~/.claude/settings.json | python3 -c "import sys,json; s=json.load(sys.stdin); print(s.get('_bc_statusline_backup', 'no backup found'))"
```

Then manually set `"statusLine"` back to the original value in `settings.json`.

---

## How context % is calculated

The daemon uses the **exact same logic** as Claude Code's built-in `ctx_monitor.js`: it parses the session transcript JSONL, finds the newest non-synthetic assistant message, and reads `message.usage` (input\_tokens + output\_tokens + cache\_read + cache\_creation). This gives you the precise context window usage, not an estimate.

Context window size is fixed at 200,000 tokens (current Claude models).
