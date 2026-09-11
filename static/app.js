const $ = (id) => document.getElementById(id);

// ---- Auth ----
function showAuthGate() {
  $("auth-gate").classList.remove("hidden");
  $("app-shell").classList.add("hidden");
}

function showApp(user) {
  $("auth-gate").classList.add("hidden");
  $("app-shell").classList.remove("hidden");
  $("sidebar-user-email").textContent = user.email;
  refreshTicker();
  refreshSidebarConn();
  loadDashboard();
}

async function checkAuth() {
  try {
    const res = await fetch("/api/me");
    if (res.ok) {
      showApp(await res.json());
    } else {
      showAuthGate();
    }
  } catch (e) {
    showAuthGate();
  }
}

$("show-signup").onclick = (e) => {
  e.preventDefault();
  $("auth-login-form").classList.add("hidden");
  $("auth-signup-form").classList.remove("hidden");
};
$("show-login").onclick = (e) => {
  e.preventDefault();
  $("auth-signup-form").classList.add("hidden");
  $("auth-login-form").classList.remove("hidden");
};

$("btn-login").onclick = async () => {
  const errEl = $("login-form-error");
  errEl.textContent = "";
  try {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: $("login-email").value.trim(),
        password: $("login-password-field").value,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Login failed");
    showApp(data.user);
  } catch (e) {
    errEl.textContent = e.message;
  }
};

$("btn-signup").onclick = async () => {
  const errEl = $("signup-form-error");
  errEl.textContent = "";
  try {
    const res = await fetch("/api/auth/signup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: $("signup-email").value.trim(),
        password: $("signup-password-field").value,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Sign up failed");
    showApp(data.user);
  } catch (e) {
    errEl.textContent = e.message;
  }
};

$("btn-logout").onclick = async (e) => {
  e.preventDefault();
  await fetch("/api/auth/logout", { method: "POST" });
  showAuthGate();
};

// ---- Tabs ----
const tabs = {
  dashboard: { btn: $("tab-dashboard"), view: $("view-dashboard"), title: "Dashboard" },
  scanner: { btn: $("tab-scanner"), view: $("view-scanner"), title: "F&O Directional Scanner" },
  evaluate: { btn: $("tab-evaluate"), view: $("view-evaluate"), title: "Evaluate Signal" },
  history: { btn: $("tab-history"), view: $("view-history"), title: "Signal History" },
  stats: { btn: $("tab-stats"), view: $("view-stats"), title: "Channel Stats" },
  telegram: { btn: $("tab-telegram"), view: $("view-telegram"), title: "Telegram Config" },
  positions: { btn: $("tab-positions"), view: $("view-positions"), title: "Positions" },
  orderbook: { btn: $("tab-orderbook"), view: $("view-orderbook"), title: "Order Book" },
  broker: { btn: $("tab-broker"), view: $("view-broker"), title: "Broker Setup" },
};
function showTab(name) {
  Object.entries(tabs).forEach(([k, t]) => {
    t.btn.classList.toggle("active", k === name);
    t.view.classList.toggle("hidden", k !== name);
  });
  $("page-title").textContent = tabs[name].title;
  if (name === "history") loadHistory();
  if (name === "stats") loadStats();
  if (name === "telegram") loadTelegramTab();
  if (name === "positions") loadPositions();
  if (name === "orderbook") loadOrderBook();
  if (name === "broker") loadBrokerTab();
  if (name === "scanner") loadScannerTab();
  if (name === "dashboard") loadDashboard();
}
Object.entries(tabs).forEach(([name, t]) => (t.btn.onclick = () => showTab(name)));

// ---- Market ticker ----
async function refreshTicker() {
  const bar = $("ticker-bar");
  try {
    const res = await fetch("/api/market/ticker");
    const quotes = await res.json();
    bar.innerHTML = "";
    quotes.forEach((q) => {
      const item = document.createElement("div");
      item.className = "ticker-item";
      if (q.available) {
        const dir = q.change >= 0 ? "up" : "down";
        const arrow = q.change >= 0 ? "▲" : "▼";
        item.innerHTML = `
          <span class="ticker-label">${q.label}</span>
          <span class="ticker-value">${q.last.toLocaleString("en-IN")}</span>
          <span class="ticker-change ${dir}">${arrow} ${Math.abs(q.change).toFixed(2)} (${q.pct_change.toFixed(2)}%)</span>
        `;
      } else {
        item.innerHTML = `<span class="ticker-label">${q.label}</span><span style="color:var(--muted)">n/a</span>`;
      }
      bar.appendChild(item);
    });
    const clock = document.createElement("div");
    clock.className = "ticker-clock";
    clock.id = "ticker-clock";
    bar.appendChild(clock);
    updateClock();
  } catch (e) {
    bar.innerHTML = `<span style="color:var(--muted)">Market data unavailable</span>`;
  }
}

function updateClock() {
  const clock = $("ticker-clock");
  if (!clock) return;
  const now = new Date();
  const timeStr = now.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false });
  clock.textContent = `${timeStr} IST`;
}
setInterval(updateClock, 1000);
setInterval(refreshTicker, 60000);

// ---- Sidebar connection status ----
async function refreshSidebarConn() {
  const dot = $("sidebar-conn-dot");
  const text = $("sidebar-conn-text");
  try {
    const res = await fetch("/api/telegram/status");
    const s = await res.json();
    if (s.listening) {
      dot.className = "conn-dot on";
      text.textContent = "Connected";
    } else if (s.configured) {
      dot.className = "conn-dot off";
      text.textContent = "Not logged in";
    } else {
      dot.className = "conn-dot off";
      text.textContent = "Not configured";
    }
  } catch (e) {
    dot.className = "conn-dot off";
    text.textContent = "Offline";
  }
}
setInterval(refreshSidebarConn, 30000);

function handleKiteRedirect() {
  const params = new URLSearchParams(window.location.search);
  if (params.has("kite_connected") || params.has("kite_error")) {
    const message = params.has("kite_connected")
      ? "Kite Connect: logged in for today."
      : "Kite Connect login failed: " + params.get("kite_error");
    history.replaceState({}, "", window.location.pathname);
    setTimeout(() => {
      showTab("broker");
      const warnEl = $("kite-warning");
      if (warnEl) warnEl.innerHTML = `<div style="color:${params.has("kite_error") ? "var(--red)" : "var(--green)"}">${message}</div>`;
    }, 300);
  }
}

checkAuth().then(handleKiteRedirect);

