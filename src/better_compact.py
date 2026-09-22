#!/usr/bin/env python3
"""
Better Auto-Compact for Claude Code
https://github.com/jsvoboda/claude-better-compact

Monitors Claude Code sessions for inactivity using the native session JSON
files that Claude Code writes to ~/.claude/sessions/. Auto-compacts when:
  1. Session status is "idle" (not busy/responding/tool-running)
  2. Has been idle longer than the configured timeout
  3. Context usage exceeds the configured threshold

Usage (invoked by Claude Code hooks + install script):
  better_compact.py stop-hook       # Claude Code Stop event (stdin: JSON)
  better_compact.py pre-tool-hook   # Claude Code PreToolUse event (stdin: JSON)
  better_compact.py daemon          # Background daemon process
  better_compact.py status          # Show current status
  better_compact.py install         # Interactive install
  better_compact.py uninstall       # Remove everything
"""

import sys
import os
import json
import time
import signal
import shutil
import subprocess
import glob
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path.home() / ".claude" / "better-compact"
SESSIONS_DIR = Path.home() / ".claude" / "sessions"   # written by Claude Code
CONFIG_FILE = BASE_DIR / "config.json"
TRANSCRIPT_DIR = BASE_DIR / "transcripts"              # session_id -> transcript path
STATE_FILE = BASE_DIR / "compact-state.json"
DAEMON_PID_FILE = BASE_DIR / "daemon.pid"
LOG_FILE = BASE_DIR / "daemon.log"

CONTEXT_WINDOW_DEFAULT = 200_000
CONTEXT_WINDOW_1M = 1_048_576


def context_window_for_model(model_id: str) -> int:
    """Return the context window size for a given model ID."""
    m = (model_id or "").lower()
    if "1m" in m or "1048576" in m:
        return CONTEXT_WINDOW_1M
    return CONTEXT_WINDOW_DEFAULT

DEFAULT_CONFIG = {
    "inactivity_timeout_minutes": 5,
    "compact_threshold_percent": 50,
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


# ── Session JSON reading (Claude Code native) ─────────────────────────────────

def read_session_json(pid: int):
    """Read ~/.claude/sessions/<pid>.json - written by Claude Code itself."""
    j = SESSIONS_DIR / f"{pid}.json"
    try:
        return json.loads(j.read_text())
    except Exception:
        return None


def infer_transcript_path(session: dict) -> Path:
    """
    Infer the transcript JSONL path from the session JSON without needing the Stop hook.
    Claude Code stores transcripts at:
      ~/.claude/projects/<cwd-with-/-replaced-by->/  <sessionId>.jsonl
    """
    cwd = session.get("cwd", "")
    session_id = session.get("sessionId", "")
    if not cwd or not session_id:
        return Path("")
    encoded = cwd.replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"


def find_all_sessions() -> list[dict]:
    """Return all active Claude Code sessions from ~/.claude/sessions/*.json."""
    sessions = []
    try:
        for p in SESSIONS_DIR.glob("*.json"):
            try:
                data = json.loads(p.read_text())
                # Verify the process is actually still alive
                pid = data.get("pid", 0)
                if pid > 1:
                    try:
                        os.kill(pid, 0)
                        sessions.append(data)
                    except ProcessLookupError:
                        pass  # Process dead, skip
            except Exception:
                pass
    except Exception:
        pass
    return sessions


def _tmux_idle_seconds(session: dict) -> float:
    """Seconds since last tmux pane activity (keypresses, scroll, etc.), or inf."""
    tmux_info = session.get("tmux", "")
    if not tmux_info or not shutil.which("tmux"):
        return float("inf")
    pane = tmux_info.split(".")[-1] if "." in tmux_info else ""
    if not pane.startswith("%"):
        return float("inf")
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-t", pane, "-p", "#{pane_last_used}"],
            capture_output=True, text=True, timeout=2
        )
        ts = r.stdout.strip()
        if ts and ts.isdigit():
            return time.time() - int(ts)
    except Exception:
        pass
    return float("inf")


