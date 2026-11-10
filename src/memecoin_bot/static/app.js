"use strict";

const state = { csrf: null, status: null, operations: null, previewId: null, paperCloseId: null, toastTimer: null, automationTimer: null, candidatePayload: null, candidateFilter: "shortlist", radarExpanded: false };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function text(selector, value) {
  const node = $(selector);
  if (node) node.textContent = String(value);
}

function formatSol(lamports, digits = 5) {
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(Number(lamports) / 1_000_000_000)} SOL`;
}

function formatUsd(value) {
  if (value == null || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", notation: number >= 1_000_000 ? "compact" : "standard", maximumFractionDigits: number < 1 ? 6 : 2 }).format(number);
}

function formatSignedSol(lamports) {
  if (lamports == null) return "—";
  const value = Number(lamports) / 1_000_000_000;
  const sign = value > 0 ? "+" : "";
  return `${sign}${new Intl.NumberFormat(undefined, { maximumFractionDigits: 9 }).format(value)} SOL`;
}

function formatMove(value) {
  if (value == null || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? `${number > 0 ? "+" : ""}${number.toFixed(2)}%` : "—";
}

function discoverySourceLabel(source) {
  if (source === "manual_call") return "Call Inbox";
  if (source === "pumpfun_api") return "Pump.fun API";
  if (source === "dexscreener_profile") return "DEX Screener";
  if (source === "dexscreener_boost") return "DEX Boost";
  if (source === "dexscreener_top_boost") return "DEX Top Boost";
  if (source === "dexscreener_takeover") return "Community Takeover";
  if (source?.startsWith("pAMM")) return "PumpSwap";
  return "Pump on-chain";
}

function short(value, head = 5, tail = 4) {
  if (!value) return "—";
  return value.length > head + tail + 2 ? `${value.slice(0, head)}…${value.slice(-tail)}` : value;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.hidden = false;
  window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => { node.hidden = true; }, 4200);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body) headers.set("Content-Type", "application/json");
  if (state.csrf && options.method && options.method !== "GET") headers.set("X-CSRF-Token", state.csrf);
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  let payload = {};
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/auth/pair") showPairScreen();
    const detail = payload.message || payload.detail;
    const message = typeof detail === "string" ? detail : "The request could not be completed";
    throw new Error(message);
  }
  return payload;
}

function showPairScreen() {
  state.csrf = null;
  window.clearInterval(state.automationTimer);
  state.automationTimer = null;
  $("#appShell").hidden = true;
  $("#pairScreen").hidden = false;
  $("#pairCode").focus();
}

function showApp() {
  $("#pairScreen").hidden = true;
  $("#appShell").hidden = false;
  if (!state.automationTimer) {
    state.automationTimer = window.setInterval(() => {
      if (state.csrf && location.hash === "#discovery") {
        loadAutomation().catch(() => {});
      }
    }, 5000);
  }
}

async function restoreSession() {
  const auth = await api("/api/auth/status");
  if (!auth.authenticated) return showPairScreen();
  state.csrf = auth.csrf_token;
  showApp();
  const results = await Promise.allSettled([loadStatus(), loadOrders(true)]);
  if (results[0].status === "rejected") {
    text("#rpcStatus", "RPC unavailable");
    $("#rpcDot").className = "status-dot offline";
    toast("RPC status is unavailable. Paper records remain accessible.");
  }
  if (results[1].status === "rejected") toast("Order activity could not be loaded.");
  await route();
}

async function loadStatus() {
  const [status, operations] = await Promise.all([
    api("/api/status"),
    api("/api/operations/health"),
  ]);
  state.status = status;
  state.operations = operations;
  text("#balanceValue", formatSol(status.balance_lamports));
  text("#exposureValue", formatSol(status.daily_exposure_lamports));
  text("#exposureLimit", `Daily cap ${formatSol(status.limits.max_daily_lamports)}`);
  text("#jupiterValue", status.jupiter_configured ? "READY" : "MISSING");
  text("#sidebarMode", "Paper only");
  $("#sidebarMode").classList.remove("live");
  text("#walletShort", short(status.wallet, 6, 6));
  $("#copyWallet").dataset.wallet = status.wallet;
  text("#rpcStatus", status.rpc_health === "ok" ? "RPC online" : "RPC unavailable");
  $("#rpcDot").className = `status-dot ${status.rpc_health === "ok" ? "online" : "offline"}`;
  renderReadiness(status, operations);
  renderLimits(status.limits);
}

function renderOverview(portfolio) {
  const openPositions = portfolio.positions.filter((position) => position.status === "open");
  const openRentReserved = openPositions.reduce((total, position) => {
    const rent = Number(position.entry_execution?.ata_rent_lamports);
    return total + (Number.isSafeInteger(rent) && rent > 0 ? rent : 0);
  }, 0);
  const cap = Number(state.status?.strategy?.max_open_positions || 0);
  const forward = portfolio.forward_performance || {};
  const completeExits = Number(forward.autonomous?.trades || 0);
  const preliminaryTarget = Number(forward.minimum_interpretable_trades || 30);
  const validationTarget = 200;
  const worker = state.operations?.workers?.paper_automation || {};
  const exitWorker = state.operations?.workers?.paper_exit_monitor || {};
  const exitIssues = state.operations?.degradation_reasons?.paper_exit_monitor || [];
  const collector = state.operations?.workers?.market_collector || {};
  const collectorIssues = state.operations?.degradation_reasons?.market_collector || [];
  const candleCapacity = state.operations?.ohlcv_capacity || {};
  const workerKnown = Boolean(state.operations?.workers?.paper_automation && state.operations?.workers?.paper_exit_monitor);
  const cachedMarks = openPositions.filter((position) => position.quote_state === "recent_mark").length;
  const staleMarks = openPositions.filter((position) => position.quote_state === "stale").length;
  const exitFailures = Number(exitWorker.last_result?.exit_failures || 0);
  const quoteFailures = Number(exitWorker.last_result?.quote_failures || 0);
  const entryPaused = Boolean(worker.last_result?.entry_paused);

  text("#overviewOpenPositions", `${openPositions.length} / ${cap || "—"}`);
  text("#overviewPositionLimit", cap ? `${Math.max(0, cap - openPositions.length)} slots available` : "Position cap unavailable");
  const unvaluedPositions = Number(portfolio.unvalued_position_count || 0);
  text("#overviewEquity", unvaluedPositions ? "—" : formatSol(portfolio.equity_lamports));
  text("#overviewEquityNote", unvaluedPositions
    ? `${unvaluedPositions} open position${unvaluedPositions === 1 ? "" : "s"} without an exit mark`
    : !openPositions.length ? "All cash; no open positions" : staleMarks
    ? `${staleMarks} old or missing marks · estimate`
    : cachedMarks ? `${cachedMarks} saved marks · estimate` : "Based on current exit quotes");
  text("#overviewEvidence", `${completeExits} / ${validationTarget}`);
  text("#overviewEvidenceNote", `${preliminaryTarget} trades needed for a preliminary read`);
  const campaignStartedAt = Date.parse(portfolio.paper_campaign?.started_at || "");
  if (Number.isFinite(campaignStartedAt)) {
    const continuity = state.operations?.paper_continuity || {};
    const streakStart = Date.parse(continuity.current_streak_started_at || "");
    if (continuity.state === "current" && Number.isFinite(streakStart)) {
      const verifiedDays = Math.max(0, Math.floor(Number(continuity.current_streak_seconds || 0) / 86_400));
      const earliestWindowEnd = new Date(Math.max(campaignStartedAt, streakStart) + 30 * 86_400_000);
      const earliestDate = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(earliestWindowEnd);
      text("#overviewValidationTimeline", `Recorded worker continuity: ${Math.min(verifiedDays, 30)} / 30 days · earliest possible window end ${earliestDate}. Market data quality and trade results require separate validation.`);
    } else {
      text("#overviewValidationTimeline", "Paper campaign started, but worker continuity is unverified. Cycle history is recorded prospectively; a restart, error, or extended heartbeat gap resets the streak.");
    }
  } else {
    text("#overviewValidationTimeline", "Paper validation has no recorded start time. Start a forward paper run to begin the observation window.");
  }
  text("#overviewWorker", !workerKnown ? "Unavailable" : worker.running && exitWorker.running ? (exitIssues.length ? "Exit risk" : worker.last_error ? "Degraded" : entryPaused ? "Entry locked" : "Running") : worker.state === "paused" ? "Paused" : "Stopped");
  text("#overviewWorkerNote", !workerKnown ? "Refresh system status" : exitWorker.last_completed_at ? `Exit check ${formatTimestamp(exitWorker.last_completed_at)}` : "No completed exit check yet");

  let heading = "Review verified candidates";
  let copy = "Discovery shows the audit, signal score, and rejection reason before a paper entry.";
  let link = "#discovery";
  let label = "Open Discovery";
  if (!workerKnown) {
    heading = "Worker status unavailable";
    copy = "Refresh the paper desk before making a decision about the worker.";
    link = "#paper";
    label = "Open Paper Portfolio";
  } else if (!worker.running || !exitWorker.running || worker.last_error || exitWorker.last_error || exitIssues.includes("exit_monitor_stale") || exitIssues.includes("exit_monitor_unavailable")) {
    heading = "Paper worker needs attention";
    copy = exitIssues.includes("exit_monitor_stale") ? "Exit checks are overdue. New paper entries are blocked until the monitor completes a fresh check." : exitIssues.includes("exit_monitor_unavailable") ? "The exit monitor has no verified recent check. New paper entries are blocked until it completes one." : "Open Discovery to inspect its latest event and resume the decision loop when appropriate.";
  } else if (exitFailures) {
    heading = "An automated exit failed";
    copy = `${exitFailures} exit attempt${exitFailures === 1 ? "" : "s"} failed in the last worker cycle. New paper entries are paused while the worker retries exits.`;
    link = "#paper";
    label = "Open Paper Portfolio";
  } else if (quoteFailures) {
    heading = "Exit quotes unavailable";
    copy = `${quoteFailures} open position${quoteFailures === 1 ? " had" : "s had"} no executable exit quote in the last worker cycle. Stops and profit targets require fresh quotes.`;
    link = "#paper";
    label = "Open Paper Portfolio";
  } else if (!collector.running || collectorIssues.length) {
    heading = "Check candle collection";
    copy = collectorIssues.includes("ohlcv_rate_limited")
      ? "The candle provider is rate limited. Recent bars may be missing; new entries require complete, recent history."
      : collectorIssues.includes("ohlcv_capacity_below_demand")
        ? `Candle collection delivered ${Number(candleCapacity.observed_successful_requests_per_minute).toFixed(1)} calls/minute; the current market-ready set needs at least ${Number(candleCapacity.minimum_calls_per_minute_to_refresh_each_ready_mint_every_120s).toFixed(1)}. Entries with stale bars remain blocked.`
      : collectorIssues.includes("dex_requests_failed")
        ? "A DEX market-data batch failed. Discovery may be incomplete until the next successful collection."
        : "The real-candle collector is unavailable or degraded. New entries require complete, recent history.";
    link = "#lab";
    label = "Open Strategy Lab";
  } else if (entryPaused || (cap > 0 && openPositions.length >= cap)) {
    heading = "Monitor existing positions";
    copy = entryPaused ? "New entries are paused by policy. The worker continues monitoring exits." : "The paper position cap is full. The worker continues checking exits and logging decisions.";
    link = "#paper";
    label = "Open Paper Portfolio";
  } else if (staleMarks) {
    heading = "Refresh paper marks";
    copy = `${staleMarks} open position${staleMarks === 1 ? " has" : "s have"} an old or missing saved mark. Refresh quotes to see a current valuation.`;
    link = "#paper";
    label = "Open Paper Portfolio";
  }
  text("#overviewFocusTitle", heading);
  text("#overviewFocusCopy", copy);
  const legacyOpen = Number(forward.open_positions_without_signal_evidence || 0);
  text("#overviewFocusDetail", `${completeExits} complete autonomous exits · ${legacyOpen} open without full entry evidence · ${Number(forward.incomplete_closed_excluded || 0)} incomplete closed excluded · live execution locked`);
  const action = $("#overviewFocusLink");
  action.href = link;
  action.textContent = label;
}

async function loadOverview() {
  const portfolio = await api("/api/paper/portfolio");
  renderOverview(portfolio);
}

function renderReadiness(status, operations) {
  const paperWorker = operations.workers?.paper_automation || {};
  const exitWorker = operations.workers?.paper_exit_monitor || {};
  const exitIssues = operations.degradation_reasons?.paper_exit_monitor || [];
  const backup = operations.backup || {};
  const rows = [
    ["RPC connection", status.rpc_health === "ok" ? "ONLINE" : "ERROR", status.rpc_health === "ok"],
    ["Jupiter credential", status.jupiter_configured ? "CONFIGURED" : "MISSING", status.jupiter_configured],
    ["Signing wallet", short(status.wallet, 6, 5), Boolean(status.wallet)],
    ["Live execution", status.live_enabled ? "ARMED" : "LOCKED", !status.live_enabled],
    ["State database", operations.database?.quick_check === "ok" ? "VERIFIED" : "ERROR", operations.database?.quick_check === "ok"],
    ["Paper worker history", paperWorker.durable ? `${Number(paperWorker.cycles_completed || 0)} CYCLES` : "VOLATILE", Boolean(paperWorker.durable)],
    ["Paper exit monitor", exitIssues.includes("exit_monitor_stale") ? "STALE" : exitIssues.length ? "DEGRADED" : exitWorker.running ? "RUNNING" : "STOPPED", Boolean(exitWorker.running) && !exitIssues.length],
    ["State backup", backup.last_backup_at ? "VERIFIED" : backup.enabled ? "PENDING" : "DISABLED", Boolean(backup.last_backup_at) || !backup.enabled],
  ];
  const root = $("#readinessList");
  root.replaceChildren();
  for (const [label, value, healthy] of rows) {
    const row = document.createElement("div");
    const labelNode = document.createElement("span");
    const valueNode = document.createElement("strong");
    labelNode.textContent = label;
    valueNode.textContent = value;
    valueNode.className = healthy ? "check-good" : "check-bad";
    row.append(labelNode, valueNode);
    root.append(row);
  }
}

function renderLimits(limits) {
  const entries = [
    ["Maximum Trade", formatSol(limits.max_trade_lamports), "Largest permitted new SOL exposure per order."],
    ["Daily Exposure", formatSol(limits.max_daily_lamports), "Total confirmed or pending SOL buys permitted each UTC day."],
    ["Maximum Slippage", `${(limits.max_slippage_bps / 100).toFixed(2)}%`, "Server rejects wider trade requests."],
    ["Price Impact", `${(limits.max_price_impact_bps / 100).toFixed(2)}%`, "Quote is rejected above this estimate."],
    ["Priority Fee Cap", formatSol(limits.max_priority_fee_lamports, 6), "Maximum total priority fee after compute-unit bounding."],
  ];
  const root = $("#limitsList");
  root.replaceChildren();
  for (const [name, value, description] of entries) {
    const row = document.createElement("div");
    row.className = "limit-row";
    for (const [tag, content] of [["strong", name], ["strong", value], ["span", description]]) {
      const node = document.createElement(tag);
      node.textContent = content;
      row.append(node);
    }
    root.append(row);
  }
}

function orderSide(order) {
  return order.input_mint === "So11111111111111111111111111111111111111112" ? "BUY" : "SELL";
}

function appendOrderRow(root, order, compact = false) {
  const row = document.createElement("tr");
  const time = new Intl.DateTimeFormat(undefined, compact ? { hour: "2-digit", minute: "2-digit" } : { dateStyle: "medium", timeStyle: "short" }).format(new Date(order.created_at));
  const values = compact
    ? [time, orderSide(order), orderSide(order) === "BUY" ? formatSol(order.input_amount) : order.input_amount.toLocaleString(), order.status, order.signature ? short(order.signature) : "—"]
    : [time, short(order.client_order_id, 8, 5), `${short(order.input_mint)} → ${short(order.output_mint)}`, orderSide(order) === "BUY" ? formatSol(order.input_amount) : order.input_amount.toLocaleString(), order.status, order.signature ? short(order.signature, 7, 6) : "—"];
  values.forEach((value, index) => {
    const cell = document.createElement("td");
    cell.textContent = value;
    if ((compact && index === 3) || (!compact && index === 4)) cell.className = "status-text";
    row.append(cell);
  });
  root.append(row);
}

async function loadOrders(overviewOnly = false) {
  const payload = await api("/api/orders");
  const overview = $("#overviewOrders");
  overview.replaceChildren();
  payload.orders.slice(0, 5).forEach((item) => appendOrderRow(overview, item, true));
  $("#overviewOrdersEmpty").hidden = payload.orders.length > 0;
  if (!overviewOnly) {
    const table = $("#ordersTable");
    table.replaceChildren();
    payload.orders.forEach((item) => appendOrderRow(table, item, false));
    $("#ordersEmpty").hidden = payload.orders.length > 0;
  }
}

async function loadPositions() {
  const payload = await api("/api/positions");
  const table = $("#positionsTable");
  table.replaceChildren();
  for (const position of payload.positions) {
    const row = document.createElement("tr");
    const values = [short(position.position_id, 10, 4), short(position.mint, 7, 6), formatSol(position.entry_lamports), position.initial_token_amount.toLocaleString(), position.take_profit_taken ? "YES" : "NO", position.status];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index === 5) cell.className = "status-text";
      row.append(cell);
    });
    table.append(row);
  }
  $("#positionsEmpty").hidden = payload.positions.length > 0;
}

function tokenCell(primary, secondary) {
  const cell = document.createElement("td");
  const name = document.createElement("strong");
  const detail = document.createElement("small");
  name.textContent = primary;
  detail.textContent = secondary;
  cell.className = "token-cell";
  cell.append(name, detail);
  return cell;
}

function statusBadge(label, tone) {
  const badge = document.createElement("span");
  badge.className = `status-badge ${tone}`;
  badge.textContent = label;
  return badge;
}

function renderHotRadar(candidates) {
  const ranked = candidates
    .filter((candidate) => Number.isFinite(Number(candidate.hot_score)))
    .sort((left, right) => Number(right.hot_score) - Number(left.hot_score))
    .slice(0, 15);
  const visible = state.radarExpanded ? ranked : ranked.slice(0, 5);
  const table = $("#hotRadarTable");
  table.replaceChildren();
  visible.forEach((candidate, index) => {
    const activity = candidate.market_activity || {};
    const buys = Number(activity.txns_5m_buys || 0);
    const sells = Number(activity.txns_5m_sells || 0);
    const total = buys + sells;
    const buyPercent = total ? buys / total * 100 : 0;
    const row = document.createElement("tr");
    const flags = candidate.hot_risk_flags || [];
    row.dataset.heat = candidate.hot_state || "quiet";
    if (flags.includes("paid_boost_present")) row.dataset.promoted = "true";
    const rank = document.createElement("td");
    rank.textContent = String(index + 1).padStart(2, "0");
    rank.className = "hot-rank";
    row.append(rank, tokenCell(candidate.symbol || short(candidate.mint, 7, 5), candidate.name || short(candidate.mint, 10, 8)));

    const heat = document.createElement("td");
    const heatWrap = document.createElement("div");
    heatWrap.className = "heat-signal";
    const heatLabel = document.createElement("div");
    heatLabel.className = "heat-signal-label";
    const heatScore = document.createElement("strong");
    heatScore.textContent = String(candidate.hot_score);
    const heatState = document.createElement("span");
    heatState.textContent = candidate.hot_state || "quiet";
    heatLabel.append(heatScore, heatState);
    const heatTrack = document.createElement("span");
    heatTrack.className = "signal-track";
    const heatFill = document.createElement("i");
    heatFill.style.width = `${Math.max(0, Math.min(100, Number(candidate.hot_score) || 0))}%`;
    heatTrack.append(heatFill);
    heatWrap.append(heatLabel, heatTrack);
    heat.append(heatWrap);
    row.append(heat);

    const flow = document.createElement("td");
    if (total) {
      const flowLabel = document.createElement("span");
      flowLabel.className = "flow-label";
      flowLabel.textContent = `${buyPercent.toFixed(0)}% buy · ${total} tx`;
      const flowTrack = document.createElement("span");
      flowTrack.className = "flow-track";
      const flowBuy = document.createElement("i");
      flowBuy.style.width = `${buyPercent}%`;
      flowTrack.append(flowBuy);
      flow.append(flowLabel, flowTrack);
    } else {
      flow.textContent = "No flow";
    }
    flow.className = buyPercent >= 55 ? "pnl-positive flow-cell" : buyPercent < 45 && total ? "pnl-negative flow-cell" : "flow-cell";
    row.append(flow);

    for (const value of [formatUsd(activity.volume_5m_usd), formatUsd(activity.volume_1h_usd)]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    const move = document.createElement("td");
    move.textContent = formatMove(activity.price_change_5m);
    const moveNumber = Number(activity.price_change_5m);
    if (Number.isFinite(moveNumber)) move.className = moveNumber >= 0 ? "pnl-positive" : "pnl-negative";
    row.append(move);
    for (const value of [formatUsd(candidate.liquidity_usd), formatUsd(activity.market_cap_usd)]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    const risk = document.createElement("td");
    if (flags.length) {
      const riskList = document.createElement("div");
      riskList.className = "risk-chip-list";
      for (const flag of flags.slice(0, 2)) {
        const chip = document.createElement("span");
        chip.textContent = flag.replaceAll("_", " ");
        riskList.append(chip);
      }
      if (flags.length > 2) {
        const more = document.createElement("span");
        more.textContent = `+${flags.length - 2} more`;
        riskList.append(more);
      }
      risk.append(riskList);
      risk.className = "attention-risk";
    } else {
      risk.textContent = "No attention flag";
      risk.className = "pnl-positive organic-label";
    }
    row.append(risk);
    row.title = (candidate.hot_reasons || []).join(" · ");
    table.append(row);
  });
  const hot = ranked.filter((candidate) => candidate.hot_state === "hot").length;
  const organic = ranked.filter((candidate) => !(candidate.hot_risk_flags || []).includes("paid_boost_present")).length;
  text("#hotRadarHotCount", hot);
  text("#hotRadarOrganicCount", organic);
  text("#marketWatchCount", `${ranked.length} tracked · ${hot} hot`);
  text("#hotRadarUpdated", new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date()));
  const toggle = $("#radarToggle");
  toggle.hidden = ranked.length <= 5;
  toggle.textContent = state.radarExpanded ? "Show top 5" : `Show all ${ranked.length}`;
  toggle.setAttribute("aria-expanded", String(state.radarExpanded));
  $("#hotRadarEmpty").hidden = ranked.length > 0;
}

function renderCandidates(payload) {
  state.candidatePayload = payload;
  const table = $("#candidatesTable");
  table.replaceChildren();
  text("#scanPrograms", `${payload.program_ids?.length || 2} verified programs`);
  const allCandidates = payload.candidates || [];
  const shortlist = allCandidates.filter((candidate) => candidate.audit_status === "safe");
  const visibleCandidates = state.candidateFilter === "shortlist" ? shortlist : allCandidates;
  text("#shortlistCount", shortlist.length);
  text("#allCandidateCount", allCandidates.length);
  text("#candidateScopeSummary", state.candidateFilter === "shortlist"
    ? `${shortlist.length} passed the on-chain audit. Signal and data freshness still decide paper entry.`
    : `Showing all ${allCandidates.length} scanned candidates, including rejected and unaudited tokens.`);
  for (const [selector, selected] of [["#showShortlist", state.candidateFilter === "shortlist"], ["#showAllCandidates", state.candidateFilter === "all"]]) {
    const button = $(selector);
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  }
  const latestDecision = new Map();
  for (const decision of payload.decisions || []) {
    if (!latestDecision.has(decision.mint)) latestDecision.set(decision.mint, decision);
  }
  for (const candidate of visibleCandidates) {
    const row = document.createElement("tr");
    const sourceLabel = discoverySourceLabel(candidate.source_program);
    const token = tokenCell(candidate.symbol || short(candidate.mint, 7, 5), `${candidate.name || short(candidate.mint, 10, 8)} · ${sourceLabel}`);
    token.title = `Mint: ${candidate.mint} · Source: ${sourceLabel}`;
    row.append(token);

    const auditCell = document.createElement("td");
    const tone = candidate.audit_status === "safe" ? "safe" : candidate.audit_status === "blocked" ? "danger" : "";
    auditCell.append(statusBadge(candidate.audit_status, tone));
    auditCell.title = candidate.audit_summary || "No audit detail";
    row.append(auditCell);

    const decision = latestDecision.get(candidate.mint);
    const scoreCell = document.createElement("td");
    scoreCell.textContent = decision ? `${decision.score}/100` : "—";
    row.append(scoreCell);
    const decisionCell = document.createElement("td");
    decisionCell.append(statusBadge(decision?.verdict || "not scored", decision?.verdict === "eligible" ? "safe" : decision?.verdict === "reject" ? "danger" : ""));
    if (decision?.reasons?.length) decisionCell.title = decision.reasons.join(" · ");
    row.append(decisionCell);

    const marketCell = document.createElement("td");
    marketCell.className = "candidate-market";
    const liquidity = document.createElement("strong");
    liquidity.textContent = formatUsd(candidate.liquidity_usd);
    const volume = document.createElement("small");
    volume.textContent = `24h vol ${formatUsd(candidate.volume_24h_usd)}`;
    marketCell.append(liquidity, volume);
    row.append(marketCell);

    const actionCell = document.createElement("td");
    const action = document.createElement("button");
    action.type = "button";
    action.className = "table-action";
    action.textContent = "Review in Paper";
    action.dataset.paperMint = candidate.mint;
    action.disabled = candidate.audit_status !== "safe";
    action.title = action.disabled ? "On-chain audit has not passed" : "Open the paper entry form for an independent audit and simulation";
    actionCell.append(action);
    row.append(actionCell);
    table.append(row);
  }
  $("#candidatesEmpty").hidden = visibleCandidates.length > 0;
  text("#candidatesEmpty", allCandidates.length ? "No audited-safe candidates right now. Select All scanned to review rejected and pending tokens." : "No candidates recorded. Run a bounded scan to begin.");
  renderHotRadar(allCandidates);
  renderDecisions(payload.decisions || []);
}

async function loadCandidates() {
  const [payload, decisionPayload] = await Promise.all([
    api("/api/discovery/candidates"),
    api("/api/strategy/decisions"),
  ]);
  renderCandidates({ ...payload, decisions: decisionPayload.decisions });
}

function safeExternalHref(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname ? url.href : null;
  } catch {
    return null;
  }
}

function renderNarrative(payload) {
  const signals = Array.isArray(payload.signals) ? payload.signals : [];
  const items = Array.isArray(payload.items) ? payload.items : [];
  const collector = payload.collector || {};
  const providerState = collector.last_result?.narrative?.providers || {};
  const modelResearch = payload.openai_research || {};
  const lastModelRun = collector.last_result?.narrative?.openai_research || {};
  text("#narrativeModelState", modelResearch.enabled ? "OpenAI research on" : "OpenAI research off");
  const providerDegraded = Object.values(providerState).some((value) => value?.status === "degraded")
    || lastModelRun.status === "degraded";
  const badge = $("#narrativeCollectorBadge");
  badge.textContent = collector.last_error || providerDegraded ? "Degraded" : collector.running ? "Live" : "Stopped";
  badge.className = `status-badge ${collector.last_error || providerDegraded ? "danger" : collector.running ? "safe" : ""}`;
  const sourceStatuses = Object.entries(providerState).map(([name, value]) => `${name}: ${value.status}`);
  if (modelResearch.enabled) sourceStatuses.push(`OpenAI research: ${lastModelRun.status || "waiting"}`);
  badge.title = collector.last_error || sourceStatuses.join(" · ") || "Public evidence collector status";
  text("#narrativeLinked", signals.length);
  text("#narrativeHighConfidence", signals.filter((signal) => signal.confidence === "high").length);
  text("#narrativeEvidenceCount", items.length);
  const enabledSources = Object.entries(payload.sources || {}).filter(([, enabled]) => enabled).map(([name]) => {
    const status = providerState[name]?.status;
    return `${name.toUpperCase()}${status === "degraded" ? " !" : ""}`;
  });
  text("#narrativeSources", enabledSources.length ? enabledSources.join(" + ") : "Disabled");

  const tags = $("#narrativeTags");
  tags.replaceChildren();
  for (const tag of (payload.top_tags || []).slice(0, 8)) {
    const chip = document.createElement("span");
    chip.textContent = `${String(tag.tag).replaceAll("_", " ")} · ${tag.mentions}`;
    tags.append(chip);
  }
  if (!tags.children.length) {
    const waiting = document.createElement("span");
    waiting.className = "narrative-tags-waiting";
    waiting.textContent = "Narrative clusters are still collecting";
    tags.append(waiting);
  }

  const signalTable = $("#narrativeSignalsTable");
  signalTable.replaceChildren();
  for (const signal of signals.slice(0, 12)) {
    const nowMs = Date.now();
    const refreshMs = Date.parse(signal.updated_at || "");
    const mentionMs = Date.parse(signal.latest_mention_at || "");
    const refreshAge = (nowMs - refreshMs) / 1000;
    const mentionAge = (nowMs - mentionMs) / 1000;
    const entryRecent = Number.isFinite(refreshAge) && refreshAge >= 0 && refreshAge <= 900
      && Number.isFinite(mentionAge) && mentionAge >= 0 && mentionAge <= 3600;
    const row = document.createElement("tr");
    row.dataset.signal = entryRecent ? signal.state || "quiet" : "quiet";
    row.append(tokenCell(signal.symbol || short(signal.mint, 7, 5), signal.name || short(signal.mint, 10, 8)));
    const score = document.createElement("td");
    const scoreWrap = document.createElement("div");
    scoreWrap.className = "narrative-score";
    const scoreLabel = document.createElement("span");
    scoreLabel.textContent = `${signal.score}/100`;
    const scoreState = document.createElement("small");
    scoreState.textContent = entryRecent ? signal.state || "quiet" : "older evidence";
    const scoreTrack = document.createElement("i");
    const scoreFill = document.createElement("b");
    scoreFill.style.width = `${Math.max(0, Math.min(100, Number(signal.score) || 0))}%`;
    scoreTrack.append(scoreFill);
    scoreWrap.append(scoreLabel, scoreState, scoreTrack);
    score.append(scoreWrap);
    row.append(score);
    const evidence = document.createElement("td");
    evidence.textContent = `${signal.unique_sources} src · ${signal.news_count} news · ${signal.social_count} social · ${Number.isFinite(mentionAge) && mentionAge >= 0 ? `latest ${formatDuration(Math.floor(mentionAge))} ago` : "mention time unknown"}`;
    row.append(evidence);
    const linkage = document.createElement("td");
    linkage.append(statusBadge(signal.explicit_contract_mentions ? `${signal.explicit_contract_mentions} contract` : "ticker/name", signal.confidence === "high" ? "safe" : ""));
    linkage.title = `${signal.confidence || "low"} confidence`;
    row.append(linkage);
    const risk = document.createElement("td");
    const flags = signal.risk_flags || [];
    risk.textContent = flags.length ? flags.map((flag) => flag.replaceAll("_", " ")).join(" · ") : entryRecent ? "No narrative flag" : "No entry bonus: older evidence";
    risk.className = flags.length ? "attention-risk" : "pnl-positive organic-label";
    row.append(risk);
    row.title = `${(signal.reasons || []).join(" · ")} · Entry bonus requires refresh within 15m and mention within 1h.`;
    signalTable.append(row);
  }
  $("#narrativeSignalsEmpty").hidden = signals.length > 0;

  const feed = $("#narrativeFeed");
  feed.replaceChildren();
  for (const item of items.slice(0, 12)) {
    const article = document.createElement("article");
    article.className = "narrative-item";
    const meta = document.createElement("div");
    meta.className = "narrative-item-meta";
    const source = document.createElement("span");
    source.textContent = `${item.source_kind === "news" ? "NEWS" : "SOCIAL"} · ${item.source_name}`;
    const time = document.createElement("time");
    time.dateTime = item.published_at || "";
    time.textContent = formatTimestamp(item.published_at, "Recent");
    meta.append(source, time);
    const heading = document.createElement("h3");
    const href = safeExternalHref(item.source_url);
    if (href) {
      const link = document.createElement("a");
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.referrerPolicy = "no-referrer";
      link.textContent = item.title;
      heading.append(link);
    } else {
      heading.textContent = item.title;
    }
    const details = document.createElement("div");
    details.className = "narrative-item-details";
    const tier = document.createElement("span");
    tier.textContent = String(item.evidence_tier || "unverified").replaceAll("_", " ");
    details.append(tier);
    for (const flag of (item.risk_flags || []).filter((flag) => flag !== "unverified_social_source").slice(0, 2)) {
      const warning = document.createElement("span");
      warning.className = "narrative-warning";
      warning.textContent = flag.replaceAll("_", " ");
      details.append(warning);
    }
    article.append(meta, heading, details);
    const assessment = item.model_assessment?.result;
    if (typeof assessment?.claim_summary === "string") {
      const summary = document.createElement("p");
      summary.className = "narrative-model-summary";
      summary.textContent = `Model summary of source claim: ${assessment.claim_summary}`;
      article.append(summary);
    }
    feed.append(article);
  }
  $("#narrativeFeedEmpty").hidden = items.length > 0;
}

async function loadNarrative() {
  renderNarrative(await api("/api/narrative"));
}

function formatDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return "—";
  if (value >= 86400) return `${Math.round(value / 86400)}d`;
  if (value >= 3600) return `${Math.round(value / 3600)}h`;
  if (value >= 60) return `${Math.round(value / 60)}m`;
  return `${value}s`;
}

function formatTimestamp(value, fallback = "—") {
  if (!value) return fallback;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? fallback : new Intl.DateTimeFormat(undefined, { dateStyle: "short", timeStyle: "short" }).format(date);
}

function renderAutomation(payload) {
  const consoleNode = $(".automation-console");
  consoleNode.dataset.state = payload.state;
  const badge = $("#automationBadge");
  const entryPaused = Boolean(payload.last_result?.entry_paused);
  const exitWorker = payload.exit_monitor || {};
  const exitIssues = payload.exit_monitor_issues || [];
  const quoteFailures = Number(exitWorker.last_result?.quote_failures || 0);
  const exitFailures = Number(exitWorker.last_result?.exit_failures || 0);
  consoleNode.dataset.entryPaused = String(entryPaused);
  const entryReason = String(payload.last_result?.pause_reason || "risk limit").replaceAll("_", " ");
  badge.textContent = payload.running && exitIssues.includes("exit_monitor_stale") ? "Exit monitor stale" : payload.running && exitIssues.includes("exit_monitor_unavailable") ? "Exit monitor unverified" : payload.running && !exitWorker.running ? "Exit monitor stopped" : payload.running && (quoteFailures || exitFailures) ? "Exit risk" : payload.running && entryPaused ? "Entry locked" : payload.state;
  badge.className = `status-badge ${payload.state === "degraded" || exitIssues.length ? "danger" : ["running", "working"].includes(payload.state) && !entryPaused ? "safe" : ""}`;
  $("#startAutomation").disabled = payload.running && exitWorker.running;
  $("#stopAutomation").disabled = !payload.running && !exitWorker.running;
  $("#runAutomationOnce").disabled = payload.running || payload.state === "working";
  const message = payload.last_error || exitWorker.last_error
    ? `Last cycle failed safely: ${payload.last_error || exitWorker.last_error}`
    : payload.running && exitIssues.includes("exit_monitor_stale")
      ? "Exit checks are overdue. New paper entries remain blocked until a fresh exit check completes."
    : payload.running && exitIssues.includes("exit_monitor_unavailable")
      ? "No recent exit check is verified. New paper entries remain blocked until the monitor completes one."
    : payload.running && !exitWorker.running
      ? "Exit monitor stopped. New paper entries are blocked; restart automation to resume exit checks."
    : payload.state === "paused"
      ? "Circuit breaker paused new paper entries. Review the latest event before restarting."
      : payload.running && exitFailures
        ? `${exitFailures} paper exit attempt${exitFailures === 1 ? " failed" : "s failed"} in the last monitor cycle. New entries are paused.`
      : payload.running && quoteFailures
        ? `${quoteFailures} open paper position${quoteFailures === 1 ? " has" : "s have"} no fresh exit quote. Stops and profit targets cannot be evaluated for ${quoteFailures === 1 ? "it" : "them"}.`
      : payload.running && entryPaused
        ? `Entries paused: ${entryReason}. Exits and signal logging continue.`
      : payload.running
        ? "Discovery and exit checks run independently. The exit monitor keeps checking positions while candidate audits run."
        : "Worker stopped. Paper positions and history remain saved locally.";
  text("#automationMessage", message);
  text("#automationCadence", `Every ${formatDuration(payload.interval_seconds)}`);
  text("#automationNext", payload.next_cycle_at ? formatTimestamp(payload.next_cycle_at) : payload.running ? "Cycle running" : "—");
  text("#automationCycles", Number(payload.cycles_completed || 0).toLocaleString());
  const ladder = (payload.policy.tp_levels_bps || []).map((level, index) => {
    const sell = payload.policy.tp_sell_bps?.[index] || 0;
    return `+${(level / 100).toFixed(0)}% → ${(sell / 100).toFixed(0)}%`;
  }).join(" · ");
  text("#automationEntryExit", ladder || "—");
  text("#automationTrailing", `Arm +${(payload.policy.trailing_activation_bps / 100).toFixed(0)}% · Trail ${(payload.policy.trailing_stop_bps / 100).toFixed(0)}%`);
  text("#automationBreaker", `-${(payload.policy.max_drawdown_bps / 100).toFixed(0)}% equity`);
  const latest = payload.events?.[0];
  text("#automationLatestEvent", latest?.message || "No autonomous cycles recorded.");
  text("#automationLatestTime", latest ? formatTimestamp(latest.created_at) : "");
}

async function loadAutomation() {
  const payload = await api("/api/automation/status");
  renderAutomation(payload);
  return payload;
}

function renderCalls(calls) {
  const table = $("#callsTable");
  table.replaceChildren();
  for (const call of calls.slice(0, 20)) {
    const row = document.createElement("tr");
    const values = [
      formatTimestamp(call.created_at),
      short(call.mint, 7, 5),
      call.source_label || "Direct paste",
      call.score == null ? "—" : `${call.score}/100`,
      call.verdict || "—",
      call.status,
      call.reason || "—",
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index === 5) cell.className = call.status === "opened" ? "pnl-positive" : call.status === "rejected" || call.status === "error" ? "pnl-negative" : "";
      row.append(cell);
    });
    table.append(row);
  }
  $("#callsEmpty").hidden = calls.length > 0;
}

async function loadCalls() {
  const payload = await api("/api/calls");
  renderCalls(payload.calls);
}

function renderDecisions(decisions) {
  const table = $("#decisionsTable");
  table.replaceChildren();
  const executionReasons = {
    stale_route: "stale quote",
    slippage_limit: "slippage limit",
    price_impact_limit: "price impact limit",
    modeled_slippage_limit: "modeled slippage limit",
    entry_liquidity_unavailable: "liquidity evidence unavailable",
    sol_usd_reference_unavailable: "fresh SOL price unavailable",
    liquidity_share_limit: "order too large for pool liquidity",
    zero_output: "zero modeled output",
    execution_model_rejected: "execution model rejected route",
  };
  for (const decision of decisions.slice(0, 25)) {
    const row = document.createElement("tr");
    const time = new Intl.DateTimeFormat(undefined, { dateStyle: "short", timeStyle: "short" }).format(new Date(decision.created_at));
    const failurePrefix = "simulation_failed:PaperExecutionError:";
    const action = typeof decision.action === "string" && decision.action.startsWith(failurePrefix)
      ? `Entry skipped · ${executionReasons[decision.action.slice(failurePrefix.length)] || "execution model rejected route"}`
      : decision.action;
    const reasons = Array.isArray(decision.reasons) ? decision.reasons : [];
    const gates = Array.isArray(decision.evidence?.market_gate_reasons) ? decision.evidence.market_gate_reasons : [];
    const primaryReason = decision.primary_reason || (decision.verdict === "reject" ? gates[0] : null) || reasons.at(-1) || "—";
    const values = [time, short(decision.mint, 7, 5), `${decision.score}/100`, decision.verdict, action, primaryReason];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index === 3) cell.className = decision.verdict === "eligible" ? "pnl-positive" : decision.verdict === "reject" ? "pnl-negative" : "";
      row.append(cell);
    });
    row.title = reasons.join(" · ");
    table.append(row);
  }
  $("#decisionsEmpty").hidden = decisions.length > 0;
}

function renderPaperPortfolio(portfolio, refreshed) {
  const campaign = portfolio.paper_campaign;
  text("#paperCampaignNote", campaign && campaign.mode === "forward_paper_only"
    ? `Forward paper run started ${formatTimestamp(campaign.started_at)}. Earlier journal retained as ${campaign.source_state_file}.`
    : "Original paper journal");
  text("#paperCash", formatSol(portfolio.cash_lamports));
  const unvaluedPositions = Number(portfolio.unvalued_position_count || 0);
  text("#paperOpenValue", unvaluedPositions ? "—" : formatSol(portfolio.open_value_lamports));
  text("#paperEquity", unvaluedPositions ? "—" : formatSol(portfolio.equity_lamports));
  text("#paperPnl", formatSignedSol(portfolio.total_pnl_lamports));
  const openPositions = portfolio.positions.filter((position) => position.status === "open");
  const cachedMarks = openPositions.filter((position) => position.quote_state === "recent_mark").length;
  const staleMarks = openPositions.filter((position) => position.quote_state === "stale").length;
  const quoteFailures = openPositions.filter((position) => position.quote_error).length;
  const belowFeeQuotes = openPositions.filter((position) => position.quote_error_reason === "below_exit_fees").length;
  const unverifiedMarks = cachedMarks + staleMarks;
  $("#paperPnl").className = unvaluedPositions || unverifiedMarks ? "pnl-unverified" : portfolio.total_pnl_lamports >= 0 ? "pnl-positive" : "pnl-negative";
  text("#paperOpenValueNote", unvaluedPositions ? `${unvaluedPositions} position${unvaluedPositions === 1 ? "" : "s"} have no exit mark` : !openPositions.length ? "No open positions" : staleMarks ? "Includes old marks" : cachedMarks ? "Includes saved marks" : "Current exit quotes");
  text("#paperEquityNote", unvaluedPositions
    ? `Known cash and marks: ${formatSol(portfolio.equity_lamports)}; total value unknown`
    : !openPositions.length ? "No open positions; all cash" : staleMarks
    ? `Estimate includes ${formatSol(portfolio.stale_mark_value_lamports)} in old marks · zero-mark scenario ${formatSol(portfolio.equity_if_stale_zero_lamports)}`
    : cachedMarks ? "Estimate from saved marks" : "Cash + current marks");
  const pnlNote = unvaluedPositions
    ? "Unavailable until every open position has an exit mark"
    : !openPositions.length ? "Realized paper P&L"
      : unverifiedMarks ? "Estimate · refresh quotes" : "Realized + current unrealized";
  text("#paperPnlNote", openRentReserved > 0
    ? `${pnlNote} · ${formatSol(openRentReserved)} token-account rent reserved`
    : pnlNote);
  $("#paperPnlNote").title = openRentReserved > 0
    ? "The paper model reserves token-account rent on entry and does not assume it is recovered on exit. Recovery would require closing the empty token account."
    : "";
  text("#paperQuoteState", belowFeeQuotes
    ? `${belowFeeQuotes} exit route${belowFeeQuotes === 1 ? "" : "s"} below modeled fees; new entries paused`
    : quoteFailures
    ? `${quoteFailures} exit quote refresh failure${quoteFailures === 1 ? "" : "s"} · exit rules require fresh quotes`
    : staleMarks ? `${staleMarks} old or missing saved mark${staleMarks === 1 ? "" : "s"} · refresh for valuation`
    : cachedMarks ? `${cachedMarks} saved mark${cachedMarks === 1 ? "" : "s"} · refresh for valuation`
      : openPositions.length ? (refreshed ? "Quotes refreshed now" : "Current exit quotes") : "No open positions");
  $("#paperQuoteState").classList.toggle("quote-warning", staleMarks > 0 || quoteFailures > 0);
  const execution = portfolio.execution_summary || {};
  const policy = portfolio.execution_policy || {};
  text("#paperExplicitCosts", formatSol(execution.explicit_cost_lamports || 0));
  text("#paperModeledSlippage", formatSol(execution.modeled_slippage_lamports || 0));
  text(
    "#paperSlippagePolicy",
    `${Number(policy.base_slippage_bps || 0) / 100}% base + ${Number(policy.liquidity_shock_bps || 0) / 100}% stress`,
  );
  text("#paperQuoteAgePolicy", `${Number(policy.max_quote_age_ms || 0).toLocaleString()} ms max`);
  text("#paperLiquidityCap", `${Number(policy.max_trade_liquidity_bps || 0) / 100}% max`);

  const forward = portfolio.forward_performance || {};
  const autonomous = forward.autonomous || {};
  const trades = Number(autonomous.trades || 0);
  text("#forwardTrades", trades);
  text("#forwardPnl", autonomous.net_pnl_lamports == null ? "—" : formatSignedSol(autonomous.net_pnl_lamports));
  text("#forwardWinRate", autonomous.win_rate_pct == null ? "—" : `${autonomous.win_rate_pct}%`);
  text("#forwardDrawdown", autonomous.max_closed_trade_drawdown_pct == null ? "—" : `${autonomous.max_closed_trade_drawdown_pct}%`);
  text("#forwardProfitFactor", autonomous.profit_factor == null ? "—" : autonomous.profit_factor);
  text("#forwardReturn", autonomous.net_trade_return_pct == null ? "—" : `${autonomous.net_trade_return_pct}%`);
  text("#forwardRentReserved", autonomous.ata_rent_reserved_lamports == null ? "—" : formatSol(autonomous.ata_rent_reserved_lamports));
  text("#forwardRentRecovery", autonomous.net_pnl_if_full_ata_rent_reclaimed_lamports == null
    ? "—"
    : `${formatSignedSol(autonomous.net_pnl_if_full_ata_rent_reclaimed_lamports)}; assumes every token account is closed and rent returned`);
  text("#forwardExpectancy", autonomous.expectancy_pct == null ? "—" : `${autonomous.expectancy_pct}%`);
  text("#forwardHolding", autonomous.average_holding_seconds == null ? "—" : `${(autonomous.average_holding_seconds / 60).toFixed(1)} min`);
  const marks = forward.quote_mark_history || {};
  text("#forwardMarkCoverage", `${Number(marks.costed_marks || 0)} costed marks · ${Number(marks.open_positions_with_bounded_mark_gaps || 0)} / ${Number(forward.open_positions || 0)} open paths covered · ${Number(marks.closed_positions_with_bounded_mark_gaps || 0)} / ${Number(forward.closed_positions_total || 0)} closed paths covered`);
  text("#forwardMarkDrawdown", autonomous.max_observed_mark_drawdown_pct == null
    ? "Unavailable until a covered trade closes"
    : `${autonomous.max_observed_mark_drawdown_pct}% lower bound · ${Number(autonomous.trades_with_bounded_mark_gaps || 0)} closed paths`);
  const lossReasons = autonomous.loss_reasons || {};
  text("#forwardLossReasons", Object.keys(lossReasons).length ? Object.entries(lossReasons).map(([reason, count]) => `${reason.replaceAll("_", " ")} (${count})`).join(", ") : "None recorded");
  const status = $("#forwardStatus");
  const unmonitored = Number(forward.exclusion_reasons?.unbounded_quote_mark_gap || 0);
  status.textContent = unmonitored ? "Monitoring gaps" : trades === 0 ? "Awaiting exits" : trades < Number(forward.minimum_interpretable_trades || 30) ? "Early sample" : "Descriptive sample";
  status.className = `status-badge ${trades && !unmonitored ? "safe" : ""}`;
  text("#forwardNote", `${trades} fully monitored autonomous exits; ${unmonitored} exits crossed an unmonitored interval. ${forward.call_inbox?.trades || 0} call-driven and ${forward.manual_closed_excluded || 0} manual trades are separate. ${forward.incomplete_closed_excluded || 0} closed trades were excluded in total, including unmonitored exits. ${forward.open_positions_without_signal_evidence || 0} open positions lack full entry evidence and will be excluded when closed. At least ${forward.minimum_interpretable_trades || 30} fully monitored exits are needed before interpreting performance.`);

  const table = $("#paperPositionsTable");
  table.replaceChildren();
  for (const position of portfolio.positions) {
    const row = document.createElement("tr");
    const market = position.market || {};
    row.append(tokenCell(market.symbol || short(position.mint, 7, 5), market.name || short(position.mint, 10, 8)));
    for (const value of [formatSol(position.entry_lamports), position.current_value_lamports == null ? "—" : formatSol(position.current_value_lamports)]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    const pnl = document.createElement("td");
    pnl.textContent = formatSignedSol(position.pnl_lamports);
    if (position.pnl_lamports != null) pnl.className = position.pnl_lamports >= 0 ? "pnl-positive" : "pnl-negative";
    row.append(pnl);
    const ladder = document.createElement("td");
    const completedStages = Number(position.tp_stage || 0);
    const totalStages = state.status?.paper_automation?.tp_levels_bps?.length || 0;
    const remainingPercent = Number(position.remaining_bps || 0) / 100;
    ladder.textContent = position.status === "closed"
      ? "Complete"
      : `TP ${completedStages}/${totalStages} · ${remainingPercent.toFixed(0)}% left`;
    ladder.className = "status-text";
    ladder.title = `${formatSol(position.realized_lamports || 0)} realized proceeds`;
    row.append(ladder);
    const quote = document.createElement("td");
    const mark = position.last_mark_execution || {};
    const quoteReasonLabels = {
      stale_route: "stale quote",
      slippage_limit: "slippage limit",
      price_impact_limit: "impact limit",
      modeled_slippage_limit: "slippage model",
      liquidity_share_limit: "liquidity limit",
      zero_output: "zero output",
    };
    const quoteLabel = position.quote_error_reason === "below_exit_fees"
      ? "below fees"
      : position.quote_error
      ? (quoteReasonLabels[position.quote_error_reason] || "quote failed")
      : position.quote_state === "recent_mark" ? "saved mark"
        : position.quote_state === "stale" ? (position.last_quoted_at ? "old mark" : "no mark")
          : position.quote_state;
    quote.append(statusBadge(quoteLabel, position.quote_state === "live" ? "safe" : ""));
    if (position.quote_state === "live") {
      const details = document.createElement("small");
      details.className = "execution-fill-detail";
      details.textContent = `${Number(mark.adverse_slippage_bps || 0)} bps / ${Number(mark.quote_age_ms || 0)} ms`;
      quote.append(details);
      quote.title = "Adverse fill haircut / quote age";
    } else if (position.quote_error_reason === "below_exit_fees") {
      quote.title = `A route exists, but gross proceeds of ${Number(position.quote_gross_out_lamports || 0).toLocaleString()} lamports do not cover modeled exit fees. The saved mark is old; entries stay paused.`;
    } else if (position.quote_error) {
      quote.title = `Exit quote refresh failed: ${quoteReasonLabels[position.quote_error_reason] || "route unavailable"}. Exit rules require a fresh quote.`;
    } else if (position.quote_state === "recent_mark") {
      quote.title = `Last mark: ${formatTimestamp(position.last_quoted_at)}. Refresh for a current exit quote.`;
    } else if (position.status === "open" && position.last_quoted_at) {
      quote.title = `Old mark: ${formatTimestamp(position.last_quoted_at)}. Refresh for a current exit quote.`;
    }
    row.append(quote);
    const positionStatus = document.createElement("td");
    positionStatus.textContent = position.status;
    positionStatus.className = "status-text";
    row.append(positionStatus);
    const actionCell = document.createElement("td");
    if (position.status === "open") {
      const action = document.createElement("button");
      action.type = "button";
      action.className = "table-action";
      action.textContent = "Close";
      action.dataset.closePaper = position.position_id;
      actionCell.append(action);
    } else {
      actionCell.textContent = "—";
    }
    row.append(actionCell);
    table.append(row);
  }
  $("#paperPositionsEmpty").hidden = portfolio.positions.length > 0;
}

async function loadPaperPortfolio(refresh = false) {
  const payload = await api(refresh ? "/api/paper/portfolio/refresh" : "/api/paper/portfolio", refresh ? { method: "POST" } : {});
  renderPaperPortfolio(payload, refresh);
}

function parsePercentList(selector) {
  const values = $(selector).value.split(",").map((value) => Number(value.trim()));
  if (!values.length || values.some((value) => !Number.isFinite(value) || value <= 0)) {
    throw new Error("Enter positive percentages separated by commas.");
  }
  return values.map((value) => Math.round(value * 100));
}

function labScenarioPayload() {
  const tpLevels = parsePercentList("#labTpTargets");
  const tpSales = parsePercentList("#labTpSales");
  if (tpLevels.length !== tpSales.length) throw new Error("Provide one sale portion for every TP target.");
  return {
    mint: $("#labMint").value,
    timeframe: $("#labTimeframe").value,
    tp_levels_bps: tpLevels,
    tp_sell_bps: tpSales,
    stop_loss_bps: Math.round(Number($("#labStopLoss").value) * 100),
  };
}

function renderEvaluation(result) {
  const complete = result.status === "complete";
  const noClosedEvidence = ["insufficient_training_trades", "no_closed_out_of_sample_trades"].includes(result.status);
  text("#evaluationBadge", complete ? "Evaluated" : result.status === "unverified_data" ? "Unverified" : noClosedEvidence ? "No trade evidence" : "Collecting");
  $("#evaluationBadge").className = `status-badge ${complete ? "safe" : ""}`;
  if (!complete) {
    $("#evaluationGates").replaceChildren();
    $("#evaluationRegimes").replaceChildren();
    text("#evaluationExpectancy", "—");
    $("#evaluationExpectancy").className = "";
    text("#evaluationProfitFactor", "—");
    text("#evaluationStress", "—");
    $("#evaluationStress").className = "";
    text("#evaluationPromotion", "Locked");
    text("#evaluationTrades", result.reason || `${result.available_candles || 0} / ${result.minimum_candles || 36} candles`);
    return;
  }
  const oos = result.out_of_sample || {};
  const stress = result.cost_stress_150pct || {};
  const hasTrades = Number(oos.trades || 0) > 0;
  const unclosed = Number(oos.censored_trades || 0);
  text("#evaluationBadge", unclosed ? "Partial history" : hasTrades ? "Evaluated" : "No trades");
  text("#evaluationExpectancy", hasTrades ? `${Number(oos.expectancy_pct || 0).toFixed(2)}%` : "—");
  text("#evaluationProfitFactor", oos.profit_factor == null ? "—" : Number(oos.profit_factor).toFixed(2));
  text("#evaluationStress", hasTrades ? `${Number(stress.return_pct || 0).toFixed(2)}%` : "—");
  text("#evaluationPromotion", result.promotion_ready ? "Eligible for review" : "Locked");
  text("#evaluationTrades", unclosed ? `${Number(oos.trades)} closed · ${unclosed} unclosed at fold boundary; review incomplete` : hasTrades ? `${Number(oos.trades)} closed trades across ${Number(oos.folds || 0)} folds` : "No eligible out-of-sample trades; performance is unavailable");
  $("#evaluationExpectancy").className = Number(oos.expectancy_pct || 0) > 0 ? "pnl-positive" : "pnl-negative";
  $("#evaluationStress").className = Number(stress.return_pct || 0) > 0 ? "pnl-positive" : "pnl-negative";

  const gates = $("#evaluationGates");
  gates.replaceChildren();
  const gateLabels = {
    positive_expectancy: "Positive out-of-sample expectancy",
    profit_factor_1_25: "Profit factor at least 1.25",
    positive_cost_stress: "Positive return at 150% costs",
    minimum_200_closed_positions: "At least 200 closed paper positions",
    no_censored_out_of_sample_positions: "No unclosed positions at evaluation boundaries",
    unbiased_multi_token_universe: "Historical failed and inactive tokens included",
    point_in_time_safety_audit: "Historical token safety verified at entry time",
  };
  for (const [key, label] of Object.entries(gateLabels)) {
    const line = document.createElement("div");
    const passed = Boolean(result.validation_gates?.[key]);
    line.className = `evaluation-gate ${passed ? "passed" : "failed"}`;
    line.append(statusBadge(passed ? "Pass" : "Pending", passed ? "safe" : ""));
    const copy = document.createElement("span");
    copy.textContent = label;
    line.append(copy);
    gates.append(line);
  }

  const regimes = $("#evaluationRegimes");
  regimes.replaceChildren();
  for (const regime of result.regimes || []) {
    const line = document.createElement("div");
    line.className = "evaluation-regime";
    const name = document.createElement("strong");
    name.textContent = String(regime.regime || "unknown").replaceAll("_", " ");
    const detail = document.createElement("span");
    detail.textContent = `${regime.trades} trades · ${Number(regime.expectancy_pct || 0).toFixed(2)}% expectancy`;
    line.append(name, detail);
    regimes.append(line);
  }
  if (!regimes.childElementCount) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No out-of-sample trades yet.";
    regimes.append(empty);
  }
}

function renderLabRuns(runs) {
  const table = $("#labRunsTable");
  table.replaceChildren();
  for (const run of runs) {
    const result = run.result || {};
    const metrics = result.out_of_sample || result;
    const measured = result.status === "complete" && Number(metrics.trades || 0) > 0;
    const row = document.createElement("tr");
    const values = [
      formatTimestamp(run.created_at), short(run.mint, 6, 4), run.timeframe,
      `${run.strategy_name} · ${result.status || "unknown"}${Number(metrics.censored_trades || 0) ? ` · ${metrics.censored_trades} unclosed` : ""}`, metrics.trades ?? 0,
      measured ? `${Number(metrics.win_rate_pct || 0).toFixed(1)}%` : "—",
      measured ? `${Number(metrics.return_pct || 0).toFixed(3)}%` : "—",
      measured ? `${Number(metrics.max_drawdown_pct || 0).toFixed(3)}%` : "—",
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index === 6 && measured) cell.className = Number(metrics.return_pct || 0) >= 0 ? "pnl-positive" : "pnl-negative";
      row.append(cell);
    });
    table.append(row);
  }
  $("#labRunsEmpty").hidden = runs.length > 0;
}

function renderLabChart(candles) {
  const host = $("#candleChart");
  host.replaceChildren();
  if (!candles.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No completed trade candles for this pool yet. Keep the collector running.";
    host.append(empty);
    return;
  }
  const shown = candles.slice(-60);
  const low = Math.min(...shown.map((candle) => candle.low));
  const high = Math.max(...shown.map((candle) => candle.high));
  const span = Math.max(high - low, Math.abs(high) * 0.001, Number.EPSILON);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 800 260");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${shown.length} completed pool trade candlesticks`);
  const step = 780 / shown.length;
  const y = (price) => 20 + (high - price) / span * 210;
  shown.forEach((candle, index) => {
    const x = 10 + index * step + step / 2;
    const up = candle.close >= candle.open;
    const color = up ? "#5bd69c" : "#ff6b72";
    const wick = document.createElementNS("http://www.w3.org/2000/svg", "line");
    wick.setAttribute("x1", x); wick.setAttribute("x2", x);
    wick.setAttribute("y1", y(candle.high)); wick.setAttribute("y2", y(candle.low));
    wick.setAttribute("stroke", color); wick.setAttribute("stroke-width", "1");
    const body = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    const bodyTop = Math.min(y(candle.open), y(candle.close));
    body.setAttribute("x", x - Math.max(1.5, step * 0.28));
    body.setAttribute("y", bodyTop);
    body.setAttribute("width", Math.max(3, step * 0.56));
    body.setAttribute("height", Math.max(2, Math.abs(y(candle.open) - y(candle.close))));
    body.setAttribute("fill", color);
    svg.append(wick, body);
  });
  host.append(svg);
}