// ---- Parse ----
$("btn-parse").onclick = async () => {
  const raw_text = $("raw-text").value.trim();
  if (!raw_text) return;
  const res = await fetch("/api/parse", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ raw_text }),
  });
  const data = await res.json();

  $("f-symbol").value = data.symbol || "";
  $("f-resolved").value = data.resolved_symbol || data.symbol || "";
  $("f-instrument").value = data.instrument || "EQ";
  $("f-strike").value = data.strike ?? "";
  $("f-type").value = data.signal_type || "positional";
  $("f-action").value = data.action || "buy";
  $("f-entry-low").value = data.entry_low ?? "";
  $("f-entry-high").value = data.entry_high ?? "";
  $("f-sl").value = data.sl ?? "";
  $("f-targets").value = (data.targets || []).join(", ");

  const warnEl = $("parse-warnings");
  warnEl.innerHTML = "";
  (data.parse_warnings || []).forEach((w) => {
    const d = document.createElement("div");
    d.textContent = "⚠ " + w;
    warnEl.appendChild(d);
  });
};

// ---- Evaluate ----
$("btn-evaluate").onclick = async () => {
  const targets = $("f-targets").value
    .split(",")
    .map((t) => parseFloat(t.trim()))
    .filter((t) => !isNaN(t));

  const payload = {
    raw_text: $("raw-text").value.trim() || null,
    channel: $("channel-name").value.trim() || "unknown",
    signal_type: $("f-type").value,
    action: $("f-action").value,
    symbol: $("f-symbol").value.trim(),
    resolved_symbol: $("f-resolved").value.trim() || $("f-symbol").value.trim(),
    instrument: $("f-instrument").value,
    strike: $("f-strike").value ? parseFloat($("f-strike").value) : null,
    entry_low: $("f-entry-low").value ? parseFloat($("f-entry-low").value) : null,
    entry_high: $("f-entry-high").value ? parseFloat($("f-entry-high").value) : null,
    sl: $("f-sl").value ? parseFloat($("f-sl").value) : null,
    targets,
  };

  if (!payload.symbol) {
    $("eval-status").textContent = "Symbol is required.";
    return;
  }

  $("eval-status").textContent = "Fetching chart, options, and news…";
  $("btn-evaluate").disabled = true;
  try {
    const res = await fetch("/api/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error("Evaluation request failed (" + res.status + ")");
    const data = await res.json();
    renderTradeSetup(payload, "trade-setup");
    renderResult(data);
    $("eval-status").textContent = "Saved as signal #" + data.signal_id;
  } catch (e) {
    $("eval-status").textContent = "Error: " + e.message;
  } finally {
    $("btn-evaluate").disabled = false;
  }
};

function scoreColor(score) {
  if (score >= 70) return "var(--green)";
  if (score >= 50) return "var(--amber)";
  return "var(--red)";
}

function renderResult(data) {
  renderEvaluation(data, "");
  $("result-panel").classList.remove("hidden");
}

function renderEvaluation(data, prefix) {
  const id = (name) => prefix + name;

  const circle = $(id("score-circle"));
  circle.textContent = data.score;
  circle.style.borderColor = scoreColor(data.score);
  circle.style.color = scoreColor(data.score);

  $(id("verdict-text")).textContent = data.verdict;
  let directionText = "Direction read as: " + data.direction;
  if (data.auto_trade) {
    if (data.auto_trade.placed) {
      directionText += ` · 📈 Auto-trade: paper order #${data.auto_trade.order_id} placed at ${data.auto_trade.entry_price}`;
    } else {
      directionText += ` · Auto-trade skipped: ${data.auto_trade.reason}`;
    }
  }
  $(id("direction-text")).textContent = directionText;

  const flagsEl = $(id("flags-container"));
  flagsEl.innerHTML = "";
  if (!data.red_flags || data.red_flags.length === 0) {
    const d = document.createElement("div");
    d.style.color = "var(--muted)";
    d.style.fontSize = "12px";
    d.textContent = "No red flags raised.";
    flagsEl.appendChild(d);
  } else {
    data.red_flags.forEach((f) => {
      const d = document.createElement("div");
      d.className = "flag";
      d.textContent = "🚩 " + f;
      flagsEl.appendChild(d);
    });
  }

  const tbody = document.querySelector(`#${id("breakdown-table")} tbody`);
  tbody.innerHTML = "";
  (data.breakdown || []).forEach(([label, pts, max, note]) => {
    const tr = document.createElement("tr");
    const pct = max ? Math.round((100 * pts) / max) : 0;
    tr.innerHTML = `
      <td>${label}</td>
      <td>${pts}/${max}</td>
      <td><div class="bar-bg"><div class="bar-fill" style="width:${pct}%"></div></div></td>
      <td style="color:var(--muted)">${note}</td>
    `;
    tbody.appendChild(tr);
  });

  const t = data.technicals || {};
  $(id("tech-kv")).innerHTML = t.available
    ? `
    <div><span>Data source</span>${t.source === "kite" ? '<span class="badge positive">Kite</span>' : '<span class="badge neutral">yfinance</span>'}</div>
    <div><span>Last close</span>${t.last_close}</div>
    <div><span>EMA20 / EMA50</span>${t.ema20} / ${t.ema50}</div>
    <div><span>EMA200</span>${t.ema200 ?? "n/a"}</div>
    <div><span>RSI(14)</span>${t.rsi14}</div>
    <div><span>ATR(14)</span>${t.atr14}</div>
    <div><span>Vol vs 20d avg</span>${t.vol_ratio ? t.vol_ratio + "x" : "n/a"}</div>
    <div><span>20d high/low</span>${t.prior_20d_high} / ${t.prior_20d_low}</div>
    <div><span>Breakout up?</span>${t.breakout_up}</div>
    <div><span>Rel. strength (10d)</span>${t.rel_strength_10d != null ? (t.rel_strength_10d * 100).toFixed(2) + "%" : "n/a"}</div>
  `
    : `<div style="color:var(--muted)">${t.reason || "Not available."}</div>`;

  const o = data.options || {};
  $(id("opts-kv")).innerHTML = o.available
    ? `
    <div><span>Expiry</span>${o.expiry}</div>
    <div><span>OI</span>${o.oi}</div>
    <div><span>Change in OI</span>${o.change_in_oi}</div>
    <div><span>IV</span>${o.iv}</div>
    <div><span>LTP</span>${o.ltp}</div>
    <div><span>Volume</span>${o.volume}</div>
  `
    : `<div style="color:var(--muted)">${o.reason || "Not available."}</div>`;

  const sc = data.stock_context || {};
  $(id("stock-kv")).innerHTML = sc.available
    ? `
    <div><span>Market cap</span>${sc.market_cap_cr ? "₹" + sc.market_cap_cr.toLocaleString("en-IN") + " cr" : "n/a"}</div>
    <div><span>Tier</span>${sc.tier}</div>
    <div><span>Sector</span>${sc.sector || "n/a"}</div>
    <div><span>Industry</span>${sc.industry || "n/a"}</div>
    <div><span>Sector rel. strength (10d)</span>${sc.sector_rel_strength_10d != null ? (sc.sector_rel_strength_10d * 100).toFixed(2) + "%" : "n/a"}</div>
  `
    : `<div style="color:var(--muted)">${sc.reason || "Not available."}</div>`;

  const rr = data.risk_reward || {};
  $(id("rr-kv")).innerHTML = rr.available
    ? `
    <div><span>Entry used</span>${rr.entry_used}</div>
    <div><span>Risk / unit</span>${rr.risk_per_unit}</div>
    <div><span>R:R to first target</span>${rr.rr_first_target}</div>
    <div><span>R:R to last target</span>${rr.rr_last_target}</div>
  `
    : `<div style="color:var(--muted)">${rr.reason || "Entry/SL/targets incomplete."}</div>`;

  const scr = data.screener || {};
  const screenerEl = $(id("screener-list"));
  if (scr.available) {
    screenerEl.innerHTML =
      `<div style="color:var(--muted);font-size:12px;margin-bottom:6px">${scr.passed}/${scr.total} conditions met</div>` +
      scr.checks
        .map(
          (c) =>
            `<div class="screener-check ${c.passed ? "pass" : "fail"}"><span class="check-icon">${c.passed ? "✓" : "✕"}</span>${c.name}</div>`
        )
        .join("");
  } else {
    screenerEl.innerHTML = `<div style="color:var(--muted);font-size:13px">${scr.reason || "Not available."}</div>`;
  }

  const newsEl = $(id("news-list"));
  newsEl.innerHTML = "";
  const n = data.news || {};
  if (!n.available || !n.headlines || n.headlines.length === 0) {
    newsEl.innerHTML = `<div style="color:var(--muted);font-size:13px">No recent headlines found.</div>`;
  } else {
    n.headlines.forEach((h) => {
      const d = document.createElement("div");
      d.className = "headline";
      d.innerHTML = `
        <span class="badge ${h.tone}">${h.tone}</span>
        &nbsp;<a href="${h.link}" target="_blank" rel="noopener">${h.title}</a>
        <div class="meta">${h.published}</div>
      `;
      newsEl.appendChild(d);
    });
  }
}

function renderTradeSetup(s, elId) {
  const el = $(elId);
  if (!el) return;
  const entry = s.entry_low === s.entry_high ? s.entry_low : `${s.entry_low}-${s.entry_high}`;
  const targets = (s.targets || []).join(" / ");
  const actionLabel = s.action === "sell" ? "SELL" : "BUY";
  const label = s.instrument === "EQ" ? "Price" : "Premium";
  el.innerHTML = `
    <div class="ts-item">
      <div class="ts-label">${actionLabel} ${label}</div>
      <div class="ts-value buy">${entry ?? "-"}</div>
    </div>
    <div class="ts-item">
      <div class="ts-label">Stop Loss</div>
      <div class="ts-value sl">${s.sl ?? "-"}</div>
    </div>
    <div class="ts-item">
      <div class="ts-label">Target${(s.targets || []).length > 1 ? "s" : ""}</div>
      <div class="ts-value target">${targets || "-"}</div>
    </div>
  `;
}

const DETAIL_MODAL_TEMPLATE = `
  <div class="score-header">
    <div class="score-circle" id="d-score-circle">--</div>
    <div>
      <div class="verdict" id="d-verdict-text"></div>
      <div class="verdict-sub" id="d-direction-text"></div>
    </div>
  </div>
  <div class="flags" id="d-flags-container"></div>
  <h2 style="margin-top:20px">Scoring breakdown</h2>
  <table id="d-breakdown-table">
    <thead><tr><th>Factor</th><th>Points</th><th style="width:200px">Bar</th><th>Notes</th></tr></thead>
    <tbody></tbody>
  </table>
  <div class="two-col" style="margin-top:20px">
    <div>
      <h2>Technicals</h2>
      <div class="kv" id="d-tech-kv"></div>
    </div>
    <div>
      <h2>Options (best-effort)</h2>
      <div class="kv" id="d-opts-kv"></div>
    </div>
  </div>
  <h2 style="margin-top:20px">Stock Context</h2>
  <div class="kv" id="d-stock-kv"></div>
  <h2 style="margin-top:20px">Risk / Reward</h2>
  <div class="kv" id="d-rr-kv"></div>
  <h2 style="margin-top:20px">Screener checklist</h2>
  <div id="d-screener-list"></div>
  <h2 style="margin-top:20px">Recent news</h2>
  <div id="d-news-list"></div>
`;

// ---- Signal detail modal (used from History) ----
async function openSignalDetail(signalId) {
  const modal = $("detail-modal");
  const backdrop = $("detail-modal-backdrop");
  modal.classList.remove("hidden");
  backdrop.classList.remove("hidden");
  $("detail-modal-body").innerHTML = `<div style="color:var(--muted);padding:20px">Loading…</div>`;
  $("detail-raw-text").classList.add("hidden");
  try {
    const res = await fetch(`/api/signals/${signalId}`);
    if (!res.ok) throw new Error("Could not load signal detail");
    const s = await res.json();
    $("detail-modal-body").innerHTML = DETAIL_MODAL_TEMPLATE;
    $("detail-modal-title").textContent =
      `#${s.id} · ${s.resolved_symbol || s.symbol}${s.instrument !== "EQ" ? " " + s.strike + s.instrument : ""} · ${s.channel}`;
    if (s.raw_text) {
      $("detail-raw-text").textContent = s.raw_text;
      $("detail-raw-text").classList.remove("hidden");
    }
    renderTradeSetup(s, "detail-trade-setup");
    renderEvaluation(s.evaluation || {}, "d-");
  } catch (e) {
    $("detail-modal-body").innerHTML = `<div style="color:var(--red);padding:20px">Error: ${e.message}</div>`;
  }
}

function closeSignalDetail() {
  $("detail-modal").classList.add("hidden");
  $("detail-modal-backdrop").classList.add("hidden");
}
$("detail-modal-backdrop").onclick = closeSignalDetail;
$("btn-close-detail").onclick = closeSignalDetail;

// ---- History ----
function renderSignalRows(rows, tableSelector) {
  const tbody = document.querySelector(`${tableSelector} tbody`);
  tbody.innerHTML = "";
  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" style="color:var(--muted)">No signals yet.</td></tr>`;
    return;
  }
  rows.forEach((s) => {
    const tr = document.createElement("tr");
    tr.className = "history-row-clickable";
    tr.title = "Click for full evaluation report";
    tr.innerHTML = `
      <td>${s.id}</td>
      <td>${s.source === "telegram" ? "📡" : s.source === "scanner" ? "🔍" : "✍️"}</td>
      <td>${s.channel}</td>
      <td>${s.resolved_symbol || s.symbol}${s.instrument !== "EQ" ? " " + s.strike + s.instrument : ""}</td>
      <td>${s.signal_type}</td>
      <td>${s.score}</td>
      <td>${s.verdict}</td>
      <td>
        <select class="outcome-select" data-id="${s.id}">
          <option value="pending" ${s.outcome === "pending" ? "selected" : ""}>Pending</option>
          <option value="target_hit" ${s.outcome === "target_hit" ? "selected" : ""}>Target hit</option>
          <option value="sl_hit" ${s.outcome === "sl_hit" ? "selected" : ""}>SL hit</option>
          <option value="partial" ${s.outcome === "partial" ? "selected" : ""}>Partial</option>
        </select>
      </td>
    `;
    tr.onclick = () => openSignalDetail(s.id);
    tbody.appendChild(tr);
  });

  tbody.querySelectorAll(".outcome-select").forEach((sel) => {
    sel.onclick = (e) => e.stopPropagation();
    sel.onchange = async (e) => {
      e.stopPropagation();
      await fetch(`/api/signals/${sel.dataset.id}/outcome`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ outcome: sel.value }),
      });
    };
  });
}