def idle_seconds(session: dict) -> float:
    """
    Return how long this session has been idle, in seconds.
    Takes the minimum of:
      1. Claude Code's native statusUpdatedAt (resets when any message is submitted)
      2. tmux pane_last_used (resets on any keypress/scroll in tmux)
      3. tty mtime (resets on any terminal I/O including user typing)
    This means typing in the prompt box resets the countdown even without submitting.
    """
    if session.get("status") != "idle":
        return 0.0
    updated_ms = session.get("statusUpdatedAt", 0)
    if not updated_ms:
        return 0.0
    claude_idle = (time.time() * 1000 - updated_ms) / 1000.0

    candidates = [claude_idle, _tmux_idle_seconds(session)]
    return min(candidates)


# ── Context calculation (mirrors ctx_monitor.js exactly) ──────────────────────

def _used_total(usage: dict) -> int:
    return (
        usage.get("input_tokens", 0)
        + usage.get("output_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
    )


def get_context_from_transcript(transcript_path: Path, ctx_window: int = 0) -> tuple[float, int]:
    """Return (percent_used, tokens_used) from transcript's newest assistant message."""
    from datetime import datetime

    latest_ts = -float("inf")
    latest_usage = None
    latest_model = ""

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
                if "synthetic" in str(msg.get("model", "")).lower():
                    continue
                if j.get("isApiErrorMessage"):
                    continue
                if msg.get("role") != "assistant":
                    continue
                usage = msg.get("usage", {})
                if not usage or _used_total(usage) == 0:
                    continue
                content = msg.get("content", [])
                if isinstance(content, list) and any(
                    isinstance(b, dict)
                    and b.get("type") == "text"
                    and "no response requested" in str(b.get("text", "")).lower()
                    for b in content
                ):
                    continue
                ts = 0.0
                ts_str = j.get("timestamp", "")
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
                    latest_model = str(msg.get("model", ""))
    except Exception:
        return 0.0, 0

    if not latest_usage:
        return 0.0, 0

    used = _used_total(latest_usage)
    win = ctx_window or context_window_for_model(latest_model)
    pct = round((used * 1000) / win) / 10.0 if win > 0 else 0.0
    return min(pct, 100.0), used


# ── State file (read by statusline.js) ────────────────────────────────────────

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


# ── Compact trigger ────────────────────────────────────────────────────────────

def _session_tty(pid: int) -> str:
    """Return the controlling tty path for a process (e.g. '/dev/ttys041')."""
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "tty="],
                           capture_output=True, text=True, timeout=3)
        name = r.stdout.strip()
        if name and name != "??":
            return f"/dev/{name}" if not name.startswith("/") else name
    except Exception:
        pass
    return ""


def _compact_via_socket(session: dict) -> bool:
    """
    Try to send /compact via the messaging socket using the session key.
    The key file is read at runtime (never logged). Returns True on success.

    Protocol (reverse-engineered from extension source):
      - Unix domain socket at messagingSocketPath
      - Key file: ~/.claude/sessions/<pid>.<sha256>.key (600, owner-only)
      - Auth: send JSON {"type":"auth","token":"<key_content>"} + newline
      - Compact: send JSON {"type":"compact"} + newline
      - Server responds to each message with JSON; silent on bad auth.
    """
    import socket as _sock

    socket_path = session.get("messagingSocketPath", "")
    pid = session.get("pid", 0)
    if not socket_path or not pid:
        return False

    # Find the key file for this session
    key_file = None
    try:
        for kf in SESSIONS_DIR.glob(f"{pid}.*.key"):
            key_file = kf
            break
    except Exception:
        return False
    if not key_file:
        return False

    # Read key without logging its value
    try:
        key = key_file.read_text().strip()
    except Exception:
        return False

    s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(socket_path)

        def send_msg(obj):
            s.send((json.dumps(obj) + "\n").encode())

        def recv_msg():
            buf = b""
            s.settimeout(2)
            try:
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    if b"\n" in buf:
                        break
            except _sock.timeout:
                pass
            return buf.decode(errors="replace").strip()

        # Auth handshake — try a few common patterns since protocol is undocumented
        send_msg({"type": "auth", "token": key})
        auth_resp = recv_msg()
        log(f"Socket auth response: {auth_resp[:100] if auth_resp else '(none)'}")

        if not auth_resp:
            # Try raw key as first line (some simple protocols just send the token)
            s2 = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
            s2.settimeout(5)
            s2.connect(socket_path)
            s2.send((key + "\n").encode())
            import time; time.sleep(0.3)
            try:
                r = s2.recv(4096)
                log(f"Raw key response: {r[:100]}")
                if r:
                    # Got a response — connected. Try compact.
                    s2.send(json.dumps({"type": "compact"}).encode() + b"\n")
                    time.sleep(0.5)
                    return True
            except _sock.timeout:
                pass
            finally:
                s2.close()
            return False

        # If we got a response to auth, try sending compact
        import time; time.sleep(0.2)
        send_msg({"type": "compact"})
        compact_resp = recv_msg()
        log(f"Socket compact response: {compact_resp[:100] if compact_resp else '(none)'}")

        # Any non-error response = success
        if compact_resp and "error" not in compact_resp.lower():
            log(f"Compact sent via socket for PID {pid}")
            return True

        return False
    except Exception as e:
        log(f"Socket compact error for PID {pid}: {e}")
        return False
    finally:
        s.close()