function renderLabSignal(payload) {
  const signal = payload.signal;
  const trustedAge = payload.latest_trusted_close_age_seconds;
  const frameSeconds = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600 }[payload.timeframe] || 60;
  text("#labSignalScore", `${signal.score}/100`);
  text("#labTrend", signal.trend);
  text("#labMomentum", signal.momentum_pct == null ? "—" : `${Number(signal.momentum_pct).toFixed(2)}%`);
  text("#labVolume", signal.volume_ratio == null ? "—" : `${Number(signal.volume_ratio).toFixed(2)}x`);
  text("#labLiquidity", signal.liquidity_usd == null ? "—" : formatUsd(signal.liquidity_usd));
  const badge = $("#labDataBadge");
  badge.textContent = signal.status === "collecting" && trustedAge != null && trustedAge > frameSeconds * 2
    ? "Old data"
    : signal.status === "collecting" && payload.candles.length && trustedAge == null
      ? "Unverified"
      : signal.status === "collecting" ? "Collecting" : signal.status === "stale" ? "Stale" : signal.status === "gap" ? "Data gap" : signal.confirmed ? "Confirmed" : "No entry";
  badge.className = `status-badge ${signal.confirmed ? "safe" : signal.status === "ready" ? "danger" : ""}`;
  const reasons = $("#labSignalReasons");
  reasons.replaceChildren();
  for (const reason of signal.reasons) {
    const item = document.createElement("li");
    item.textContent = reason;
    reasons.append(item);
  }
  const freshness = trustedAge == null
    ? "No first-observed closed bar yet."
    : `Latest trusted bar closed ${formatDuration(trustedAge)} ago.`;
  text("#labVolumeNote", `${payload.volume_note} ${freshness}`);
  renderLabChart(payload.candles);
}