async function loadHistory() {
  const res = await fetch("/api/signals");
  const rows = await res.json();
  renderSignalRows(rows, "#history-table");
}

// ---- Dashboard (home) ----
async function loadDashboard() {
  await Promise.all([loadDashboardSummary(), loadDashboardSignals()]);
}

async function loadDashboardSignals() {
  const res = await fetch("/api/signals");
  const rows = await res.json();
  renderSignalRows(rows.slice(0, 15), "#dashboard-signals-table");
}

async function loadDashboardSummary() {
  const tiles = [];

  try {
    const meRes = await fetch("/api/me");
    const posRes = await fetch("/api/trading/pnl-summary");
    if (posRes.ok) {
      const pnl = await posRes.json();
      tiles.push(["Open positions", pnl.open_positions]);
      tiles.push(["Realized P&L today", (pnl.realized_today >= 0 ? "+" : "") + pnl.realized_today]);
    }
  } catch (e) {}

  try {
    const sigRes = await fetch("/api/signals");
    if (sigRes.ok) {
      const signals = await sigRes.json();
      tiles.push(["Total signals", signals.length]);
      const pending = signals.filter((s) => s.outcome === "pending").length;
      tiles.push(["Pending outcome", pending]);
    }
  } catch (e) {}

  try {
    const scanRes = await fetch("/api/scanner/runs/latest");
    if (scanRes.ok) {
      const run = await scanRes.json();
      tiles.push(["CE qualified", run.ce_qualified_count]);
      tiles.push(["PE qualified", run.pe_qualified_count]);
      tiles.push(["Last scan", run.completed_at ? new Date(run.completed_at).toLocaleTimeString("en-IN") : run.status]);
    } else {
      tiles.push(["F&O scan", "none yet"]);
    }
  } catch (e) {}

  try {
    const tgRes = await fetch("/api/telegram/status");
    if (tgRes.ok) {
      const tg = await tgRes.json();
      tiles.push(["Telegram", tg.listening ? "Listening" : tg.authorized ? "Connected" : "Not connected"]);
    }
  } catch (e) {}

  $("dashboard-summary-grid").innerHTML = tiles
    .map(([label, value]) => `<div class="summary-tile"><div class="tile-value">${value}</div><div class="tile-label">${label}</div></div>`)
    .join("");
}