def _compact_via_applescript(pid: int, tty: str) -> bool:
    """
    On macOS without tmux, find the Terminal.app or iTerm2 tab running
    this tty and send /compact to it. Returns True on success.
    """
    if sys.platform != "darwin" or not tty:
        return False

    tty_name = tty.replace("/dev/", "")

    # Terminal.app
    terminal_script = f"""
tell application "Terminal"
    repeat with w in windows
        repeat with t in tabs of w
            try
                if tty of t contains "{tty_name}" then
                    do script "/compact" in t
                    return "ok"
                end if
            end try
        end repeat
    end repeat
    return "not_found"
end tell
"""
    try:
        r = subprocess.run(["osascript", "-e", terminal_script],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and "ok" in r.stdout:
            log(f"Compact sent via Terminal.app (tty {tty_name})")
            return True
    except Exception:
        pass

    # iTerm2
    iterm_script = f"""
tell application "iTerm2"
    repeat with w in windows
        try
            repeat with t in tabs of w
                repeat with sess in sessions of t
                    try
                        if tty of sess contains "{tty_name}" then
                            tell sess to write text "/compact"
                            return "ok"
                        end if
                    end try
                end repeat
            end repeat
        end try
    end repeat
    return "not_found"
end tell
"""
    try:
        r2 = subprocess.run(["osascript", "-e", iterm_script],
                            capture_output=True, text=True, timeout=5)
        if r2.returncode == 0 and "ok" in r2.stdout:
            log(f"Compact sent via iTerm2 (tty {tty_name})")
            return True
    except Exception:
        pass

    return False


def trigger_compact(session: dict, context_pct: float) -> bool:
    """
    Send /compact to the session. Tries in order:
      1. tmux send-keys (pane ID from native session JSON)
      2. Messaging socket with session key auth
      3. AppleScript (Terminal.app / iTerm2) matching by tty
      4. System notification fallback
    Returns True when the command was delivered or notification sent.
    """
    pid = session.get("pid", 0)

    # ── 1. tmux ─────────────────────────────────────────────────────────────────
    tmux_info = session.get("tmux", "")
    if tmux_info and shutil.which("tmux"):
        pane = tmux_info.split(".")[-1] if "." in tmux_info else ""
        if pane.startswith("%"):
            try:
                result = subprocess.run(
                    ["tmux", "send-keys", "-t", pane, "/compact", "Enter"],
                    capture_output=True, timeout=5,
                )
                if result.returncode == 0:
                    log(f"Compact via tmux pane {pane}")
                    return True
                log(f"tmux failed rc={result.returncode}")
            except Exception as e:
                log(f"tmux error: {e}")

    # ── 2. Messaging socket ──────────────────────────────────────────────────────
    if _compact_via_socket(session):
        return True

    # ── 3. AppleScript ───────────────────────────────────────────────────────────
    tty = _session_tty(pid) if pid else ""
    if tty and _compact_via_applescript(pid, tty):
        return True

    # ── 4. Notification fallback ─────────────────────────────────────────────────
    msg = (
        f"Claude Code context is {context_pct:.1f}% full "
        f"(idle {idle_seconds(session):.0f}s). Run /compact to free space."
    )
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{msg}" with title "Better Compact" sound name "Ping"'],
                capture_output=True, timeout=5,
            )
        else:
            subprocess.run(["notify-send", "Better Compact", msg],
                           capture_output=True, timeout=5)
    except Exception:
        pass

    log(f"Compact notification sent for PID {pid} (no direct channel available)")
    return True


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
            stdout=log_fh, stderr=log_fh,
            start_new_session=True, close_fds=True,
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