async function loadLabCandles() {
  const mint = $("#labMint").value;
  if (!mint) return;
  const frame = $("#labTimeframe").value;
  const payload = await api(`/api/strategy-lab/candles?mint=${encodeURIComponent(mint)}&timeframe=${encodeURIComponent(frame)}`);
  renderLabSignal(payload);
}

function renderLabSummary(payload) {
  const cohort = payload.research_cohort || {};
  const readiness = payload.entry_readiness || {};
  text("#labEntryReadiness", readiness.market_gate_eligible == null
    ? "Current entry data is unavailable."
    : `${readiness.market_gate_eligible} market qualified · ${readiness.entry_eligible || 0} full entry signals · ${readiness.candle_data_limited || 0} awaiting usable 1m/5m bars · ${readiness.signal_misaligned || 0} signal mismatch · ${readiness.pre_candle_gate_blocked || 0} awaiting fresh audit or pool data`);
  const collection = payload.collector?.last_result?.market_collection;
  const providerErrors = Number(collection?.provider_error_count || 0);
  const newCandles = Number(collection?.authentic_candles_refreshed || 0);
  const collectionNote = collection
    ? ` Last collector cycle: ${newCandles} ${newCandles === 1 ? "candle" : "candles"}${providerErrors ? `, ${providerErrors} provider ${providerErrors === 1 ? "error" : "errors"}` : ""}. ${Number(collection.research_active_5m_size || 0)} sampled pools had 5m DEX trades; ${Number(collection.research_active_5m_scheduled || 0)} were scheduled for candles. Activity is not approval.`
    : "";
  text("#labCohortNote", Number(cohort.size || 0) > 0
    ? `Research sample: ${cohort.size} of ${cohort.population_count || "?"} recent tokens, selected without score or audit filtering. ${cohort.focus_size || 0} receive intensive candle collection.${collectionNote}`
    : "Research sample is waiting for discovered tokens.");
  const capture = payload.research_capture || {};
  const focusCapture = capture.focus || {};
  const secondaryCapture = capture.secondary || {};
  text("#labCaptureNote", Number(cohort.size || 0) > 0
    ? `Timely 1m bars on the current pool: focus ${focusCapture.trusted_timely_bars || 0} / ${focusCapture.eligible_minute_slots || 0} elapsed minutes (${focusCapture.observed_closed_bars || 0} bars observed); remaining sample ${secondaryCapture.trusted_timely_bars || 0} / ${secondaryCapture.eligible_minute_slots || 0} (${secondaryCapture.observed_closed_bars || 0} observed). Missing bars may mean no trades or provider gaps.`
    : "Timely candle coverage is waiting for a research sample.");
  const followup = payload.latest_followup;
  const rawWindowMinutes = followup ? Math.round((Number(followup.expires_at) - Number(followup.selected_at)) / 60) : 0;
  const windowMinutes = Number.isFinite(rawWindowMinutes) && rawWindowMinutes > 0 ? rawWindowMinutes : 0;
  const followupLabels = {
    observed_timely: "Observed on time",
    observed_backfilled: "Observed later",
    pending_horizon: "Waiting for bars",
    entry_bar_missing: "First bar missing",
    future_bar_gap: "Bar gap",
    untrusted_capture: "Capture unverified",
    unverified_source: "Source unverified",
    invalid_candle_values: "Invalid candle",
    invalid_candle_order: "Bar order invalid",
    pool_snapshot_unavailable_at_selection: "Pool unverified",
    pool_snapshot_stale_at_selection: "Pool data old",
  };
  const pathLabel = (frame) => followup
    ? (followupLabels[followup.outcomes?.[frame]?.status] || "Unavailable")
    : "Waiting";
  text("#labFollowupWindow", followup && windowMinutes ? `${windowMinutes}m window` : "Waiting");
  text("#labFollowupSample", followup
    ? `Selected from ${followup.population_count} market-ready tokens`
    : "No selection recorded yet");
  text("#labFollowupFive", pathLabel("5m"));
  text("#labFollowupFifteen", pathLabel("15m"));
  const select = $("#labMint");
  const selected = select.value;
  select.replaceChildren();
  if (!payload.tracked.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No market history yet";
    select.append(option);
  } else {
    const dataRank = (token) => token.trusted_close_age_seconds_1m == null
      ? 2 : token.trusted_close_age_seconds_1m <= 120 ? 0 : 1;
    const tracked = [...payload.tracked].sort((a, b) => dataRank(a) - dataRank(b)
      || b.trusted_candle_count_1m - a.trusted_candle_count_1m
      || b.candle_count_1m - a.candle_count_1m);
    for (const token of tracked) {
      const option = document.createElement("option");
      option.value = token.mint;
      const age = token.trusted_close_age_seconds_1m;
      const freshness = age == null ? "unverified" : age <= 120 ? "recent bar" : `bar ${formatDuration(age)} old`;
      option.textContent = `${token.symbol || short(token.mint, 7, 5)} · ${token.trusted_candle_count_1m} trusted 1m · ${freshness}`;
      select.append(option);
    }
    if (payload.tracked.some((token) => token.mint === selected)) select.value = selected;
  }
  const risk = payload.risk;
  text("#labDailyPnl", formatSignedSol(risk.daily_realized_pnl_lamports));
  text("#labDailyLimit", `Stops entries at -${formatSol(risk.daily_loss_limit_lamports)}`);
  text("#labLossStreak", String(risk.consecutive_losses));
  text("#labLossLimit", `Limit ${risk.max_consecutive_losses}`);
  text("#labLiquidityFloor", formatUsd(risk.liquidity_breaker_usd));
  text("#labPortfolioBreaker", `-${(risk.max_drawdown_bps / 100).toFixed(0)}%`);
  renderLabRuns(payload.runs);
}

