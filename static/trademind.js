// ---- TradeMind: broker-import trading journal, rule-compliance and process-score ----
// Loaded after app.js -- reuses its global $() helper. Kept in its own file since this is a
// genuinely separate subsystem from the rest of the app (see app/trademind/schema.py).

let _tmAccounts = [];

async function loadTradeMindTab() {
  await loadTmAccounts();
  showTmSubview("dashboard");
}

document.querySelectorAll("#tm-subnav .filter-tab").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll("#tm-subnav .filter-tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    showTmSubview(btn.dataset.tmView);
  };
});

function showTmSubview(name) {
  document.querySelectorAll(".tm-subview").forEach((el) => el.classList.toggle("hidden", el.id !== `tm-view-${name}`));
  if (name === "dashboard") loadTmDashboard();
  if (name === "accounts") loadTmAccounts();
  if (name === "import") { loadTmImportAccountSelect(); loadTmImports(); }
  if (name === "trades") loadTmTrades();
  if (name === "rules") { loadTmRules(); loadTmCompliance(); }
}

function tmFmtMoney(v) {
  if (v == null) return "-";
  const sign = v > 0 ? "+" : "";
  return sign + v.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

// ---- Dashboard ----
async function loadTmDashboard() {
  const res = await fetch("/api/trademind/dashboard");
  const s = await res.json();
  const pnlColor = s.net_pnl > 0 ? "var(--green)" : s.net_pnl < 0 ? "var(--red)" : "var(--muted)";
  const tiles = [
    ["Total trades", s.total_trades],
    ["Net P&L", `<span style="color:${pnlColor}">${tmFmtMoney(s.net_pnl)}</span>`],
    ["Gross P&L", tmFmtMoney(s.gross_pnl)],
    ["Total charges", tmFmtMoney(s.total_charges)],
    ["Win rate", s.win_rate != null ? s.win_rate + "%" : "-"],
    ["Avg process score", s.avg_process_score != null ? s.avg_process_score : "Not journaled yet"],
    ["Journaled trades", `${s.journaled_trades} / ${s.total_trades}`],
    ["Rule compliance", s.compliance.compliance_pct != null ? s.compliance.compliance_pct + "%" : "No rules evaluated yet"],
  ];
  $("tm-dashboard-grid").innerHTML = tiles
    .map(([label, value]) => `<div class="summary-tile"><div class="tile-value">${value}</div><div class="tile-label">${label}</div></div>`)
    .join("");
}

// ---- Broker accounts ----
async function loadTmAccounts() {
  const res = await fetch("/api/trademind/broker-accounts");
  _tmAccounts = await res.json();
  const tbody = document.querySelector("#tm-accounts-table tbody");
  tbody.innerHTML = _tmAccounts
    .map(
      (a) => `
      <tr>
        <td>${a.nickname}</td>
        <td>${a.broker}</td>
        <td>${a.masked_client_id || "-"}</td>
        <td>${a.account_purpose || "-"}</td>
        <td>${tmFmtMoney(a.starting_capital)}</td>
        <td>${a.last_synced_at ? new Date(a.last_synced_at).toLocaleString() : "Never"}</td>
      </tr>`
    )
    .join("");
}

$("btn-tm-add-account").onclick = async () => {
  const statusEl = $("tm-account-status");
  const nickname = $("tm-acc-nickname").value.trim();
  if (!nickname) {
    statusEl.textContent = "Nickname is required.";
    return;
  }
  statusEl.textContent = "Adding…";
  try {
    const res = await fetch("/api/trademind/broker-accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        broker: $("tm-acc-broker").value,
        nickname,
        masked_client_id: $("tm-acc-client-id").value.trim() || null,
        account_purpose: $("tm-acc-purpose").value,
        starting_capital: parseFloat($("tm-acc-capital").value) || 0,
        classification: $("tm-acc-classification").value,
      }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || "Failed to add account");
    $("tm-acc-nickname").value = "";
    $("tm-acc-client-id").value = "";
    $("tm-acc-capital").value = "";
    statusEl.textContent = "Added.";
    await loadTmAccounts();
  } catch (e) {
    statusEl.textContent = "Error: " + e.message;
  }
};