$("dashboard-view-all").onclick = (e) => {
  e.preventDefault();
  showTab("history");
};

// ---- Telegram ----
async function loadTelegramTab() {
  await refreshTelegramStatus();
  await refreshTelegramChannels();
  await loadBroadcastSettings();
}

async function loadBroadcastSettings() {
  const select = $("bc-target-channel");
  select.innerHTML =
    `<option value="">-- choose a channel --</option>` +
    _allChannels
      .map((c) => `<option value="${c.telegram_chat_id}" data-title="${(c.title || "").replace(/"/g, "&quot;")}">${c.title || c.telegram_chat_id}</option>`)
      .join("");

  const res = await fetch("/api/telegram/broadcast-settings");
  const s = await res.json();
  $("bc-enabled").value = String(!!s.enabled);
  $("bc-min-score").value = s.min_score ?? 60;
  if (s.target_chat_id) select.value = String(s.target_chat_id);
}

$("btn-save-broadcast-settings").onclick = async () => {
  const statusEl = $("broadcast-settings-status");
  const select = $("bc-target-channel");
  const selectedOption = select.options[select.selectedIndex];
  const payload = {
    enabled: $("bc-enabled").value === "true",
    target_chat_id: select.value ? parseInt(select.value, 10) : null,
    target_chat_title: select.value ? selectedOption.dataset.title || selectedOption.textContent : null,
    min_score: parseFloat($("bc-min-score").value) || 0,
  };
  if (payload.enabled && !payload.target_chat_id) {
    statusEl.textContent = "Pick a target channel first.";
    return;
  }
  statusEl.textContent = "Saving…";
  try {
    const res = await fetch("/api/telegram/broadcast-settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error((await res.json()).detail || "Save failed");
    statusEl.textContent = "Saved.";
  } catch (e) {
    statusEl.textContent = "Error: " + e.message;
  }
};

$("btn-send-broadcast-test").onclick = async () => {
  const statusEl = $("broadcast-settings-status");
  const btn = $("btn-send-broadcast-test");
  btn.disabled = true;
  statusEl.textContent = "Sending test message…";
  try {
    const res = await fetch("/api/telegram/broadcast-test", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Send failed");
    statusEl.textContent = `Sent to ${data.target}. Check the channel.`;
  } catch (e) {
    statusEl.textContent = "Error: " + e.message;
  } finally {
    btn.disabled = false;
  }
};

async function refreshTelegramStatus() {
  const badge = $("telegram-status-badge");
  const text = $("telegram-status-text");
  const loginPanel = $("telegram-login-panel");
  refreshSidebarConn();
  try {
    const res = await fetch("/api/telegram/status");
    const s = await res.json();
    if (!s.configured) {
      badge.className = "badge negative";
      badge.textContent = "not configured";
      text.textContent = "Enter your API ID and API Hash below, then save.";
      loginPanel.classList.add("hidden");
    } else if (!s.authorized) {
      badge.className = "badge negative";
      badge.textContent = "not logged in";
      text.textContent = "Credentials saved. Log in below (one-time).";
      loginPanel.classList.remove("hidden");
    } else if (s.listening) {
      badge.className = "badge positive";
      badge.textContent = "listening";
      text.textContent = "Connected and watching your enabled channels for new signals.";
      loginPanel.classList.add("hidden");
    } else {
      badge.className = "badge neutral";
      badge.textContent = "connected";
      text.textContent = "Logged in, but the listener isn't active yet -- click \"Recheck status\".";
      loginPanel.classList.add("hidden");
    }
  } catch (e) {
    badge.className = "badge negative";
    badge.textContent = "error";
    text.textContent = String(e);
  }
}

function resetLoginSteps(toStep) {
  $("login-step-phone").classList.toggle("hidden", toStep !== "phone");
  $("login-step-code").classList.toggle("hidden", toStep !== "code");
  $("login-step-password").classList.toggle("hidden", toStep !== "password");
  $("login-error").innerHTML = "";
}

function showLoginError(msg) {
  $("login-error").innerHTML = `<div>⚠ ${msg}</div>`;
}

$("btn-send-code").onclick = async () => {
  const phone = $("login-phone").value.trim();
  if (!phone) return showLoginError("Enter your phone number, with country code.");
  try {
    const res = await fetch("/api/telegram/login/send-code", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to send code");
    if (data.already_authorized) {
      await refreshTelegramStatus();
      return;
    }
    resetLoginSteps("code");
  } catch (e) {
    showLoginError(e.message);
  }
};

$("btn-change-phone").onclick = () => resetLoginSteps("phone");

$("btn-verify-code").onclick = async () => {
  const code = $("login-code").value.trim();
  if (!code) return showLoginError("Enter the code Telegram sent you.");
  try {
    const res = await fetch("/api/telegram/login/verify-code", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Verification failed");
    if (data.needs_password) {
      resetLoginSteps("password");
      return;
    }
    if (data.logged_in) {
      $("login-code").value = "";
      await refreshTelegramStatus();
    }
  } catch (e) {
    showLoginError(e.message);
  }
};

$("btn-verify-password").onclick = async () => {
  const password = $("login-password").value;
  if (!password) return showLoginError("Enter your 2FA password.");
  try {
    const res = await fetch("/api/telegram/login/verify-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Verification failed");
    if (data.logged_in) {
      $("login-password").value = "";
      await refreshTelegramStatus();
    }
  } catch (e) {
    showLoginError(e.message);
  }
};

$("btn-recheck-status").onclick = async () => {
  $("telegram-status-badge").textContent = "checking…";
  try {
    await fetch("/api/telegram/reconnect", { method: "POST" });
  } catch (e) {
    // status refresh below will surface the current state either way
  }
  await refreshTelegramStatus();
};

$("btn-save-telegram-config").onclick = async () => {
  const statusEl = $("config-status");
  const api_id = $("cfg-api-id").value.trim();
  const api_hash = $("cfg-api-hash").value.trim();
  if (!api_id || !api_hash) {
    statusEl.textContent = "Both fields are required.";
    return;
  }
  statusEl.textContent = "Saving…";
  try {
    const res = await fetch("/api/telegram/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_id, api_hash }),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Save failed");
    }
    $("cfg-api-hash").value = "";
    statusEl.textContent = "Saved.";
    await fetch("/api/telegram/reconnect", { method: "POST" }).catch(() => {});
    await refreshTelegramStatus();
  } catch (e) {
    statusEl.textContent = "Error: " + e.message;
  }
};

let _allChannels = [];

async function refreshTelegramChannels() {
  const res = await fetch("/api/telegram/channels");
  _allChannels = await res.json();
  renderMonitoredList();
  renderBrowseList();
}

function renderMonitoredList() {
  const container = $("monitored-list");
  const monitored = _allChannels.filter((c) => c.enabled);

  $("monitored-subtitle").textContent =
    `${monitored.length} channel${monitored.length === 1 ? "" : "s"} · signals will be received from enabled channels`;

  container.innerHTML = "";
  if (monitored.length === 0) {
    container.innerHTML = `<div class="channel-empty">No channels monitored yet. Click "+ Add Channel" to pick from your synced Telegram channels.</div>`;
    return;
  }
  monitored.forEach((c) => container.appendChild(buildMonitoredRow(c)));
}

function buildMonitoredRow(c) {
  const row = document.createElement("div");
  row.className = "channel-row";
  row.innerHTML = `
    <span class="channel-dot on"></span>
    <div class="channel-info">
      <div class="channel-name">${c.title || "(untitled)"}${c.username ? " &middot; @" + c.username : ""}</div>
      <div class="channel-id">${c.telegram_chat_id}</div>
    </div>
    <div class="channel-actions">
      <button class="pill-toggle on" data-id="${c.id}" data-action="toggle">ON</button>
      <button class="icon-btn" data-id="${c.id}" data-action="remove" title="Stop monitoring">🗑</button>
    </div>
  `;
  row.querySelector('[data-action="toggle"]').onclick = () => setChannelEnabled(c.id, false);
  row.querySelector('[data-action="remove"]').onclick = () => setChannelEnabled(c.id, false);
  return row;
}

function renderBrowseList() {
  const container = $("browse-list");
  const term = ($("browse-search").value || "").trim().toLowerCase();
  const candidates = _allChannels.filter((c) => !c.enabled);
  const filtered = term
    ? candidates.filter(
        (c) =>
          (c.title || "").toLowerCase().includes(term) || (c.username || "").toLowerCase().includes(term)
      )
    : candidates;

  $("browse-heading").textContent = `Browse Channels (${candidates.length})`;

  container.innerHTML = "";
  if (candidates.length === 0) {
    container.innerHTML = `<div class="channel-empty">All synced channels are already monitored. Click "Sync my channels from Telegram" below to pull in new ones.</div>`;
    return;
  }
  if (filtered.length === 0) {
    container.innerHTML = `<div class="channel-empty">No channels match "${term}".</div>`;
    return;
  }
  filtered.forEach((c) => container.appendChild(buildBrowseRow(c)));
}

function buildBrowseRow(c) {
  const row = document.createElement("div");
  row.className = "channel-row";
  row.innerHTML = `
    <span class="channel-dot"></span>
    <div class="channel-info">
      <div class="channel-name">${c.title || "(untitled)"}${c.username ? " &middot; @" + c.username : ""}</div>
      <div class="channel-id">${c.telegram_chat_id}</div>
    </div>
    <button class="add-btn" data-id="${c.id}">+ Add</button>
  `;
  row.querySelector(".add-btn").onclick = () => setChannelEnabled(c.id, true);
  return row;
}

async function setChannelEnabled(id, enabled) {
  await fetch(`/api/telegram/channels/${id}/toggle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  const c = _allChannels.find((x) => x.id === id);
  if (c) c.enabled = enabled ? 1 : 0;
  renderMonitoredList();
  renderBrowseList();
}

$("btn-add-channel").onclick = () => {
  $("browse-channels-panel").classList.remove("hidden");
  renderBrowseList();
};
$("btn-close-browse").onclick = (e) => {
  e.preventDefault();
  $("browse-channels-panel").classList.add("hidden");
};
$("browse-search").oninput = () => renderBrowseList();

$("btn-sync-channels").onclick = async () => {
  const statusEl = $("sync-status");
  statusEl.textContent = "Syncing…";
  try {
    const res = await fetch("/api/telegram/sync-channels", { method: "POST" });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Sync failed");
    }
    _allChannels = await res.json();
    renderMonitoredList();
    renderBrowseList();
    statusEl.textContent = `Synced ${_allChannels.length} channel(s)/group(s).`;
  } catch (e) {
    statusEl.textContent = "Error: " + e.message;
  }
};

// ---- Stats ----
async function loadStats() {
  const res = await fetch("/api/channels/stats");
  const rows = await res.json();
  const tbody = document.querySelector("#stats-table tbody");
  tbody.innerHTML = "";
  rows.forEach((c) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${c.channel}</td>
      <td>${c.total}</td>
      <td>${c.targets_hit}</td>
      <td>${c.sl_hit}</td>
      <td>${c.partial}</td>
      <td>${c.pending}</td>
      <td>${c.hit_rate != null ? c.hit_rate + "%" : "n/a"}</td>
      <td>${c.avg_score != null ? c.avg_score : "n/a"}</td>
    `;
    tbody.appendChild(tr);
  });
}

// ---- Positions ----
async function loadPositions() {
  const res = await fetch("/api/trading/positions");
  const rows = await res.json();
  const summaryRes = await fetch("/api/trading/pnl-summary");
  const summary = await summaryRes.json();
  $("positions-summary").textContent =
    `${summary.open_positions} open · realized today: ${summary.realized_today >= 0 ? "+" : ""}${summary.realized_today}`;

  const tbody = document.querySelector("#positions-table tbody");
  tbody.innerHTML = "";
  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9" style="color:var(--muted)">No open positions. Enable auto-trade in Broker Setup, or positions will appear here once a signal clears your score threshold.</td></tr>`;
    return;
  }
  rows.forEach((o) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${o.id}</td>
      <td><span class="badge ${o.mode === "live" ? "negative" : "neutral"}">${o.mode}</span></td>
      <td>${o.resolved_symbol || o.symbol}${o.instrument !== "EQ" ? " " + o.strike + o.instrument : ""}</td>
      <td>${o.side}</td>
      <td>${o.quantity}</td>
      <td>${o.entry_price ?? "-"}</td>
      <td>${o.sl ?? "-"}</td>
      <td>${o.target ?? "-"}</td>
      <td><button class="icon-btn" data-id="${o.id}" title="Close position">✕</button></td>
    `;
    tr.querySelector("button").onclick = async () => {
      await fetch(`/api/trading/orders/${o.id}/close`, { method: "POST" });
      loadPositions();
    };
    tbody.appendChild(tr);
  });
}

// ---- Order book ----
async function loadOrderBook() {
  const res = await fetch("/api/trading/orders");
  const rows = await res.json();
  const tbody = document.querySelector("#orderbook-table tbody");
  tbody.innerHTML = "";
  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="10" style="color:var(--muted)">No orders yet.</td></tr>`;
    return;
  }
  rows.forEach((o) => {
    const pnlColor = o.pnl == null ? "var(--muted)" : o.pnl >= 0 ? "var(--green)" : "var(--red)";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${o.id}</td>
      <td>${(o.created_at || "").slice(0, 16).replace("T", " ")}</td>
      <td><span class="badge ${o.mode === "live" ? "negative" : "neutral"}">${o.mode}</span></td>
      <td>${o.resolved_symbol || o.symbol}${o.instrument !== "EQ" ? " " + o.strike + o.instrument : ""}</td>
      <td>${o.side}</td>
      <td>${o.quantity}</td>
      <td>${o.entry_price ?? "-"}</td>
      <td>${o.exit_price ?? "-"}</td>
      <td style="color:${pnlColor}">${o.pnl != null ? (o.pnl >= 0 ? "+" : "") + o.pnl : "-"}</td>
      <td><span class="badge ${o.status === "open" ? "pending" : o.status === "sl_hit" ? "negative" : "positive"}">${o.status}</span></td>
    `;
    tbody.appendChild(tr);
  });
}

// ---- Broker Setup ----
const BROKERS = [
  { id: "upstox", name: "Upstox" },
  { id: "dhan", name: "Dhan" },
  { id: "angelone", name: "Angel One (SmartAPI)" },
];

async function loadBrokerTab() {
  await Promise.all([loadAutoTradeSettings(), loadBrokerList(), loadKiteStatus()]);
}

async function loadKiteStatus() {
  const badge = $("kite-status-badge");
  const text = $("kite-status-text");
  try {
    const res = await fetch("/api/broker/kite/status");
    const s = await res.json();
    if (!s.configured) {
      badge.className = "badge negative";
      badge.textContent = "not configured";
      text.textContent = "Enter your API key/secret and save.";
    } else if (s.logged_in_today) {
      badge.className = "badge positive";
      badge.textContent = "logged in today";
      text.textContent = s.kite_user_id ? `Connected as ${s.kite_user_id}.` : "Connected.";
    } else {
      badge.className = "badge negative";
      badge.textContent = "login required";
      text.textContent = "Click \"Log in to Kite\" -- today's session hasn't started yet.";
    }
  } catch (e) {
    badge.className = "badge negative";
    badge.textContent = "error";
    text.textContent = String(e);
  }
}

$("btn-save-kite-credentials").onclick = async () => {
  const warnEl = $("kite-warning");
  warnEl.innerHTML = "";
  const api_key = $("kite-api-key").value.trim();
  const api_secret = $("kite-api-secret").value.trim();
  if (!api_key || !api_secret) {
    warnEl.innerHTML = `<div>⚠ Both fields are required.</div>`;
    return;
  }
  try {
    const res = await fetch("/api/broker/kite/credentials", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key, api_secret }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || "Save failed");
    $("kite-api-secret").value = "";
    await loadKiteStatus();
  } catch (e) {
    warnEl.innerHTML = `<div>⚠ ${e.message}</div>`;
  }
};

$("btn-kite-login").onclick = async () => {
  const warnEl = $("kite-warning");
  warnEl.innerHTML = "";
  try {
    const res = await fetch("/api/broker/kite/login-url");
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Could not get login URL");
    window.open(data.url, "_blank");
    warnEl.innerHTML = `<div style="color:var(--muted)">Log in on the Zerodha tab that just opened, then come back here -- it'll redirect and this status will update.</div>`;
  } catch (e) {
    warnEl.innerHTML = `<div>⚠ ${e.message}</div>`;
  }
};

async function loadAutoTradeSettings() {
  const res = await fetch("/api/trading/settings");
  const s = await res.json();
  $("at-enabled").value = String(!!s.enabled);
  $("at-mode").value = s.mode;
  $("at-min-score").value = s.min_score;
  $("at-quantity").value = s.quantity;
  $("at-max-positions").value = s.max_open_positions;
  $("at-max-loss").value = s.max_daily_loss ?? "";
  updateLiveWarning();
}

function updateLiveWarning() {
  const warnEl = $("at-live-warning");
  if ($("at-mode").value === "live") {
    warnEl.innerHTML = `<div>⚠ Live mode places REAL orders on your connected Zerodha account with REAL money when a signal clears your score threshold -- no per-trade confirmation. Make sure Kite Connect below shows "logged in today" first, and that you've paper-traded this setup enough to trust it. If Kite isn't connected, live signals are logged as "not placed" with a reason, never silently skipped.</div>`;
  } else {
    warnEl.innerHTML = "";
  }
}
$("at-mode").onchange = updateLiveWarning;

$("btn-save-at-settings").onclick = async () => {
  const statusEl = $("at-settings-status");
  const payload = {
    enabled: $("at-enabled").value === "true",
    mode: $("at-mode").value,
    min_score: parseFloat($("at-min-score").value),
    quantity: parseFloat($("at-quantity").value),
    max_open_positions: parseInt($("at-max-positions").value, 10),
    max_daily_loss: $("at-max-loss").value ? parseFloat($("at-max-loss").value) : null,
  };
  statusEl.textContent = "Saving…";
  try {
    const res = await fetch("/api/trading/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error((await res.json()).detail || "Save failed");
    statusEl.textContent = "Saved.";
  } catch (e) {
    statusEl.textContent = "Error: " + e.message;
  }
};

async function loadBrokerList() {
  const res = await fetch("/api/broker/accounts");
  const accounts = await res.json();
  const byBroker = Object.fromEntries(accounts.map((a) => [a.broker, a]));

  const container = $("broker-list");
  container.innerHTML = "";
  BROKERS.forEach((b) => {
    const acct = byBroker[b.id];
    const connected = acct && acct.connected;
    const row = document.createElement("div");
    row.className = "channel-row";
    row.innerHTML = `
      <span class="channel-dot ${connected ? "on" : ""}"></span>
      <div class="channel-info">
        <div class="channel-name">${b.name}</div>
        <div class="channel-id">${connected ? "Connected" : "Not connected"}</div>
      </div>
      <button class="${connected ? "pill-toggle on" : "add-btn"}" data-id="${b.id}">
        ${connected ? "Disconnect" : "+ Connect"}
      </button>
    `;
    row.querySelector("button").onclick = () =>
      connected ? disconnectBroker(b.id) : openBrokerConnect(b);
    container.appendChild(row);
  });
}

function openBrokerConnect(broker) {
  $("broker-connect-panel").classList.remove("hidden");
  $("broker-connect-heading").textContent = `Connect ${broker.name}`;
  $("broker-cred-key").value = "";
  $("broker-cred-secret").value = "";
  $("btn-save-broker-cred").onclick = async () => {
    await fetch("/api/broker/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        broker: broker.id,
        credentials: { key: $("broker-cred-key").value.trim(), secret: $("broker-cred-secret").value.trim() },
      }),
    });
    $("broker-connect-panel").classList.add("hidden");
    loadBrokerList();
  };
}
$("btn-cancel-broker-cred").onclick = () => $("broker-connect-panel").classList.add("hidden");