async function loadStrategyLab() {
  const payload = await api("/api/strategy-lab/summary");
  renderLabSummary(payload);
  await loadLabCandles();
}

const viewTitles = {
  overview: ["Paper research", "Paper desk"],
  discovery: ["On-Chain Radar", "Candidate discovery"],
  paper: ["Simulation", "Paper portfolio"],
  lab: ["Research", "Strategy Lab"],
  audit: ["Contract Intelligence", "Token audit"],
  trade: ["Execution", "Trade console"],
  positions: ["Position Journal", "Managed positions"],
  activity: ["Local Audit Log", "Order activity"],
  risk: ["Server Policy", "Risk limits"],
};

async function route() {
  const name = location.hash.slice(1) || "overview";
  const view = viewTitles[name] ? name : "overview";
  if (matchMedia("(max-width: 760px)").matches) $("#navMore").open = false;
  else if (["audit", "trade", "positions", "activity", "risk"].includes(view)) $("#navMore").open = true;
  $$("[data-view]").forEach((node) => node.classList.toggle("active", node.dataset.view === view));
  $$("[data-view-link]").forEach((node) => node.classList.toggle("active", node.dataset.viewLink === view));
  text("#pageEyebrow", viewTitles[view][0]);
  text("#pageTitle", viewTitles[view][1]);
  if (view === "orders" || view === "activity") await loadOrders(false);
  if (view === "overview") await loadOverview();
  if (view === "positions") await loadPositions();
  if (view === "discovery") await Promise.all([loadCandidates(), loadAutomation(), loadCalls(), loadNarrative()]);
  if (view === "paper") await loadPaperPortfolio(false);
  if (view === "lab") await loadStrategyLab();
  $("#workspace").focus({ preventScroll: true });
}