// ---- Import ----
function loadTmImportAccountSelect() {
  const select = $("tm-import-account");
  if (!_tmAccounts.length) {
    select.innerHTML = `<option value="">-- add a broker account first --</option>`;
    return;
  }
  select.innerHTML = _tmAccounts.map((a) => `<option value="${a.id}">${a.nickname} (${a.broker})</option>`).join("");
}

$("btn-tm-import").onclick = async () => {
  const statusEl = $("tm-import-status");
  const resultEl = $("tm-import-result");
  const accountId = $("tm-import-account").value;
  const fileInput = $("tm-import-file");
  if (!accountId) {
    statusEl.textContent = "Add and select a broker account first.";
    return;
  }
  if (!fileInput.files.length) {
    statusEl.textContent = "Choose a CSV file first.";
    return;
  }
  statusEl.textContent = "Uploading and importing…";
  resultEl.classList.add("hidden");
  try {
    const formData = new FormData();
    formData.append("file", fileInput.files[0]);
    const res = await fetch(`/api/trademind/broker-accounts/${accountId}/import`, { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Import failed");

    statusEl.textContent = "Done.";
    resultEl.classList.remove("hidden");
    if (data.status === "duplicate_file") {
      resultEl.innerHTML = `<div class="warnings"><div>This exact file was already imported successfully -- nothing new to do.</div></div>`;
    } else if (data.status === "failed") {
      resultEl.innerHTML = `<div class="warnings"><div>⚠ ${data.message}</div></div>`;
    } else {
      let rejectedHtml = "";
      if (data.rejected_rows && data.rejected_rows.length) {
        rejectedHtml = `<div style="margin-top:8px;color:var(--muted);font-size:12px">Rejected rows: ${data.rejected_rows
          .map((r) => `#${r.row_number} (${r.reason})`)
          .join(", ")}</div>`;
      }
      resultEl.innerHTML = `
        <div class="summary-grid">
          <div class="summary-tile"><div class="tile-value">${data.rows_processed}</div><div class="tile-label">Rows processed</div></div>
          <div class="summary-tile"><div class="tile-value">${data.rows_rejected}</div><div class="tile-label">Rows rejected</div></div>
          <div class="summary-tile"><div class="tile-value">${data.duplicate_rows}</div><div class="tile-label">Duplicates skipped</div></div>
          <div class="summary-tile"><div class="tile-value">${data.trades_created}</div><div class="tile-label">Trades created</div></div>
        </div>${rejectedHtml}`;
    }
    fileInput.value = "";
    await loadTmImports();
  } catch (e) {
    statusEl.textContent = "Error: " + e.message;
  }
};

async function loadTmImports() {
  const res = await fetch("/api/trademind/imports");
  const jobs = await res.json();
  const tbody = document.querySelector("#tm-imports-table tbody");
  tbody.innerHTML = jobs
    .map((j) => {
      const canRollback = j.status === "completed";
      return `
      <tr>
        <td>${j.file_name}</td>
        <td><span class="badge ${j.status === "completed" ? "positive" : j.status === "failed" ? "negative" : "pending"}">${j.status}</span></td>
        <td>${j.rows_processed}</td>
        <td>${j.rows_rejected}</td>
        <td>${j.duplicate_rows}</td>
        <td>${j.trades_created}</td>
        <td>${new Date(j.created_at).toLocaleString()}</td>
        <td>${canRollback ? `<button class="secondary" onclick="tmRollbackImport(${j.id})">Roll back</button>` : ""}</td>
      </tr>`;
    })
    .join("");
}

async function tmRollbackImport(jobId) {
  if (!confirm("Roll back this import? Every trade and execution it created will be permanently deleted.")) return;
  try {
    const res = await fetch(`/api/trademind/imports/${jobId}/rollback`, { method: "POST" });
    if (!res.ok) throw new Error((await res.json()).detail || "Rollback failed");
    await loadTmImports();
    await loadTmTrades();
    await loadTmDashboard();
  } catch (e) {
    alert("Error: " + e.message);
  }
}

// ---- Trades ----
async function loadTmTrades() {
  const res = await fetch("/api/trademind/trades");
  const trades = await res.json();
  const tbody = document.querySelector("#tm-trades-table tbody");
  tbody.innerHTML = trades
    .map((t) => {
      const pnlColor = t.net_pnl > 0 ? "var(--green)" : t.net_pnl < 0 ? "var(--red)" : "var(--muted)";
      const score = t.process_score != null ? t.process_score : "Not journaled";
      return `
      <tr style="cursor:pointer" onclick="openTmTradeDetail(${t.id})">
        <td>${t.symbol}</td>
        <td>${t.side}</td>
        <td>${t.quantity}</td>
        <td>${t.avg_entry_price.toFixed(2)}</td>
        <td>${t.avg_exit_price.toFixed(2)}</td>
        <td>${tmFmtMoney(t.gross_pnl)}</td>
        <td>${tmFmtMoney(t.charges)}</td>
        <td style="color:${pnlColor}">${tmFmtMoney(t.net_pnl)}</td>
        <td>${score}</td>
        <td><button class="secondary" onclick="event.stopPropagation();openTmTradeDetail(${t.id})">Open</button></td>
      </tr>`;
    })
    .join("");
}

async function openTmTradeDetail(tradeId) {
  const res = await fetch(`/api/trademind/trades/${tradeId}`);
  const t = await res.json();
  $("tm-trade-modal-title").textContent = `${t.symbol} -- ${t.side} ${t.quantity}`;

  const j = t.journal || {};
  const ruleRows = (t.rule_evaluations || [])
    .map((r) => `<div>${r.compliant ? "✅" : "❌"} ${r.rule_type.replace(/_/g, " ")} -- ${r.detail}</div>`)
    .join("") || `<div style="color:var(--muted)">No rule evaluations yet -- fill in the plan below and save it.</div>`;

  $("tm-trade-modal-body").innerHTML = `
    <div class="summary-grid" style="margin-bottom:16px">
      <div class="summary-tile"><div class="tile-value">${tmFmtMoney(t.net_pnl)}</div><div class="tile-label">Net P&amp;L</div></div>
      <div class="summary-tile"><div class="tile-value">${tmFmtMoney(t.charges)}</div><div class="tile-label">Charges</div></div>
      <div class="summary-tile"><div class="tile-value">${t.process_score != null ? t.process_score : "-"}</div><div class="tile-label">Process score</div></div>
    </div>

    <h3>Plan (for rule compliance)</h3>
    <div class="grid" style="grid-template-columns: 1fr 1fr 1fr">
      <label>Planned SL <input id="tm-plan-sl" type="number" step="0.01" value="${t.planned_sl ?? ""}" /></label>
      <label>Planned target <input id="tm-plan-target" type="number" step="0.01" value="${t.planned_target ?? ""}" /></label>
      <label>Planned risk (₹) <input id="tm-plan-risk" type="number" step="0.01" value="${t.planned_risk ?? ""}" /></label>
    </div>
    <div class="grid" style="grid-template-columns: 1fr 1fr">
      <label>Setup <input id="tm-plan-setup" value="${t.setup || ""}" /></label>
      <label>Strategy <input id="tm-plan-strategy" value="${t.strategy || ""}" /></label>
    </div>
    <div class="row"><button class="primary" onclick="saveTmPlan(${t.id})">Save plan</button></div>

    <h3 style="margin-top:16px">Rule compliance</h3>
    ${ruleRows}

    <h3 style="margin-top:16px">Journal</h3>
    <div class="row">
      <label style="display:flex;align-items:center;gap:6px;font-size:13px"><input type="checkbox" id="tm-j-setup" style="width:auto" ${j.setup_followed ? "checked" : ""}/> Setup followed</label>
      <label style="display:flex;align-items:center;gap:6px;font-size:13px"><input type="checkbox" id="tm-j-sl" style="width:auto" ${j.sl_followed ? "checked" : ""}/> Stop-loss followed</label>
      <label style="display:flex;align-items:center;gap:6px;font-size:13px"><input type="checkbox" id="tm-j-exit" style="width:auto" ${j.exit_plan_followed ? "checked" : ""}/> Exit plan followed</label>
    </div>
    <label>Emotional state <input id="tm-j-emotion" value="${j.emotional_state || ""}" /></label>
    <label>Mistakes <textarea id="tm-j-mistakes" rows="2">${j.mistakes || ""}</textarea></label>
    <label>Lesson learned <textarea id="tm-j-lesson" rows="2">${j.lesson_learned || ""}</textarea></label>
    <div class="row"><button class="primary" onclick="saveTmJournal(${t.id})">Save journal</button>
      <span id="tm-journal-status" style="color:var(--muted);font-size:12px"></span></div>
  `;

  $("tm-trade-modal-backdrop").classList.remove("hidden");
  $("tm-trade-modal").classList.remove("hidden");
}

$("btn-tm-close-trade").onclick = () => {
  $("tm-trade-modal-backdrop").classList.add("hidden");
  $("tm-trade-modal").classList.add("hidden");
};
$("tm-trade-modal-backdrop").onclick = () => $("btn-tm-close-trade").onclick();

async function saveTmPlan(tradeId) {
  await fetch(`/api/trademind/trades/${tradeId}/plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      planned_sl: parseFloat($("tm-plan-sl").value) || null,
      planned_target: parseFloat($("tm-plan-target").value) || null,
      planned_risk: parseFloat($("tm-plan-risk").value) || null,
      setup: $("tm-plan-setup").value || null,
      strategy: $("tm-plan-strategy").value || null,
    }),
  });
  await openTmTradeDetail(tradeId);
}