async function disconnectBroker(brokerId) {
  await fetch(`/api/broker/${brokerId}/disconnect`, { method: "POST" });
  loadBrokerList();
}

// ---- F&O Scanner ----
let _scannerFilter = "ALL";
let _scannerLatestRunId = null;

async function loadScannerTab() {
  await Promise.all([refreshScannerUniverseCount(), loadLatestScanSummary(), loadScannerSignalSettings()]);
}

async function loadScannerSignalSettings() {
  const res = await fetch("/api/scanner/signal-settings");
  const s = await res.json();
  $("chk-scanner-signals").checked = !!s.enabled;
}

$("chk-scanner-signals").onchange = async (e) => {
  const statusEl = $("scanner-signals-status");
  try {
    await fetch("/api/scanner/signal-settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: e.target.checked }),
    });
    statusEl.textContent = e.target.checked ? "Enabled." : "Disabled.";
  } catch (err) {
    statusEl.textContent = "Error: " + err.message;
  }
};

async function refreshScannerUniverseCount() {
  try {
    const res = await fetch("/api/scanner/universe");
    const rows = await res.json();
    $("scanner-universe-count").textContent = `${rows.length} F&O stocks in universe`;
  } catch (e) {
    $("scanner-universe-count").textContent = "";
  }
}

