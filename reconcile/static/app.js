(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  function isNegative(value) {
    const s = String(value ?? "").trim();
    return s.startsWith("-") && !/^-?0(\.0+)?$/.test(s);
  }

  function amountCell(value) {
    const neg = isNegative(value);
    return `<span class="${neg ? "diff neg" : ""}">${esc(value)}</span>`;
  }

  function statusBadge(status, label) {
    return `<span class="badge ${esc(status)}">${esc(label || status)}</span>`;
  }

  function reasonChips(reasons) {
    if (!reasons || !reasons.length) return '<span class="muted">—</span>';
    return `<div class="chips">${reasons.map((r) =>
      `<span class="chip ${r.blocking ? "blocking" : ""}" title="${esc(r.message)}">${esc(r.message.split("：")[0])}</span>`
    ).join("")}</div>`;
  }

  async function api(path, options) {
    const res = await fetch(path, options);
    let data = null;
    try { data = await res.json(); } catch (_) { /* 非 JSON */ }
    if (!res.ok) {
      throw new Error((data && data.error) || `HTTP ${res.status}`);
    }
    return data;
  }

  let toastTimer = null;
  function toast(message, isError = false) {
    const el = $("#toast");
    el.textContent = message;
    el.classList.toggle("error", isError);
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 4200);
  }

  // ------------------------------------------------------------ 状态
  const state = {
    config: null,
    tab: "results",
    results: [],
    loaded: { daily: false, exceptions: false, imports: false },
    orderCache: new Map(),
  };

  function filtersFromInputs() {
    return {
      date: $("#fDate").value,
      status: $("#fStatus").value,
      currency: $("#fCurrency").value,
      reason: $("#fReason").value,
      q: $("#fQ").value.trim(),
    };
  }

  function filterQuery(extra = {}) {
    const params = new URLSearchParams();
    const f = { ...filtersFromInputs(), ...extra };
    Object.entries(f).forEach(([k, v]) => { if (v) params.set(k, v); });
    const qs = params.toString();
    return qs ? `?${qs}` : "";
  }

  // ------------------------------------------------------------ 对账结果
  async function loadResults() {
    try {
      const data = await api(`/api/results${filterQuery()}`);
      state.results = data.results;
      renderSummary(data.summary);
      renderResults(data.results);
      $("#dbState").textContent = `已加载 ${data.summary.order_count} 笔订单`;
      $("#dbState").classList.remove("muted");
      $("#exportResults").href = `/api/export/results.csv${filterQuery()}`;
    } catch (err) {
      toast(`加载对账结果失败：${err.message}`, true);
    }
  }

  function renderSummary(summary) {
    const box = $("#summary");
    if (!summary || summary.order_count === 0) { box.innerHTML = ""; return; }
    const cards = [];
    cards.push(`
      <div class="sum-card">
        <div class="cap"><span>当前筛选</span><span>${summary.order_count} 笔</span></div>
        <div class="big">${summary.match_count} 一致 · ${summary.mismatch_count} 差额 · ${summary.exception_count} 异常</div>
        <div class="small">一致率 ${summary.order_count ? Math.round(summary.match_count / summary.order_count * 100) : 0}%</div>
      </div>`);
    for (const c of summary.by_currency) {
      cards.push(`
        <div class="sum-card">
          <div class="cap"><span>${esc(c.currency)}</span><span>${c.order_count} 笔</span></div>
          <div class="big">净实收 ${esc(c.net_total)}</div>
          <div class="small">应收 ${esc(c.receivable_total)} · 差额 ${esc(c.difference_total)}</div>
          <div class="small">一致 ${c.match_count} / 差额 ${c.mismatch_count} / 异常 ${c.exception_count}</div>
        </div>`);
    }
    box.innerHTML = cards.join("");
  }

  function renderResults(rows) {
    const tbody = $("#resultsBody");
    const empty = $("#resultsEmpty");
    if (!rows.length) {
      tbody.innerHTML = "";
      empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden");
    tbody.innerHTML = rows.map((r) => `
      <tr class="clickable" data-order="${esc(r.order_id)}" data-version="${r.version}">
        <td class="mono">${esc(r.settlement_date || "未结算")}</td>
        <td class="mono"><strong>${esc(r.order_id)}</strong></td>
        <td>${esc(r.currency)}</td>
        <td class="num">${esc(r.receivable)}</td>
        <td class="num">${esc(r.gross_paid)}</td>
        <td class="num">${esc(r.valid_refund)}</td>
        <td class="num">${esc(r.net_received)}</td>
        <td class="num">${amountCell(r.difference)}</td>
        <td>${statusBadge(r.status, r.status_label)}</td>
        <td class="num">v${r.version}</td>
        <td>${reasonChips(r.reasons)}</td>
      </tr>`).join("");
  }

  function closeAllDetails() {
    $$("tr.detail-row").forEach((tr) => tr.remove());
    $$("tbody tr.expanded").forEach((tr) => tr.classList.remove("expanded"));
  }

  async function toggleOrderRow(tr) {
    const orderId = tr.dataset.order;
    const version = Number(tr.dataset.version);
    const existing = tr.nextElementSibling;
    if (existing && existing.classList.contains("detail-row") && existing.dataset.detailFor === orderId) {
      existing.remove();
      tr.classList.remove("expanded");
      return;
    }
    closeAllDetails();
    tr.classList.add("expanded");
    const detail = document.createElement("tr");
    detail.className = "detail-row";
    detail.dataset.detailFor = orderId;
    detail.innerHTML = `<td colspan="11"><div class="detail-panel muted">加载计算明细…</div></td>`;
    tr.after(detail);
    try {
      const key = `${orderId}:${version}`;
      let order = state.orderCache.get(key);
      if (!order) {
        order = (await api(`/api/orders/${encodeURIComponent(orderId)}?version=${version}`)).order;
        state.orderCache.set(key, order);
      }
      renderOrderDetail(detail.querySelector(".detail-panel"), order, tr);
    } catch (err) {
      detail.querySelector(".detail-panel").innerHTML =
        `<span class="diff neg">加载失败：${esc(err.message)}</span>`;
    }
  }

  function renderOrderDetail(panel, order, tr) {
    const formula = `
      <div class="formula">
        <span><span class="label">应收</span> <span class="value">${esc(order.receivable)}</span> ${esc(order.currency)}</span>
        <span class="op">|</span>
        <span><span class="label">支付总额</span> <span class="value">${esc(order.gross_paid)}</span></span>
        <span class="op">−</span>
        <span><span class="label">有效退款</span> <span class="value">${esc(order.valid_refund)}</span></span>
        <span class="op">=</span>
        <span><span class="label">净实收</span> <span class="value">${esc(order.net_received)}</span></span>
        <span class="op">·</span>
        <span><span class="label">差额</span> ${amountCell(order.difference)} ${statusBadge(order.status, order.status_label)}</span>
        ${order.excess_refund !== "0" && order.excess_refund !== "0.00" && order.excess_refund !== "0"
          ? `<span class="op">|</span><span class="label">其中超额退款</span> <span class="value diff neg">${esc(order.excess_refund)}</span>` : ""}
      </div>`;

    const detailRows = order.details.map((d) => {
      const cls = d.line_type === "summary" ? "summary-line" : (d.included ? "" : "excluded");
      const typeLabel = { payment: "支付", refund: "退款", summary: "汇总" }[d.line_type] || d.line_type;
      const trace = d.txn_id
        ? `<button class="ghost" data-action="trace" data-txn="${esc(d.txn_id)}">溯源</button>`
        : "";
      return `<tr class="${cls}">
        <td>${esc(typeLabel)}</td>
        <td class="mono">${esc(d.event_ref || "")}</td>
        <td class="mono">${esc(d.txn_id || "")}</td>
        <td class="num">${esc(d.amount)}</td>
        <td>${esc(d.currency)}</td>
        <td class="mono">${esc(d.event_time || "")}</td>
        <td class="mono">${esc(d.settlement_date || "")}</td>
        <td>${d.line_type === "summary" ? "—" : (d.included ? "计入" : "未计入")}</td>
        <td>${esc(d.note || "")}</td>
        <td class="mono">${esc(d.source_line || "")}</td>
        <td>${trace}</td>
      </tr>`;
    }).join("");

    const versions = order.versions.map((v) => `
      <div class="version-item ${v.current ? "current" : ""}">
        <strong>v${v.version}</strong>
        ${statusBadge(v.status, v.status_label)}
        <span class="mono">净实收 ${esc(v.net_received)} · 差额 ${esc(v.difference)}</span>
        <span class="note">${esc(v.change_note || "")}</span>
        <span class="muted mono">${esc(v.created_at || "")}</span>
        <button class="ghost" data-action="version" data-order="${esc(order.order_id)}" data-version="${v.version}" ${v.current ? "disabled" : ""}>${v.current ? "当前" : "查看此版"}</button>
      </div>`).join("");

    panel.innerHTML = `
      ${formula}
      <div class="detail-grid">
        <div class="subtable">
          <table>
            <thead><tr>
              <th>类型</th><th>原始记录引用</th><th>流水号 / 原支付</th><th class="num">金额</th>
              <th>币种</th><th>北京时间</th><th>事件日</th><th>计入</th><th>说明</th><th>原始行</th><th></th>
            </tr></thead>
            <tbody>${detailRows}</tbody>
          </table>
        </div>
        <div class="version-box">
          <h4>版本历史（旧版本与修订原因均保留）</h4>
          <div class="version-list">${versions}</div>
        </div>
      </div>
      <div class="hint" style="margin-top:10px">
        每笔支付/退款明细均带有原始 CSV 行，点击“溯源”可查看流水号关联的支付、退款与冲突记录。
        版本号 v${order.version} 为当前结果。导出单笔明细：
        <a class="button-link" href="/api/export/orders/${encodeURIComponent(order.order_id)}.csv?version=${order.version}">订单 ${esc(order.order_id)} 明细 CSV</a>
      </div>`;

    panel.onclick = async (event) => {
      const traceBtn = event.target.closest('[data-action="trace"]');
      if (traceBtn) {
        event.stopPropagation();
        return showTrace(traceBtn.dataset.txn);
      }
      const verBtn = event.target.closest('[data-action="version"]');
      if (verBtn) {
        event.stopPropagation();
        try {
          const version = Number(verBtn.dataset.version);
          const key = `${order.order_id}:${version}`;
          let prev = state.orderCache.get(key);
          if (!prev) {
            prev = (await api(`/api/orders/${encodeURIComponent(order.order_id)}?version=${version}`)).order;
            state.orderCache.set(key, prev);
          }
          renderOrderDetail(panel, prev, tr);
        } catch (err) {
          toast(`加载历史版本失败：${err.message}`, true);
        }
      }
    };
  }

  // ------------------------------------------------------------ 溯源
  async function showTrace(txnId) {
    try {
      const data = await api(`/api/trace?txn_id=${encodeURIComponent(txnId)}`);
      $("#modalTitle").textContent = `流水溯源 · ${txnId}`;
      const parts = [];
      parts.push("<h4>支付事件</h4>");
      if (data.payment) {
        parts.push(`<div class="table-wrap"><table><thead><tr>
          <th>订单号</th><th class="num">金额</th><th>币种</th><th>北京时间</th><th>原始行</th></tr></thead>
          <tbody><tr><td class="mono">${esc(data.payment.order_id)}</td>
          <td class="num">${esc(data.payment.amount)}</td><td>${esc(data.payment.currency)}</td>
          <td class="mono">${esc(data.payment.event_time)}</td>
          <td class="mono">${esc(data.payment.source_line || "")}</td></tr></tbody></table></div>`);
      } else {
        parts.push('<p class="muted">没有该流水号的支付事件（可能是退款引用了不存在或已冲突的流水号）。</p>');
      }
      parts.push("<h4 style='margin-top:16px'>引用该流水号的退款</h4>");
      if (data.refunds.length) {
        parts.push(`<div class="table-wrap"><table><thead><tr>
          <th>退款 ID</th><th class="num">金额</th><th>币种</th><th>北京时间</th><th>原始行</th></tr></thead><tbody>
          ${data.refunds.map((r) => `<tr><td class="mono">refund:${esc(r.id)}</td>
            <td class="num">${esc(r.amount)}</td><td>${esc(r.currency)}</td>
            <td class="mono">${esc(r.event_time)}</td>
            <td class="mono">${esc(r.source_line || "")}</td></tr>`).join("")}
          </tbody></table></div>`);
      } else {
        parts.push('<p class="muted">无退款记录。</p>');
      }
      if (data.conflicts.length) {
        parts.push("<h4 style='margin-top:16px'>同号冲突</h4>");
        parts.push(`<div class="table-wrap"><table><thead><tr>
          <th>批次</th><th>首次事件</th><th>冲突事件</th><th>原因</th></tr></thead><tbody>
          ${data.conflicts.map((c) => `<tr><td class="mono">${esc(c.batch_id)}</td>
            <td class="mono">${esc(c.canonical || "")}</td><td class="mono">${esc(c.duplicate)}</td>
            <td>${esc(c.reason)}</td></tr>`).join("")}
          </tbody></table></div>`);
      }
      if (data.details.length) {
        parts.push("<h4 style='margin-top:16px'>当前对账结果中的明细引用</h4>");
        parts.push(`<div class="table-wrap"><table><thead><tr>
          <th>订单号</th><th>版本</th><th>类型</th><th>原始引用</th><th class="num">金额</th><th>币种</th><th>计入</th><th>说明</th></tr></thead><tbody>
          ${data.details.map((d) => `<tr><td class="mono">${esc(d.order_id)}</td>
            <td>v${esc(d.version)}</td><td>${esc(d.line_type)}</td>
            <td class="mono">${esc(d.event_ref || "")}</td><td class="num">${esc(d.amount)}</td>
            <td>${esc(d.currency)}</td><td>${d.included ? "计入" : "未计入"}</td>
            <td>${esc(d.note || "")}</td></tr>`).join("")}
          </tbody></table></div>`);
      }
      $("#modalBody").innerHTML = parts.join("");
      $("#modal").classList.remove("hidden");
    } catch (err) {
      toast(`溯源失败：${err.message}`, true);
    }
  }

  // ------------------------------------------------------------ 结算日汇总
  async function loadDaily() {
    try {
      const date = $("#dDate").value;
      const data = await api(`/api/daily${date ? `?date=${encodeURIComponent(date)}` : ""}`);
      renderDaily(data.daily);
      $("#exportDaily").href = `/api/export/daily.csv${date ? `?date=${encodeURIComponent(date)}` : ""}`;
      state.loaded.daily = true;
    } catch (err) {
      toast(`加载结算日汇总失败：${err.message}`, true);
    }
  }

  function renderDaily(rows) {
    const tbody = $("#dailyBody");
    const empty = $("#dailyEmpty");
    if (!rows.length) {
      tbody.innerHTML = "";
      empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden");
    tbody.innerHTML = rows.map((r, idx) => `
      <tr class="clickable" data-daily="${idx}">
        <td class="mono">${esc(r.settlement_date)}</td>
        <td>${esc(r.currency)}</td>
        <td class="num">v${r.version}</td>
        <td class="num">${r.order_count}</td>
        <td class="num">${r.match_count}</td>
        <td class="num">${r.mismatch_count}</td>
        <td class="num">${r.exception_count}</td>
        <td class="num">${esc(r.receivable_total)}</td>
        <td class="num">${esc(r.net_total)}</td>
        <td class="num">${amountCell(r.adjustment_total)}</td>
        <td class="muted">${esc(r.change_note || "")}</td>
      </tr>`).join("");
    tbody.onclick = (event) => {
      const tr = event.target.closest("tr[data-daily]");
      if (!tr) return;
      const next = tr.nextElementSibling;
      if (next && next.classList.contains("detail-row")) { next.remove(); return; }
      const r = rows[Number(tr.dataset.daily)];
      const detail = document.createElement("tr");
      detail.className = "detail-row";
      const adjustments = r.adjustments && r.adjustments.length
        ? `<div class="subtable" style="margin-bottom:12px"><table><thead><tr>
            <th>订单号</th><th>原始引用</th><th class="num">调整金额</th></tr></thead><tbody>
            ${r.adjustments.map((a) => `<tr><td class="mono">${esc(a.order_id)}</td>
              <td class="mono">${esc(a.event_ref)}</td><td class="num">${amountCell(a.amount)}</td></tr>`).join("")}
            </tbody></table></div>`
        : '<p class="muted">该结算日没有跨日退款调整。</p>';
      const versions = r.versions.map((v) => `
        <div class="version-item ${v.current ? "current" : ""}">
          <strong>v${v.version}</strong>
          <span class="mono">净实收 ${esc(v.net_total)} · 调整 ${esc(v.adjustment_total)} · 订单 ${v.order_count}</span>
          <span class="note">${esc(v.change_note || "")}</span>
          <span class="muted mono">${esc(v.created_at || "")}</span>
        </div>`).join("");
      detail.innerHTML = `<td colspan="11"><div class="detail-panel">
        <h4 style="margin:0 0 8px;font-size:13px">跨日退款调整明细</h4>
        ${adjustments}
        <h4 style="margin:14px 0 8px;font-size:13px">版本历史（保留上一版与差异原因）</h4>
        <div class="version-list">${versions}</div>
      </div></td>`;
      tr.after(detail);
      tr.classList.add("expanded");
    };
  }

  // ------------------------------------------------------------ 异常
  async function loadExceptions() {
    try {
      const data = await api("/api/exceptions");
      const t = (bodyId, rows, mapper, emptyText) => {
        $(`#${bodyId}`).innerHTML = rows.length
          ? rows.map(mapper).join("")
          : `<tr><td colspan="8" class="muted">${emptyText}</td></tr>`;
      };
      t("standaloneBody", data.standalone, (r) => `<tr>
        <td>${r.exception_type === "ORPHAN_REFUND" ? "孤儿退款" : "孤儿支付"}</td>
        <td class="mono">${r.ref_type === "refund" ? `refund:${r.ref_id}` : r.ref_id}</td>
        <td class="mono">${esc(r.order_id || "—")}</td>
        <td class="num">${esc(r.amount || "")}</td><td>${esc(r.currency || "")}</td>
        <td class="mono">${esc(r.event_time || "")}</td><td>${esc(r.reason)}</td>
        <td class="mono">${esc(r.source_line || "")}</td></tr>`, "暂无挂账异常");
      t("conflictBody", data.event_conflicts, (r) => `<tr>
        <td class="mono">${esc(r.txn_id)}</td><td>${esc(r.event_type)}</td>
        <td class="mono">${esc(r.batch_id)}</td>
        <td class="mono">${esc(r.canonical || "")}</td>
        <td class="mono">${esc(r.duplicate)}</td><td>${esc(r.reason)}</td></tr>`,
        "暂无同号冲突（内容相同的重复事件已静默去重）");
      t("orderConflictBody", data.order_conflicts, (r) => `<tr>
        <td class="mono">${esc(r.order_id)}</td><td class="mono">${esc(r.batch_id)}</td>
        <td class="mono">${esc(r.payload)}</td><td>${esc(r.reason)}</td></tr>`, "暂无订单冲突");
      t("rowErrorBody", data.row_errors, (r) => `<tr>
        <td>${esc(r.file_name)} <span class="muted">(${esc(r.file_type)})</span></td>
        <td class="num">${r.row_number}</td>
        <td class="mono">${esc(r.raw_line)}</td><td class="diff neg">${esc(r.error)}</td></tr>`,
        "暂无行级导入错误");
      state.loaded.exceptions = true;
    } catch (err) {
      toast(`加载异常失败：${err.message}`, true);
    }
  }

  // ------------------------------------------------------------ 导入
  async function loadImports() {
    try {
      const data = await api("/api/imports");
      $("#batchBody").innerHTML = data.imports.length
        ? data.imports.map((r) => `<tr>
            <td class="num">#${r.id}</td>
            <td class="mono">${esc(r.import_time)}</td>
            <td>${esc({ order: "订单", payment: "支付", refund: "退款" }[r.file_type] || r.file_type)}</td>
            <td class="mono">${esc(r.file_name)}</td>
            <td class="num">${r.row_count}</td>
            <td>${esc(r.status)}</td>
            <td class="mono muted" title="${esc(r.file_sha256)}">${esc(r.file_sha256.slice(0, 12))}…</td>
          </tr>`).join("")
        : '<tr><td colspan="7" class="muted">暂无导入批次</td></tr>';
      state.loaded.imports = true;
    } catch (err) {
      toast(`加载导入记录失败：${err.message}`, true);
    }
  }

  async function handleImport(event) {
    event.preventDefault();
    const file = $("#importFile").files[0];
    if (!file) return;
    const type = $("#importType").value;
    const form = new FormData();
    form.append("file", file, file.name);
    const btn = $("#importBtn");
    const box = $("#importMessage");
    btn.disabled = true;
    box.classList.add("hidden");
    try {
      const data = await api(`/api/import?type=${encodeURIComponent(type)}`, { method: "POST", body: form });
      const rep = data.recompute;
      box.classList.remove("hidden", "error");
      box.textContent =
        `${data.import.message}\n重算：订单新增 ${rep.orders_new}、修订 ${rep.orders_versioned}、未变 ${rep.orders_unchanged}；` +
        `结算日新增 ${rep.daily_new}、修订 ${rep.daily_versioned}；挂账异常 ${rep.standalone_exceptions}。`;
      $("#importFile").value = "";
      state.orderCache.clear();
      state.loaded = { daily: false, exceptions: false, imports: false };
      await Promise.all([loadResults(), loadImports()]);
      if (state.tab === "exceptions") await loadExceptions();
      if (state.tab === "daily") await loadDaily();
      toast("导入并重算完成");
    } catch (err) {
      box.classList.remove("hidden");
      box.classList.add("error");
      box.textContent = `导入失败：${err.message}`;
      toast(`导入失败：${err.message}`, true);
    } finally {
      btn.disabled = false;
    }
  }

  async function handleRecompute() {
    const btn = $("#recomputeBtn");
    btn.disabled = true;
    try {
      const data = await api("/api/recompute", { method: "POST" });
      const r = data.recompute;
      state.orderCache.clear();
      state.loaded = { daily: false, exceptions: false, imports: false };
      await Promise.all([loadResults(), loadImports()]);
      if (state.tab === "daily") await loadDaily();
      if (state.tab === "exceptions") await loadExceptions();
      toast(
        `重算完成：订单新增 ${r.orders_new}、修订 ${r.orders_versioned}、未变 ${r.orders_unchanged}；` +
        `结算日新增 ${r.daily_new}、修订 ${r.daily_versioned}。`
      );
    } catch (err) {
      toast(`重算失败：${err.message}`, true);
    } finally {
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------ 初始化
  function switchTab(tab) {
    state.tab = tab;
    $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    $$(".panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${tab}`));
    if (tab === "daily" && !state.loaded.daily) loadDaily();
    if (tab === "exceptions" && !state.loaded.exceptions) loadExceptions();
    if (tab === "imports" && !state.loaded.imports) loadImports();
  }

  async function init() {
    try {
      state.config = await api("/api/config");
    } catch (err) {
      toast(`加载配置失败：${err.message}`, true);
      return;
    }
    const currSel = $("#fCurrency");
    Object.keys(state.config.currency_scales).sort().forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c; opt.textContent = c;
      currSel.appendChild(opt);
    });
    const reasonSel = $("#fReason");
    Object.entries(state.config.reason_messages).forEach(([code, message]) => {
      const opt = document.createElement("option");
      opt.value = code;
      opt.textContent = message.split("：")[0];
      reasonSel.appendChild(opt);
    });

    $$(".tab").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

    let timer = null;
    const debounced = () => {
      clearTimeout(timer);
      timer = setTimeout(loadResults, 250);
    };
    ["#fDate", "#fStatus", "#fCurrency", "#fReason"].forEach((sel) =>
      $(sel).addEventListener("change", loadResults));
    $("#fQ").addEventListener("input", debounced);
    $("#clearFilters").addEventListener("click", () => {
      $("#fDate").value = ""; $("#fStatus").value = "";
      $("#fCurrency").value = ""; $("#fReason").value = ""; $("#fQ").value = "";
      loadResults();
    });

    $("#resultsBody").addEventListener("click", (event) => {
      const tr = event.target.closest("tr.clickable[data-order]");
      if (!tr) return;
      if (event.target.closest(".detail-row")) return;
      toggleOrderRow(tr);
    });

    $("#loadDaily").addEventListener("click", loadDaily);
    $("#dDate").addEventListener("change", loadDaily);
    $("#recomputeBtn").addEventListener("click", handleRecompute);
    $("#importForm").addEventListener("submit", handleImport);

    $("#modalClose").addEventListener("click", () => $("#modal").classList.add("hidden"));
    $("#modal").addEventListener("click", (event) => {
      if (event.target.id === "modal") $("#modal").classList.add("hidden");
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") $("#modal").classList.add("hidden");
    });

    await loadResults();
  }

  init();
})();
