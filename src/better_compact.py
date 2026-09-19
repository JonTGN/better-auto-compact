#!/usr/bin/env python3
"""
Better Auto-Compact for Claude Code
https://github.com/jsvoboda/claude-better-compact

Monitors sessions for inactivity and auto-compacts when context exceeds
a configured threshold. Runs as a background daemon, started by Claude
Code's Stop hook.

Usage (invoked by Claude Code hooks + install script):
  better_compact.py stop-hook       # Claude Code Stop event (stdin: JSON)
  better_compact.py pre-tool-hook   # Claude Code PreToolUse event (stdin: JSON)
  better_compact.py daemon          # Background daemon process
  better_compact.py status          # Show current status
"""

import sys
import os
import json
import time
import signal
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timezone

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path.home() / ".claude" / "better-compact"
CONFIG_FILE = BASE_DIR / "config.json"
SESSIONS_DIR = BASE_DIR / "sessions"
STATE_FILE = BASE_DIR / "compact-state.json"
DAEMON_PID_FILE = BASE_DIR / "daemon.pid"
LOG_FILE = BASE_DIR / "daemon.log"

CONTEXT_WINDOW = 200_000  # tokens (current Claude models)

DEFAULT_CONFIG = {
    "inactivity_timeout_minutes": 5,
    "compact_threshold_percent": 70,
    "version": "1.0.0",
}

# ── Config ─────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text())
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
    except Exception:
        pass
    return DEFAULT_CONFIG.copy()


# ── File helpers ───────────────────────────────────────────────────────────────

def write_file(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def read_file(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip()
    except Exception:
        return default


# ── Context calculation (mirrors ctx_monitor.js logic exactly) ─────────────────

def _used_total(usage: dict) -> int:
    return (
        usage.get("input_tokens", 0)
        + usage.get("output_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
    )


def get_context_from_transcript(transcript_path: Path) -> tuple[float, int]:
    """Return (percent_used, tokens_used) from the transcript's newest assistant message."""
    latest_ts = -float("inf")
    latest_usage = None

    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue

                msg = j.get("message", {})
                if not msg:
                    continue
                if j.get("isSidechain"):
                    continue
                model_str = str(msg.get("model", "")).lower()
                if "synthetic" in model_str:
                    continue
                if j.get("isApiErrorMessage"):
                    continue
                if msg.get("role") != "assistant":
                    continue

                usage = msg.get("usage", {})
                if not usage or _used_total(usage) == 0:
                    continue

                # skip "no response requested" content
                content = msg.get("content", [])
                if isinstance(content, list) and any(
                    isinstance(b, dict)
                    and b.get("type") == "text"
                    and "no response requested" in str(b.get("text", "")).lower()
                    for b in content
                ):
                    continue

                ts_str = j.get("timestamp", "")
                ts = 0.0
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")
                        ).timestamp()
                    except Exception:
                        pass

                if ts > latest_ts or (
                    ts == latest_ts
                    and _used_total(usage) > _used_total(latest_usage or {})
                ):
                    latest_ts = ts
                    latest_usage = usage

    except Exception:
        return 0.0, 0

    if not latest_usage:
        return 0.0, 0

    used = _used_total(latest_usage)
    pct = round((used * 1000) / CONTEXT_WINDOW) / 10.0 if CONTEXT_WINDOW > 0 else 0.0
    return min(pct, 100.0), used


# ── State file ─────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {"sessions": {}, "last_updated": 0.0}


def save_state(state: dict):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2))
        tmp.rename(STATE_FILE)
    except Exception:
        pass


# ── Daemon management ──────────────────────────────────────────────────────────

def is_daemon_running() -> bool:
    if not DAEMON_PID_FILE.exists():
        return False
    try:
        pid = int(DAEMON_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def ensure_daemon_running():
    if is_daemon_running():
        return
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    with open(LOG_FILE, "a") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, str(script), "daemon"],
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
        )
    write_file(DAEMON_PID_FILE, str(proc.pid))


# ── Logging ────────────────────────────────────────────────────────────────────

def log(message: str):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


# ── Compact trigger ────────────────────────────────────────────────────────────