function renderAudit(audit) {
  $("#auditResult").hidden = false;
  const badge = $("#auditBadge");
  badge.textContent = audit.passed ? "Audit passed" : "Audit blocked";
  badge.className = `status-badge ${audit.passed ? "safe" : "danger"}`;
  text("#auditMintResult", audit.mint);
  text("#auditProgram", short(audit.token_program, 8, 6));
  text("#auditDecimals", audit.decimals);
  text("#auditConcentration", `${(audit.top10_concentration_bps / 100).toFixed(2)}%`);
  const effective = audit.effective_top10_concentration_bps ?? audit.top10_concentration_bps;
  text("#auditEffectiveConcentration", `${(effective / 100).toFixed(2)}%`);
  text("#auditSupply", Number(audit.supply_atomic).toLocaleString());
  renderForensics(audit.forensics);
  const list = $("#auditFindings");
  list.replaceChildren();
  if (!audit.findings.length) {
    const item = document.createElement("li");
    item.textContent = "No blocking findings were returned by the configured checks.";
    list.append(item);
  }
  audit.findings.forEach((finding) => {
    const item = document.createElement("li");
    item.textContent = `${finding.blocking ? "Blocking" : "Notice"}: ${finding.message}`;
    item.className = finding.blocking ? "finding-blocking" : "finding-notice";
    list.append(item);
  });
}