$("btn-refresh-universe").onclick = async () => {
  const statusEl = $("scanner-status");
  statusEl.textContent = "Refreshing universe from NSE…";
  try {
    const res = await fetch("/api/scanner/universe/refresh", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Refresh failed");
    statusEl.textContent = `Universe refreshed: ${data.stocks} stocks, ${data.indices} indices.`;
    refreshScannerUniverseCount();
  } catch (e) {
    statusEl.textContent = "Error: " + e.message;
  }
};

$("btn-run-scan").onclick = async () => {
  const statusEl = $("scanner-status");
  const btn = $("btn-run-scan");
  btn.disabled = true;
  statusEl.textContent = "Scanning F&O universe… this can take up to a minute.";
  try {
    const res = await fetch("/api/scanner/run", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Scan failed");
    statusEl.textContent =
      `Scan complete: ${data.stocks_scanned}/${data.stocks_total} scanned -- ` +
      `${data.ce_qualified} CE, ${data.pe_qualified} PE, ${data.near_ce + data.near_pe} near, ${data.unavailable} unavailable` +
      (data.signals_generated ? `, ${data.signals_generated} signal(s) generated.` : ".");
    await loadLatestScanSummary();
  } catch (e) {
    statusEl.textContent = "Error: " + e.message;
  } finally {
    btn.disabled = false;
  }
};

async function loadLatestScanSummary() {
  try {
    const res = await fetch("/api/scanner/runs/latest");
    if (!res.ok) {
      renderScannerSummary(null);
      renderScannerResultsTable([]);
      return;
    }
    const run = await res.json();
    _scannerLatestRunId = run.id;
    renderScannerSummary(run);
    await loadScannerResults();
  } catch (e) {
    renderScannerSummary(null);
  }
}

function renderScannerSummary(run) {
  const grid = $("scanner-summary-grid");
  if (!run) {
    grid.innerHTML = `<div style="color:var(--muted);font-size:13px">No scans yet. Click "Refresh universe" then "Run scan now".</div>`;
    return;
  }
  const nearTotal = run.near_ce_count + run.near_pe_count;
  const tiles = [
    ["Total F&O stocks", run.stocks_total],
    ["Stocks scanned", run.stocks_scanned],
    ["CE qualified", run.ce_qualified_count],
    ["PE qualified", run.pe_qualified_count],
    ["Near qualified", nearTotal],
    ["Data unavailable", run.unavailable_count],
    ["Data conflicts", run.conflict_count],
    ["Latest scan", run.completed_at ? new Date(run.completed_at).toLocaleTimeString("en-IN") : run.status],
  ];
  grid.innerHTML = tiles
    .map(([label, value]) => `<div class="summary-tile"><div class="tile-value">${value}</div><div class="tile-label">${label}</div></div>`)
    .join("");
}

document.querySelectorAll(".filter-tab").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll(".filter-tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    _scannerFilter = btn.dataset.filter;
    loadScannerResults();
  };
});