def trigger_compact(session_dir: Path, context_pct: float) -> bool:
    """Send /compact to the Claude Code session. Returns True if handled."""
    tmux_pane = read_file(session_dir / "tmux_pane")

    if tmux_pane and shutil.which("tmux"):
        try:
            result = subprocess.run(
                ["tmux", "send-keys", "-t", tmux_pane, "/compact", "Enter"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                log(f"Sent /compact to tmux pane {tmux_pane}")
                return True
            log(f"tmux send-keys failed (rc={result.returncode}): {result.stderr.decode()}")
        except Exception as e:
            log(f"tmux compact error: {e}")

    # Fallback: system notification
    msg = (
        f"Context is {context_pct:.1f}% full and session has been inactive. "
        "Run /compact in Claude Code to free space."
    )
    try:
        if sys.platform == "darwin":
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{msg}" with title "Better Compact" sound name "Ping"',
                ],
                capture_output=True,
                timeout=5,
            )
        else:
            subprocess.run(
                ["notify-send", "Better Compact", msg],
                capture_output=True,
                timeout=5,
            )
    except Exception:
        pass

    return True


# ── Daemon tick ────────────────────────────────────────────────────────────────

def daemon_tick(config: dict) -> bool:
    """Check all sessions. Returns True if at least one active session found."""
    timeout_secs = config["inactivity_timeout_minutes"] * 60.0
    threshold = config["compact_threshold_percent"]

    if not SESSIONS_DIR.exists():
        return False

    state = load_state()
    now = time.time()
    any_active = False

    for session_dir in SESSIONS_DIR.iterdir():
        if not session_dir.is_dir():
            continue

        session_id = session_dir.name
        transcript_str = read_file(session_dir / "transcript")
        if not transcript_str:
            continue
        transcript_path = Path(transcript_str)
        if not transcript_path.exists():
            # Transcript gone — clean up stale session after 1h
            if now - session_dir.stat().st_mtime > 3600:
                shutil.rmtree(session_dir, ignore_errors=True)
                state["sessions"].pop(session_id, None)
            continue

        any_active = True

        # Activity = max of transcript mtime and any explicit activity touches
        activity_file = session_dir / "last_activity"
        last_activity = transcript_path.stat().st_mtime
        if activity_file.exists():
            last_activity = max(last_activity, activity_file.stat().st_mtime)

        inactive_secs = now - last_activity
        context_pct, tokens_used = get_context_from_transcript(transcript_path)
        countdown_secs = max(0.0, timeout_secs - inactive_secs)
        armed = context_pct >= threshold

        prev = state["sessions"].get(session_id, {})
        compact_triggered = prev.get("compact_triggered", False)

        state["sessions"][session_id] = {
            "armed": armed,
            "countdown_seconds": round(countdown_secs, 1),
            "context_pct": context_pct,
            "tokens_used": tokens_used,
            "compact_triggered": compact_triggered,
            "inactive_seconds": round(inactive_secs, 1),
        }

        if armed and inactive_secs >= timeout_secs and not compact_triggered:
            log(
                f"Auto-compact: session {session_id[:8]}... "
                f"ctx={context_pct:.1f}% inactive={inactive_secs:.0f}s"
            )
            trigger_compact(session_dir, context_pct)
            state["sessions"][session_id]["compact_triggered"] = True

    state["last_updated"] = now
    save_state(state)
    return any_active


# ── Daemon main loop ───────────────────────────────────────────────────────────

def daemon():
    # Bail if another daemon is already running (race-condition guard)
    if DAEMON_PID_FILE.exists():
        try:
            pid = int(DAEMON_PID_FILE.read_text().strip())
            if pid != os.getpid():
                os.kill(pid, 0)
                # Other daemon is alive
                return
        except (ValueError, ProcessLookupError):
            pass

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    write_file(DAEMON_PID_FILE, str(os.getpid()))

    def handle_exit(sig, frame):
        DAEMON_PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_exit)
    signal.signal(signal.SIGINT, handle_exit)

    log(f"Daemon started (PID {os.getpid()})")

    idle_ticks = 0
    while True:
        try:
            config = load_config()
            had_activity = daemon_tick(config)
            if had_activity:
                idle_ticks = 0
            else:
                idle_ticks += 1
                # Exit after 10 min with no active sessions
                if idle_ticks > 60:
                    log("No active sessions — daemon exiting")
                    break
        except Exception as e:
            log(f"Daemon tick error: {e}")
        time.sleep(10)

    DAEMON_PID_FILE.unlink(missing_ok=True)


# ── Stop hook ──────────────────────────────────────────────────────────────────