function renderForensics(forensics) {
  const panel = $("#auditForensics");
  panel.hidden = !forensics;
  if (!forensics) return;
  const badge = $("#forensicsBadge");
  badge.textContent = forensics.status;
  badge.className = `status-badge ${forensics.status === "complete" ? "safe" : forensics.status === "partial" ? "warning" : "danger"}`;
  text("#forensicsAccounts", `${forensics.analyzed_accounts}/20`);
  text("#forensicsControllers", forensics.distinct_controllers);
  text("#forensicsVaultShare", `${(forensics.recognized_vault_bps / 100).toFixed(2)}%`);
  text("#forensicsLargestController", `${(forensics.largest_controller_bps / 100).toFixed(2)}%`);
  text("#forensicsBurnShare", `${(forensics.burn_bps / 100).toFixed(2)}%`);

  if (forensics.origin) {
    text("#forensicsOrigin", short(forensics.origin.creation_signer, 8, 6));
    text(
      "#forensicsOriginMeta",
      `${forensics.origin.confidence} confidence · observed ${(forensics.origin.current_observed_bps / 100).toFixed(2)}% · ${forensics.origin.history_complete ? "history covered" : "bounded history"}`,
    );
  } else {
    text("#forensicsOrigin", "Not established");
    text("#forensicsOriginMeta", "Bounded history; no guesses");
  }

  const body = $("#forensicsHolders");
  body.replaceChildren();
  const holders = Array.isArray(forensics.holders) ? forensics.holders.slice(0, 10) : [];
  $("#forensicsEmpty").hidden = holders.length > 0;
  holders.forEach((holder) => {
    const row = document.createElement("tr");
    const classification = document.createElement("td");
    const chip = document.createElement("span");
    chip.className = `forensic-chip ${holder.classification}`;
    chip.textContent = holder.classification.replaceAll("_", " ");
    if (holder.evidence) chip.title = holder.evidence;
    classification.append(chip);
    const account = document.createElement("td");
    account.className = "mono";
    account.textContent = short(holder.token_account, 6, 5);
    const controller = document.createElement("td");
    controller.className = "mono";
    controller.textContent = holder.controller ? short(holder.controller, 6, 5) : "unresolved";
    const share = document.createElement("td");
    share.className = "mono forensic-share";
    share.textContent = `${(holder.supply_bps / 100).toFixed(2)}%`;
    row.append(classification, account, controller, share);
    body.append(row);
  });
  const limits = Array.isArray(forensics.limitations) ? forensics.limitations : [];
  text(
    "#forensicsLimit",
    limits.length ? `Coverage note: ${limits.join(" · ")}` : "All bounded forensic reads completed.",
  );
}