async function saveTmJournal(tradeId) {
  const statusEl = $("tm-journal-status");
  statusEl.textContent = "Saving…";
  try {
    await fetch(`/api/trademind/trades/${tradeId}/journal`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        setup_followed: $("tm-j-setup").checked,
        sl_followed: $("tm-j-sl").checked,
        exit_plan_followed: $("tm-j-exit").checked,
        emotional_state: $("tm-j-emotion").value || null,
        mistakes: $("tm-j-mistakes").value || null,
        lesson_learned: $("tm-j-lesson").value || null,
      }),
    });
    statusEl.textContent = "Saved.";
    await loadTmTrades();
    await loadTmDashboard();
  } catch (e) {
    statusEl.textContent = "Error: " + e.message;
  }
}

// ---- Rules ----
const TM_RULE_LABELS = {
  mandatory_stop_loss: "Mandatory stop-loss",
  min_risk_reward: "Minimum risk-reward ratio",
  max_daily_loss: "Maximum daily loss (₹)",
  max_trades_per_day: "Maximum trades per day",
};

async function loadTmRules() {
  const res = await fetch("/api/trademind/rules");
  const rules = await res.json();
  const tbody = document.querySelector("#tm-rules-table tbody");
  tbody.innerHTML = rules
    .map(
      (r) => `
      <tr>
        <td>${TM_RULE_LABELS[r.rule_type] || r.rule_type}</td>
        <td>${r.threshold_value}</td>
        <td><input type="checkbox" ${r.enabled ? "checked" : ""} onchange="tmToggleRule(${r.id}, this.checked)" /></td>
        <td><button class="secondary" onclick="tmDeleteRule(${r.id})">Delete</button></td>
      </tr>`
    )
    .join("");
}