async function loadScannerResults() {
  if (!_scannerLatestRunId) return;
  let url = `/api/scanner/results?run_id=${_scannerLatestRunId}`;
  if (_scannerFilter === "NEAR") {
    // "Near" spans two classifications -- fetch both and merge client-side.
    const [ceRes, peRes] = await Promise.all([
      fetch(`${url}&classification=NEAR_CE`),
      fetch(`${url}&classification=NEAR_PE`),
    ]);
    const rows = [...(await ceRes.json()), ...(await peRes.json())];
    renderScannerResultsTable(rows);
    return;
  }
  if (_scannerFilter !== "ALL") url += `&classification=${_scannerFilter}`;
  const res = await fetch(url);
  const rows = await res.json();
  renderScannerResultsTable(rows);
}

function dirBadge(classification) {
  if (classification === "CE_QUALIFIED") return `<span class="dir-badge ce">CE</span>`;
  if (classification === "PE_QUALIFIED") return `<span class="dir-badge pe">PE</span>`;
  if (classification === "NEAR_CE") return `<span class="dir-badge near">Near CE</span>`;
  if (classification === "NEAR_PE") return `<span class="dir-badge near">Near PE</span>`;
  if (classification === "DATA_UNAVAILABLE") return `<span class="dir-badge unavailable">N/A</span>`;
  if (classification === "DATA_CONFLICT") return `<span class="dir-badge pe">Conflict</span>`;
  return `<span class="dir-badge unavailable">--</span>`;
}