def stop_hook():
    """Called by Claude Code's Stop event. Stdin: JSON with session_id + transcript_path."""
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    session_id = data.get("session_id", "")
    transcript_path = data.get("transcript_path", "")
    if not session_id or not transcript_path:
        return

    session_dir = SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    # Record session info
    write_file(session_dir / "transcript", transcript_path)

    # Record tmux pane if available (used to send /compact)
    tmux_pane = os.environ.get("TMUX_PANE", "")
    if tmux_pane:
        write_file(session_dir / "tmux_pane", tmux_pane)
        # Write current-session pointer for optional tmux pane-focus hook
        write_file(BASE_DIR / "current-session", session_id)

    # Clear the compact-triggered flag — new interaction means new timer cycle
    write_file(session_dir / "compacted", "")
    state = load_state()
    if session_id in state.get("sessions", {}):
        state["sessions"][session_id]["compact_triggered"] = False
        save_state(state)

    # Start daemon if it isn't running
    ensure_daemon_running()

    # Output a compact-status line into Claude's context
    config = load_config()
    threshold = config["compact_threshold_percent"]
    timeout_min = config["inactivity_timeout_minutes"]

    # Get context % — prefer state file (daemon already calculated it), else compute now
    session_state = state.get("sessions", {}).get(session_id, {})
    context_pct = session_state.get("context_pct", 0.0)
    if context_pct == 0.0:
        context_pct, _ = get_context_from_transcript(Path(transcript_path))

    if context_pct >= threshold:
        print(
            f"[better-compact] ⏱ Context {context_pct:.1f}% ≥ {threshold}% — "
            f"auto-compact fires in {timeout_min}m if session goes idle"
        )
    # Below threshold: stay silent


# ── PreToolUse hook ────────────────────────────────────────────────────────────

def pre_tool_hook():
    """Called by Claude Code's PreToolUse event. Resets inactivity by touching activity file."""
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    session_id = data.get("session_id", "")
    if not session_id:
        return

    session_dir = SESSIONS_DIR / session_id
    if not session_dir.exists():
        return

    # Touch activity file to reset the inactivity clock
    activity_file = session_dir / "last_activity"
    activity_file.touch()

    # Also clear compact_triggered in state so we can compact again after resuming
    state = load_state()
    if session_id in state.get("sessions", {}):
        state["sessions"][session_id]["compact_triggered"] = False
        save_state(state)


# ── Status command ─────────────────────────────────────────────────────────────

def show_status():
    config = load_config()
    timeout_secs = config["inactivity_timeout_minutes"] * 60.0
    threshold = config["compact_threshold_percent"]

    print("Better Auto-Compact — Status")
    print("=" * 40)
    print(f"  Inactivity timeout : {config['inactivity_timeout_minutes']}m")
    print(f"  Compact threshold  : {threshold}%")
    print(f"  Daemon             : {'running' if is_daemon_running() else 'stopped'}")
    print()

    state = load_state()
    sessions = state.get("sessions", {})
    if not sessions:
        print("  No tracked sessions.")
        return

    for sid, s in sessions.items():
        ctx = s.get("context_pct", 0)
        countdown = s.get("countdown_seconds", 0)
        inactive = s.get("inactive_seconds", 0)
        armed = s.get("armed", False)
        triggered = s.get("compact_triggered", False)

        mins, secs = divmod(int(inactive), 60)
        status = "idle"
        if triggered:
            status = "compact sent"
        elif armed and countdown == 0:
            status = "COMPACT PENDING"
        elif armed:
            cm, cs = divmod(int(countdown), 60)
            status = f"armed — compact in {cm}m {cs}s"

        print(f"  Session {sid[:8]}...")
        print(f"    Context: {ctx:.1f}%  |  Inactive: {mins}m {secs}s  |  {status}")


# ── Install / uninstall ────────────────────────────────────────────────────────

def _update_settings(settings_file: Path, installed_script: Path, statusline_script: Path):
    settings = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except Exception:
            pass

    hooks = settings.setdefault("hooks", {})

    stop_cmd = f"python3 {installed_script} stop-hook"
    stop_hooks = hooks.setdefault("Stop", [])
    cmds_in_stop = [
        h.get("command", "")
        for entry in stop_hooks
        for h in entry.get("hooks", [])
    ]
    if stop_cmd not in cmds_in_stop:
        stop_hooks.append({
            "matcher": "",
            "hooks": [{"type": "command", "command": stop_cmd}],
        })

    pre_cmd = f"python3 {installed_script} pre-tool-hook"
    pre_hooks = hooks.setdefault("PreToolUse", [])
    cmds_in_pre = [
        h.get("command", "")
        for entry in pre_hooks
        for h in entry.get("hooks", [])
    ]
    if pre_cmd not in cmds_in_pre:
        pre_hooks.append({
            "matcher": "",
            "hooks": [{"type": "command", "command": pre_cmd}],
        })

    # Backup existing statusLine and install ours
    if "statusLine" in settings and "_bc_statusline_backup" not in settings:
        settings["_bc_statusline_backup"] = settings["statusLine"]
    settings["statusLine"] = {
        "type": "command",
        "command": f"node {statusline_script}",
    }

    settings_file.write_text(json.dumps(settings, indent=2))
    print(f"  Updated {settings_file}")