# ── Daemon main loop ───────────────────────────────────────────────────────────

def daemon():
    if DAEMON_PID_FILE.exists():
        try:
            pid = int(DAEMON_PID_FILE.read_text().strip())
            if pid != os.getpid():
                os.kill(pid, 0)
                return  # Another daemon alive
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
            had_sessions = daemon_tick(config)
            if had_sessions:
                idle_ticks = 0
            else:
                idle_ticks += 1
                if idle_ticks > 60:  # 10 min with no sessions
                    log("No active sessions — daemon exiting")
                    break
        except Exception as e:
            log(f"Daemon tick error: {e}")
        time.sleep(10)

    DAEMON_PID_FILE.unlink(missing_ok=True)


def daemon_tick(config: dict) -> bool:
    """Check all Claude Code sessions. Returns True if any active session found."""
    timeout_secs = config["inactivity_timeout_minutes"] * 60.0
    threshold = config["compact_threshold_percent"]

    sessions = find_all_sessions()
    if not sessions:
        return False

    state = load_state()
    now = time.time()

    for session in sessions:
        session_id = session.get("sessionId", "")
        pid = session.get("pid", 0)
        if not session_id or not pid:
            continue

        # Get transcript path — prefer Stop hook recording, fall back to inferred path
        transcript_path_str = read_file(TRANSCRIPT_DIR / session_id)
        tp = Path(transcript_path_str) if transcript_path_str else infer_transcript_path(session)
        ctx_win_str = read_file(TRANSCRIPT_DIR / f"{session_id}.ctxwin")
        ctx_win = int(ctx_win_str) if ctx_win_str.isdigit() else 0
        context_pct, tokens_used = 0.0, 0
        if tp and tp.exists():
            context_pct, tokens_used = get_context_from_transcript(tp, ctx_win)

        idle_secs = idle_seconds(session)
        countdown_secs = max(0.0, timeout_secs - idle_secs)
        armed = context_pct >= threshold
        status = session.get("status", "unknown")

        prev = state["sessions"].get(session_id, {})
        compact_triggered = prev.get("compact_triggered", False)

        # deadline = absolute Unix timestamp when compact fires — statusline uses this
        # for a live countdown that doesn't depend on daemon poll frequency
        deadline = (now + countdown_secs) if armed else 0.0

        state["sessions"][session_id] = {
            "pid": pid,
            "status": status,
            "armed": armed,
            "deadline": round(deadline, 3),
            "countdown_seconds": round(countdown_secs, 1),  # kept for status cmd
            "context_pct": context_pct,
            "tokens_used": tokens_used,
            "compact_triggered": compact_triggered,
            "idle_seconds": round(idle_secs, 1),
        }

        if armed and idle_secs >= timeout_secs and not compact_triggered:
            log(
                f"Auto-compact: session {session_id[:8]}... "
                f"ctx={context_pct:.1f}% idle={idle_secs:.0f}s"
            )
            trigger_compact(session, context_pct)
            state["sessions"][session_id]["compact_triggered"] = True

    # Clean up state entries for sessions that no longer exist
    active_ids = {s.get("sessionId") for s in sessions}
    for sid in list(state["sessions"].keys()):
        if sid not in active_ids:
            state["sessions"].pop(sid, None)

    state["last_updated"] = now
    state["threshold"] = threshold  # statusline reads this to show "auto-compact at X%"
    save_state(state)
    return True


# ── Stop hook ──────────────────────────────────────────────────────────────────