function renderScannerResultsTable(rows) {
  const tbody = document.querySelector("#scanner-results-table tbody");
  tbody.innerHTML = "";
  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" style="color:var(--muted)">No results for this filter.</td></tr>`;
    return;
  }
  rows.forEach((r) => {
    const tr = document.createElement("tr");
    tr.className = "history-row-clickable";
    tr.innerHTML = `
      <td>${dirBadge(r.classification)}</td>
      <td>${r.symbol}</td>
      <td>${r.company_name || "-"}</td>
      <td>${r.sector || "-"}</td>
      <td>${r.ce_passed_count != null ? r.ce_passed_count + "/" + r.ce_total_count : "-"}</td>
      <td>${r.pe_passed_count != null ? r.pe_passed_count + "/" + r.pe_total_count : "-"}</td>
      <td>${r.data_timestamp ? new Date(r.data_timestamp).toLocaleTimeString("en-IN") : "-"}</td>
      <td>${r.error_reason ? `<span style="color:var(--muted);font-size:11px">${r.error_reason}</span>` : ""}</td>
    `;
    tr.onclick = () => openScanDetail(r.symbol);
    tbody.appendChild(tr);
  });
}

async function openScanDetail(symbol) {
  const modal = $("scan-modal");
  const backdrop = $("scan-modal-backdrop");
  modal.classList.remove("hidden");
  backdrop.classList.remove("hidden");
  $("scan-ce-conditions").innerHTML = `<div style="color:var(--muted)">Loading…</div>`;
  $("scan-pe-conditions").innerHTML = "";
  try {
    const res = await fetch(`/api/scanner/results/${symbol}?run_id=${_scannerLatestRunId}`);
    if (!res.ok) throw new Error("Could not load result");
    const r = await res.json();
    const src = r.snapshot && r.snapshot.data_source === "kite" ? " · Kite" : " · yfinance";
    $("scan-modal-title").textContent = `${r.symbol} -- ${r.company_name || ""} (${r.classification})${src}`;
    $("scan-ce-conditions").innerHTML = renderConditionList(r.ce_conditions);
    $("scan-pe-conditions").innerHTML = renderConditionList(r.pe_conditions);
  } catch (e) {
    $("scan-ce-conditions").innerHTML = `<div style="color:var(--red)">Error: ${e.message}</div>`;
  }
}

function renderConditionList(conditions) {
  if (!conditions || conditions.length === 0) return `<div style="color:var(--muted)">No condition data.</div>`;
  return conditions
    .map(
      (c) => `
      <div class="condition-row ${c.passed ? "pass" : "fail"}">
        <div class="cond-label">${c.passed ? "✓" : "✕"} ${c.label}${c.provisional ? '<span class="provisional-tag">provisional</span>' : ""}</div>
        <div class="cond-values">${c.actual_value} ${c.comparison} ${c.required_value}</div>
      </div>`
    )
    .join("");
}

function closeScanDetail() {
  $("scan-modal").classList.add("hidden");
  $("scan-modal-backdrop").classList.add("hidden");
}
$("btn-close-scan-detail").onclick = closeScanDetail;
$("scan-modal-backdrop").onclick = closeScanDetail;

$("btn-export-scan-csv").onclick = (e) => {
  e.preventDefault();
  if (!_scannerLatestRunId) return;
  let url = `/api/scanner/export.csv?run_id=${_scannerLatestRunId}`;
  if (_scannerFilter !== "ALL" && _scannerFilter !== "NEAR") url += `&classification=${_scannerFilter}`;
  window.open(url, "_blank");
};