function renderPreview(payload) {
  state.previewId = payload.preview_id;
  const report = payload.report;
  $("#previewEmpty").hidden = true;
  $("#previewResult").hidden = false;
  const inputIsSol = report.quote.input_mint === "So11111111111111111111111111111111111111112";
  const outputIsSol = report.quote.output_mint === "So11111111111111111111111111111111111111112";
  text("#previewInput", inputIsSol ? formatSol(report.quote.in_amount) : `${report.quote.in_amount.toLocaleString()} atomic`);
  text("#previewOutput", outputIsSol ? formatSol(report.quote.out_amount) : `${report.quote.out_amount.toLocaleString()} atomic`);
  text("#previewImpact", report.quote.price_impact_bps == null ? "Unavailable" : `${(report.quote.price_impact_bps / 100).toFixed(2)}%`);
  text("#previewRouter", report.quote.router || "Metis");
  text("#previewUnits", report.result.simulated_units.toLocaleString());
  const execute = $("#executeButton");
  execute.disabled = !state.status.live_enabled;
  execute.textContent = state.status.live_enabled ? "Review Live Execution" : "Live Trading Disabled";
}

$("#pairForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  text("#pairError", "");
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  button.textContent = "Pairing…";
  try {
    const payload = await api("/api/auth/pair", { method: "POST", body: JSON.stringify({ code: $("#pairCode").value, remember_browser: $("#rememberBrowser").checked }) });
    state.csrf = payload.csrf_token;
    $("#pairCode").value = "";
    showApp();
    await loadStatus();
    await loadOrders(true);
    route();
  } catch (error) {
    text("#pairError", error.message);
    $("#pairCode").focus();
  } finally {
    button.disabled = false;
    button.textContent = "Open Console";
  }
});

$("#auditForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  button.textContent = "Auditing…";
  try {
    const result = await api("/api/audit", { method: "POST", body: JSON.stringify({ mint: $("#auditMint").value.trim() }) });
    renderAudit(result);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "Run Audit"; }
});

$$('input[name="side"]').forEach((input) => input.addEventListener("change", () => {
  const buy = $('input[name="side"]:checked').value === "buy";
  $("#buyAmountField").hidden = !buy;
  $("#sellAmountField").hidden = buy;
}));

$("#tradeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const side = $('input[name="side"]:checked').value;
  const body = {
    side,
    mint: $("#tradeMint").value.trim(),
    slippage_bps: Number($("#slippageBps").value),
    amount_sol: side === "buy" ? $("#amountSol").value : null,
    sell_percent: side === "sell" ? Number($("#sellPercent").value) : null,
  };
  button.disabled = true;
  button.textContent = "Pricing Exit…";
  state.previewId = null;
  try {
    const payload = await api("/api/trades/preview", { method: "POST", body: JSON.stringify(body) });
    renderPreview(payload);
    toast("Simulation passed. Preview expires in 90 seconds.");
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "Audit & Simulate"; }
});

$("#executeButton").addEventListener("click", () => {
  if (!state.previewId || !state.status.risk_phrase) return;
  text("#riskPhraseDisplay", state.status.risk_phrase);
  $("#riskPhrase").value = "";
  $("#executeDialog").showModal();
  $("#riskPhrase").focus();
});