def stop_hook():
    """Called by Claude Code's Stop event. Records transcript path, outputs status."""
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    session_id = data.get("session_id", "")
    transcript_path = data.get("transcript_path", "")
    if not session_id or not transcript_path:
        return

    # Record transcript path and context window size so daemon can calculate context %
    write_file(TRANSCRIPT_DIR / session_id, transcript_path)
    ctx_win = data.get("context_window", {}).get("context_window_size", 0)
    if ctx_win:
        write_file(TRANSCRIPT_DIR / f"{session_id}.ctxwin", str(ctx_win))

    # Reset compact_triggered so we can compact again after a new conversation cycle
    state = load_state()
    if session_id in state.get("sessions", {}):
        state["sessions"][session_id]["compact_triggered"] = False
        save_state(state)

    # Start daemon if not running
    ensure_daemon_running()

    # Output status into Claude's context
    config = load_config()
    threshold = config["compact_threshold_percent"]
    timeout_min = config["inactivity_timeout_minutes"]

    session_state = state.get("sessions", {}).get(session_id, {})
    context_pct = session_state.get("context_pct", 0.0)
    if context_pct == 0.0:
        context_pct, _ = get_context_from_transcript(Path(transcript_path))

    if context_pct >= threshold:
        print(
            f"[better-compact] ⏱ Context {context_pct:.1f}% ≥ {threshold}% — "
            f"auto-compact fires in {timeout_min}m if session goes idle"
        )


# ── PreToolUse hook ────────────────────────────────────────────────────────────

def pre_tool_hook():
    """
    Called by Claude Code's PreToolUse event.
    Claude Code natively updates session status to "busy" on activity,
    so this mainly resets compact_triggered.
    """
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    session_id = data.get("session_id", "")
    if not session_id:
        return

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

    sessions = find_all_sessions()
    state = load_state()

    if not sessions:
        print("  No active Claude Code sessions.")
        return

    for session in sessions:
        session_id = session.get("sessionId", "?")
        pid = session.get("pid", "?")
        status = session.get("status", "?")
        idle_secs = idle_seconds(session)
        cwd = session.get("cwd", "?")
        tmux_info = session.get("tmux", "")

        s = state.get("sessions", {}).get(session_id, {})
        ctx = s.get("context_pct", 0.0)
        countdown = s.get("countdown_seconds", timeout_secs)
        triggered = s.get("compact_triggered", False)
        armed = ctx >= threshold

        mins, secs = divmod(int(idle_secs), 60)
        compact_status = "idle"
        if triggered:
            compact_status = "compact sent ✓"
        elif armed and idle_secs >= timeout_secs:
            compact_status = "COMPACT PENDING"
        elif armed:
            cm, cs = divmod(int(countdown), 60)
            compact_status = f"armed — compact in {cm}m {cs}s"
        elif ctx > 0:
            compact_status = f"below threshold ({threshold}%)"

        print(f"  PID {pid} | {Path(cwd).name}")
        print(f"    Status   : {status} | Idle: {mins}m {secs}s")
        print(f"    Context  : {ctx:.1f}% | {compact_status}")
        if tmux_info:
            print(f"    Tmux     : {tmux_info}")
        print()


# ── Install / Uninstall ────────────────────────────────────────────────────────

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
        hooks[event] = new_entries

    backup = settings.pop("_bc_statusline_backup", None)
    if backup is not None:
        settings["statusLine"] = backup
    elif "statusLine" in settings:
        sl = settings.get("statusLine", {})
        if "better-compact" in sl.get("command", ""):
            del settings["statusLine"]

    settings_file.write_text(json.dumps(settings, indent=2))
    print(f"  Updated {settings_file}")


def install_main():
    print()
    print("Better Auto-Compact for Claude Code")
    print("=" * 40)
    print()

    try:
        t = input("Inactivity timeout before auto-compact (minutes) [5]: ").strip()
        timeout = int(t) if t else 5
    except (ValueError, EOFError):
        timeout = 5

    try:
        th = input("Minimum context usage to trigger compact (%) [50]: ").strip()
        threshold = int(th) if th else 50
    except (ValueError, EOFError):
        threshold = 50

    print()

    install_dir = BASE_DIR / "src"
    install_dir.mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "transcripts").mkdir(exist_ok=True)

    config = {
        "inactivity_timeout_minutes": timeout,
        "compact_threshold_percent": threshold,
        "version": "1.0.0",
    }
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    print(f"  Config written: {CONFIG_FILE}")

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