def _remove_from_settings(settings_file: Path, installed_script: Path):
    if not settings_file.exists():
        return

    try:
        settings = json.loads(settings_file.read_text())
    except Exception:
        return

    hooks = settings.get("hooks", {})

    for event in ("Stop", "PreToolUse"):
        entries = hooks.get(event, [])
        new_entries = []
        for entry in entries:
            filtered = [
                h for h in entry.get("hooks", [])
                if str(installed_script) not in h.get("command", "")
            ]
            if filtered:
                new_entries.append({**entry, "hooks": filtered})
            elif entry.get("matcher") or len(entry.get("hooks", [])) > len(filtered):
                pass  # drop entirely if all hooks were ours
        hooks[event] = new_entries

    # Restore original statusLine if we backed it up
    backup = settings.pop("_bc_statusline_backup", None)
    if backup is not None:
        settings["statusLine"] = backup
    elif "statusLine" in settings:
        # Remove ours only if it points to our script
        sl = settings.get("statusLine", {})
        if str(installed_script).replace("better_compact.py", "statusline.js") in sl.get("command", ""):
            del settings["statusLine"]

    settings_file.write_text(json.dumps(settings, indent=2))
    print(f"  Updated {settings_file}")


def install_main():
    print()
    print("Better Auto-Compact for Claude Code")
    print("=" * 40)
    print()

    # Gather config
    try:
        timeout_input = input("Inactivity timeout before auto-compact (minutes) [5]: ").strip()
        timeout = int(timeout_input) if timeout_input else 5
    except (ValueError, EOFError):
        timeout = 5

    try:
        threshold_input = input("Minimum context usage to trigger compact (%) [70]: ").strip()
        threshold = int(threshold_input) if threshold_input else 70
    except (ValueError, EOFError):
        threshold = 70

    print()

    install_dir = BASE_DIR / "src"
    install_dir.mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "sessions").mkdir(exist_ok=True)

    # Write config
    config = {
        "inactivity_timeout_minutes": timeout,
        "compact_threshold_percent": threshold,
        "version": "1.0.0",
    }
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    print(f"  Config written: {CONFIG_FILE}")

    # Copy scripts from repo src/ → install dir
    repo_src = Path(__file__).parent
    for fname in ("better_compact.py", "statusline.js"):
        src = repo_src / fname
        dst = install_dir / fname
        if src.exists():
            shutil.copy2(src, dst)
            dst.chmod(dst.stat().st_mode | 0o755)
            print(f"  Installed: {dst}")
        else:
            print(f"  WARNING: {src} not found — skipping")

    installed_script = install_dir / "better_compact.py"
    statusline_script = install_dir / "statusline.js"

    # Update ~/.claude/settings.json
    settings_file = Path.home() / ".claude" / "settings.json"
    _update_settings(settings_file, installed_script, statusline_script)

    print()
    print("Installation complete!")
    print()
    print(f"  Inactivity timeout : {timeout} minutes")
    print(f"  Compact threshold  : {threshold}%")
    print()
    print("Start a new Claude Code session to activate.")
    print(f"Run `python3 {installed_script} status` to check status.")
    print()


def uninstall_main():
    print()
    print("Uninstalling Better Auto-Compact...")
    print()

    # Kill daemon if running
    if is_daemon_running():
        try:
            pid = int(DAEMON_PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"  Stopped daemon (PID {pid})")
        except Exception:
            pass

    installed_script = BASE_DIR / "src" / "better_compact.py"
    settings_file = Path.home() / ".claude" / "settings.json"
    _remove_from_settings(settings_file, installed_script)

    # Remove our files
    if BASE_DIR.exists():
        shutil.rmtree(BASE_DIR, ignore_errors=True)
        print(f"  Removed {BASE_DIR}")

    print()
    print("Uninstall complete. Restart Claude Code to apply changes.")
    print()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "stop-hook":
        stop_hook()
    elif cmd == "pre-tool-hook":
        pre_tool_hook()
    elif cmd == "daemon":
        daemon()
    elif cmd == "status":
        show_status()
    elif cmd == "install":
        install_main()
    elif cmd == "uninstall":
        uninstall_main()
    else:
        print(__doc__)