$("btn-tm-add-rule").onclick = async () => {
  const ruleType = $("tm-rule-type").value;
  const threshold = parseFloat($("tm-rule-threshold").value);
  if (isNaN(threshold)) {
    alert("Enter a threshold value first.");
    return;
  }
  await fetch("/api/trademind/rules", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rule_type: ruleType, threshold_value: threshold }),
  });
  $("tm-rule-threshold").value = "";
  await loadTmRules();
};

async function tmToggleRule(ruleId, enabled) {
  await fetch(`/api/trademind/rules/${ruleId}/toggle?enabled=${enabled}`, { method: "POST" });
}

async function tmDeleteRule(ruleId) {
  if (!confirm("Delete this rule? Its past evaluations will be removed too.")) return;
  await fetch(`/api/trademind/rules/${ruleId}`, { method: "DELETE" });
  await loadTmRules();
  await loadTmCompliance();
}

async function loadTmCompliance() {
  const res = await fetch("/api/trademind/compliance-summary");
  const s = await res.json();
  $("tm-compliance-grid").innerHTML = [
    ["Rule evaluations", s.total_evaluations],
    ["Compliant", s.compliant],
    ["Compliance rate", s.compliance_pct != null ? s.compliance_pct + "%" : "-"],
  ]
    .map(([label, value]) => `<div class="summary-tile"><div class="tile-value">${value}</div><div class="tile-label">${label}</div></div>`)
    .join("");
}