$("#cancelExecute").addEventListener("click", () => $("#executeDialog").close());
$("#executeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector('button[type="submit"]');
  submit.disabled = true;
  submit.textContent = "Submitting…";
  try {
    const result = await api("/api/trades/execute", { method: "POST", body: JSON.stringify({ preview_id: state.previewId, acknowledgement: $("#riskPhrase").value }) });
    state.previewId = null;
    $("#executeDialog").close();
    toast(result.result.signature ? `Trade confirmed: ${short(result.result.signature, 8, 7)}` : "Trade completed");
    await loadStatus();
    await loadOrders(true);
  } catch (error) { toast(error.message); }
  finally { submit.disabled = false; submit.textContent = "Sign & Submit"; }
});

$("#copyWallet").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#copyWallet").dataset.wallet); toast("Wallet address copied."); }
  catch { toast("Copy failed. Select the wallet from the status page instead."); }
});

$("#scanCandidates").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Scanning & Auditing…";
  text("#scanSummary", "Reading bounded program activity…");
  try {
    const payload = await api("/api/discovery/scan", { method: "POST" });
    await loadCandidates();
    const pumpStatus = payload.providers?.pumpfun?.api === "connected" ? "Pump API connected" : "Pump on-chain";
    const dexStatus = payload.providers?.dexscreener?.profiles === "connected" ? "DEX profiles connected" : "DEX market only";
    text("#scanSummary", `${payload.scanned} candidates · ${payload.safe} passed · ${payload.blocked} blocked · ${pumpStatus} · ${dexStatus}${payload.warning ? ` · ${payload.warning}` : ""}`);
    toast("Discovery scan completed.");
  } catch (error) {
    text("#scanSummary", error.message);
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Scan now";
  }
});

$("#jumpToCall").addEventListener("click", () => {
  $("#callInboxTitle").scrollIntoView({ behavior: "smooth", block: "start" });
  $("#callMints").focus({ preventScroll: true });
});

$("#showShortlist").addEventListener("click", () => {
  state.candidateFilter = "shortlist";
  if (state.candidatePayload) renderCandidates(state.candidatePayload);
});

$("#showAllCandidates").addEventListener("click", () => {
  state.candidateFilter = "all";
  if (state.candidatePayload) renderCandidates(state.candidatePayload);
});

$("#radarToggle").addEventListener("click", () => {
  state.radarExpanded = !state.radarExpanded;
  renderHotRadar(state.candidatePayload?.candidates || []);
});

$("#marketIntel").addEventListener("toggle", (event) => {
  text(".market-intel-open", event.currentTarget.open ? "Close feeds" : "Explore feeds");
});

$("#runDecisionCycle").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Scoring & Simulating…";
  try {
    const payload = await api("/api/strategy/cycle", { method: "POST" });
    await Promise.all([loadCandidates(), loadPaperPortfolio(false)]);
    const result = payload.opened ? "Paper position opened from the highest eligible candidate." : "No eligible candidate produced a valid simulated entry.";
    text("#scanSummary", `${payload.evaluated} candidates evaluated · ${result}`);
    toast(result);
  } catch (error) {
    text("#scanSummary", error.message);
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Run Paper Decision";
  }
});

$("#refreshNarrative").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Collecting…";
  try {
    const payload = await api("/api/narrative/refresh", { method: "POST" });
    renderNarrative(payload);
    const providers = Object.entries(payload.refresh?.providers || {}).map(([name, value]) => `${name}: ${value.status}`).join(" · ");
    toast(`Narrative evidence refreshed${providers ? ` · ${providers}` : ""}.`);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Refresh Evidence";
  }
});

async function automationCommand(path, button, pendingLabel) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = pendingLabel;
  try {
    const payload = await api(path, { method: "POST" });
    renderAutomation(payload);
    await Promise.all([loadCandidates(), loadPaperPortfolio(false), loadCalls()]);
    toast(path.endsWith("/start") ? "Autonomous paper worker started." : path.endsWith("/stop") ? "Autonomous paper worker stopped." : "Autonomous paper cycle completed.");
  } catch (error) {
    toast(error.message);
    await loadAutomation().catch(() => {});
  } finally {
    button.textContent = original;
    await loadAutomation().catch(() => {});
  }
}

$("#startAutomation").addEventListener("click", (event) => automationCommand("/api/automation/start", event.currentTarget, "Starting…"));
$("#stopAutomation").addEventListener("click", (event) => automationCommand("/api/automation/stop", event.currentTarget, "Stopping…"));
$("#runAutomationOnce").addEventListener("click", (event) => automationCommand("/api/automation/run-once", event.currentTarget, "Running Cycle…"));

$("#callInboxForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  const mints = [...new Set($("#callMints").value.split(/[\s,]+/).map((value) => value.trim()).filter(Boolean))];
  if (mints.length > 5) {
    toast("Paste no more than 5 token addresses at once.");
    return;
  }
  button.disabled = true;
  button.textContent = "Auditing Calls…";
  try {
    const payload = await api("/api/calls", {
      method: "POST",
      body: JSON.stringify({ mints, source_label: $("#callSource").value.trim() || null }),
    });
    renderCalls(payload.calls);
    $("#callMints").value = "";
    await Promise.all([loadCandidates(), loadPaperPortfolio(false), loadAutomation()]);
    const opened = payload.results.filter((item) => item.status === "opened").length;
    const rejected = payload.results.filter((item) => item.status === "rejected").length;
    toast(`${payload.results.length} call${payload.results.length === 1 ? "" : "s"} processed · ${opened} opened · ${rejected} rejected.`);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Audit & Paper-Hop";
  }
});

$("#candidatesTable").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-paper-mint]");
  if (!button) return;
  $("#paperMint").value = button.dataset.paperMint;
  location.hash = "paper";
});

$("#paperOpenForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  button.textContent = "Auditing & Simulating…";
  try {
    await api("/api/paper/open", {
      method: "POST",
      body: JSON.stringify({
        mint: $("#paperMint").value.trim(),
        amount_sol: $("#paperAmount").value,
        slippage_bps: Number($("#paperSlippage").value),
      }),
    });
    await loadPaperPortfolio(true);
    toast("Paper position opened. No wallet funds moved.");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Audit, Simulate & Open";
  }
});

$("#paperPositionsTable").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-close-paper]");
  if (!button) return;
  state.paperCloseId = button.dataset.closePaper;
  $("#paperCloseDialog").showModal();
});

$("#cancelPaperClose").addEventListener("click", () => {
  state.paperCloseId = null;
  $("#paperCloseDialog").close();
});

$("#paperCloseForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.paperCloseId) return;
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  button.textContent = "Simulating…";
  try {
    await api("/api/paper/close", {
      method: "POST",
      body: JSON.stringify({ position_id: state.paperCloseId, slippage_bps: 100 }),
    });
    state.paperCloseId = null;
    $("#paperCloseDialog").close();
    await loadPaperPortfolio(true);
    toast("Paper position closed at the current exit quote.");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Quote & Close";
  }
});

$("#labMint").addEventListener("change", () => loadLabCandles().catch((error) => toast(error.message)));
$("#labTimeframe").addEventListener("change", () => loadLabCandles().catch((error) => toast(error.message)));

$("#collectLabData").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Collecting…";
  try {
    const result = await api("/api/strategy-lab/collect", { method: "POST" });
    await loadStrategyLab();
    toast(`Updated ${result.collected} markets; stored ${result.authentic_candles_refreshed || 0} new real candles.${result.ohlcv_errors?.length ? ` ${result.ohlcv_errors.length} provider errors.` : ""}`);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Collect Real Candles";
  }
});

$("#labForm").addEventListener("invalid", (event) => {
  if ($("#labPolicyDetails").contains(event.target)) $("#labPolicyDetails").open = true;
}, true);

$("#labForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  button.textContent = "Replaying…";
  try {
    const payload = await api("/api/strategy-lab/backtest", {
      method: "POST",
      body: JSON.stringify(labScenarioPayload()),
    });
    const result = payload.result;
    const measured = result.status === "complete" && Number(result.trades || 0) > 0;
    const unclosed = Number(result.censored_trades || 0);
    const replayModel = result.entry_model === "multi_timeframe_candle_gate" ? "1m + 5m replay" : "Single-frame study";
    text("#labReplayStatus", unclosed ? "Partial history" : measured ? replayModel : result.status === "complete" ? "No eligible trades" : result.status === "unverified_data" ? "Unverified data" : "More data needed");
    text("#labReplayCoverage", `${result.available_candles || 0} real candles${unclosed ? ` · ${unclosed} unclosed position` : ""}`);
    text("#labTrades", String(result.trades));
    text("#labWinRate", measured ? `${Number(result.win_rate_pct).toFixed(1)}%` : "—");
    text("#labReturn", measured ? `${Number(result.return_pct).toFixed(3)}%` : "—");
    $("#labReturn").className = measured ? (Number(result.return_pct) >= 0 ? "pnl-positive" : "pnl-negative") : "";
    text("#labDrawdown", measured ? `Max DD ${Number(result.max_drawdown_pct).toFixed(3)}%` : "Max DD —");
    await loadStrategyLab();
    toast(unclosed ? "Replay reached the end of available evidence with an unclosed position; its return is excluded." : measured ? "Historical replay completed." : result.status === "complete" ? "Replay ran, but no eligible trades were available to measure." : result.reason || `Need ${result.minimum_candles} real candles; ${result.available_candles} available.`);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Run Historical Replay";
  }
});

$("#runEvaluation").addEventListener("click", async (event) => {
  if (!$("#labForm").reportValidity()) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Evaluating…";
  try {
    const payload = await api("/api/strategy-lab/evaluate", {
      method: "POST",
      body: JSON.stringify(labScenarioPayload()),
    });
    renderEvaluation(payload.result);
    await loadStrategyLab();
    toast(payload.result.status === "complete" ? "Walk-forward evaluation completed." : payload.result.reason || `Need ${payload.result.minimum_candles} real candles; ${payload.result.available_candles} available.`);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Run Walk-Forward";
  }
});

$("#refreshButton").addEventListener("click", async () => { try { await loadStatus(); await loadOverview(); toast("Paper desk refreshed."); } catch (error) { toast(error.message); } });
$("#refreshOrders").addEventListener("click", async () => { try { await loadOrders(false); toast("Activity refreshed."); } catch (error) { toast(error.message); } });
$("#refreshPositions").addEventListener("click", async () => { try { await loadPositions(); toast("Positions refreshed."); } catch (error) { toast(error.message); } });
$("#refreshPaper").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Refreshing…";
  try { await loadPaperPortfolio(true); toast("Paper quotes refreshed."); }
  catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "Refresh Quotes"; }
});
for (const button of $$("#logoutButton, #logoutButtonMobile")) {
  button.addEventListener("click", async () => {
    try { await api("/api/auth/logout", { method: "POST" }); showPairScreen(); }
    catch (error) { toast(error.message); }
  });
}
$("#logoutAllButton").addEventListener("click", async () => {
  if (!window.confirm("Sign out every paired browser, including this one?")) return;
  try { await api("/api/auth/logout-all", { method: "POST" }); showPairScreen(); }
  catch (error) { toast(error.message); }
});
window.addEventListener("hashchange", () => { route().catch((error) => toast(error.message)); });

restoreSession().catch((error) => { showPairScreen(); text("#pairError", error.message); });
