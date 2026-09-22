#!/usr/bin/env node
/**
 * Better Auto-Compact status line for Claude Code.
 *
 * Drop-in replacement for ctx_monitor.js that adds a compact countdown.
 * Installed by install.sh → referenced in ~/.claude/settings.json.
 *
 * Reads the same stdin format as ctx_monitor.js:
 *   { session_id, transcript_path, model, cost }
 *
 * Also reads ~/.claude/better-compact/compact-state.json for countdown data
 * written by the background daemon.
 */

"use strict";

const fs = require("fs");
const path = require("path");
const os = require("os");

// ── Input ─────────────────────────────────────────────────────────────────────
const input = readJSON(0); // stdin
const sessionId = String(input.session_id ?? "");
const transcript = input.transcript_path;
const model = input.model || {};
const name = `\x1b[95m${String(model.display_name ?? "")}\x1b[0m`.trim();
const CONTEXT_WINDOW_DEFAULT = 200_000;
const costUsd = `\x1b[31m$${(Number(input.cost?.total_cost_usd) || 0).toFixed(2)}\x1b[0m`;

// ── State file ────────────────────────────────────────────────────────────────
const STATE_FILE = path.join(os.homedir(), ".claude", "better-compact", "compact-state.json");

// ── Helpers ───────────────────────────────────────────────────────────────────

function readJSON(fd) {
  try {
    return JSON.parse(fs.readFileSync(fd, "utf8"));
  } catch {
    return {};
  }
}

function readStateFile() {
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
  } catch {
    return { sessions: {} };
  }
}

function color(p) {
  if (p >= 90) return "\x1b[31m"; // red
  if (p >= 70) return "\x1b[33m"; // yellow
  return "\x1b[32m";              // green
}

const comma = (n) =>
  new Intl.NumberFormat("en-US").format(Math.max(0, Math.floor(Number(n) || 0)));

function usedTotal(u) {
  return (
    (u?.input_tokens ?? 0) +
    (u?.output_tokens ?? 0) +
    (u?.cache_read_input_tokens ?? 0) +
    (u?.cache_creation_input_tokens ?? 0)
  );
}

function syntheticModel(j) {
  const m = String(j?.message?.model ?? "").toLowerCase();
  return m === "<synthetic>" || m.includes("synthetic");
}

function assistantMessage(j) {
  return j?.message?.role === "assistant";
}

function subContext(j) {
  return j?.isSidechain === true;
}

function contentNoResponse(j) {
  const c = j?.message?.content;
  return (
    Array.isArray(c) &&
    c.some(
      (x) =>
        x &&
        x.type === "text" &&
        /no\s+response\s+requested/i.test(String(x.text))
    )
  );
}

function parseTs(j) {
  const t = j?.timestamp;
  const n = Date.parse(t);
  return Number.isFinite(n) ? n : -Infinity;
}

// ── Context calculation (same logic as ctx_monitor.js) ─────────────────────────

function newestMainUsageByTimestamp() {
  if (!transcript) return null;
  let latestTs = -Infinity;
  let latestUsage = null;

  let lines;
  try {
    lines = fs.readFileSync(transcript, "utf8").split(/\r?\n/);
  } catch {
    return null;
  }

  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (!line) continue;

    let j;
    try {
      j = JSON.parse(line);
    } catch {
      continue;
    }
    const u = j.message?.usage;
    if (
      subContext(j) ||
      syntheticModel(j) ||
      j.isApiErrorMessage === true ||
      usedTotal(u) === 0 ||
      contentNoResponse(j) ||
      !assistantMessage(j)
    )
      continue;

    const ts = parseTs(j);
    if (ts > latestTs) {
      latestTs = ts;
      latestUsage = u;
    } else if (ts === latestTs && usedTotal(u) > usedTotal(latestUsage)) {
      latestUsage = u;
    }
  }
  return latestUsage ? { usage: latestUsage } : null;
}

// ── Compact countdown ──────────────────────────────────────────────────────────

// livePct: the statusline's own freshly-computed context percentage (not daemon state)
// Using live pct avoids showing stale armed status from the 10s daemon tick.
function compactCountdownLabel(livePct) {
  if (!sessionId) return "";

  const state = readStateFile();
  // threshold comes from daemon state; fall back to 40 if daemon hasn't written it yet
  const threshold = Number(state.threshold ?? 0) || 40;

  if (livePct < threshold) {
    return ` \x1b[90m| auto-compact at ${threshold}%\x1b[0m`;
  }

  // At or above threshold — show live countdown from daemon state
  const session = state.sessions?.[sessionId];
  if (!session) return ` \x1b[90m| auto-compact at ${threshold}%\x1b[0m`;

  // Use deadline timestamp for a live countdown — accurate every poll, no daemon lag
  const deadline = Number(session.deadline ?? 0);
  const secs = deadline > 0
    ? Math.round(deadline - Date.now() / 1000)
    : Math.round(Number(session.countdown_seconds ?? 0)); // fallback for old state

  if (secs <= 0) {
    return " \x1b[31m| ⏱ compact now!\x1b[0m";
  }

  const m = Math.floor(secs / 60);
  const s = secs % 60;
  const countdown = m > 0 ? `${m}m ${s}s` : `${s}s`;
  const c = livePct >= 90 ? "\x1b[31m" : "\x1b[33m";
  return ` ${c}| ⏱ compact in ${countdown}\x1b[0m`;
}

// ── Output ─────────────────────────────────────────────────────────────────────

const result = newestMainUsageByTimestamp();
const sessionIdLabel = `\x1b[90m${sessionId}\x1b[0m`;

if (!result) {
  const countdown = compactCountdownLabel(0);
  process.stdout.write(
    `${name} | \x1b[36mcontext window usage starts after your first question.\x1b[0m | cost: ${costUsd}${countdown}\nsession: ${sessionIdLabel}\n`
  );
  process.exit(0);
}

const { usage } = result;
const CONTEXT_WINDOW = Number(input.context_window?.context_window_size) || CONTEXT_WINDOW_DEFAULT;
const used = usedTotal(usage);
const pct = CONTEXT_WINDOW > 0 ? Math.round((used * 1000) / CONTEXT_WINDOW) / 10 : 0;

const usagePercentLabel = `${color(pct)}context used ${pct.toFixed(1)}%\x1b[0m`;
const usageCountLabel = `\x1b[33m(${comma(used)}/${comma(CONTEXT_WINDOW)})\x1b[0m`;
const countdown = compactCountdownLabel(pct);

process.stdout.write(
  `${name} | ${usagePercentLabel} - ${usageCountLabel} | cost: ${costUsd}${countdown}\nsession: ${sessionIdLabel}\n`
);
