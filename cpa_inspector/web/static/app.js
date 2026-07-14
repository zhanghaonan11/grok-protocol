/* CPA Web 巡检台 workbench */
(() => {
  "use strict";

  const state = {
    connected: false,
    page: 1,
    totalPages: 0,
    total: 0,
    pageSize: 50,
    selected: new Set(),
    currentItems: [],
    profiles: [],
    importPreview: [],
    pathDropFiles: [],
    jobResults: [],
    probeResults: [],
    jobSelected: new Set(),
    activeJobKind: "",
    autoCleanup: {
      running: false,
      stopRequested: false,
      round: 0,
      timer: null,
    },
    busy: false,
    pollTimer: null,
  };

  const $ = (id) => document.getElementById(id);

  function toast(message, type = "") {
    const el = $("toast");
    el.hidden = false;
    el.textContent = message;
    el.className = "toast" + (type ? ` ${type}` : "");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => {
      el.hidden = true;
    }, 3200);
  }

  function setConnectMessage(text, type = "") {
    const el = $("connect-message");
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      el.className = "message";
      return;
    }
    el.hidden = false;
    el.textContent = text;
    el.className = "message" + (type ? ` ${type}` : "");
  }

  function setConnected(connected, label) {
    state.connected = !!connected;
    const pill = $("connection-status");
    pill.dataset.connected = state.connected ? "1" : "0";
    pill.className = "status-pill " + (state.connected ? "status-on" : "status-off");
    pill.textContent = label || (state.connected ? "已连接" : "未连接");
    const writeIds = [
      "btn-refresh",
      "btn-import",
      "btn-import-path",
      "btn-import-paste",
      "btn-export",
      "btn-export-delete",
      "btn-health",
      "btn-health-page",
      "btn-health-filtered",
      "btn-health-all",
    ];
    for (const id of writeIds) {
      $(id).disabled = !state.connected || state.busy;
    }
  }

  function setBusy(busy, label) {
    state.busy = !!busy;
    setConnected(state.connected, $("connection-status").textContent);
    if (busy) {
      $("connection-status").classList.add("status-busy");
      if (label) $("connection-status").textContent = label;
    }
  }

  async function api(path, options = {}) {
    const opts = { ...options };
    opts.headers = { ...(options.headers || {}) };
    if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== "string") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const resp = await fetch(path, opts);
    const contentType = resp.headers.get("content-type") || "";
    let data = null;
    if (contentType.includes("application/json")) {
      data = await resp.json();
    } else if (contentType.includes("application/zip") || contentType.includes("octet-stream")) {
      data = await resp.blob();
    } else {
      data = await resp.text();
    }
    if (!resp.ok) {
      let detail = "";
      if (data && typeof data === "object" && data.detail) {
        detail = Array.isArray(data.detail)
          ? data.detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
          : String(data.detail);
      } else if (typeof data === "string" && data) {
        detail = data;
      } else {
        detail = `请求失败 (${resp.status})`;
      }
      const err = new Error(detail);
      err.status = resp.status;
      err.response = resp;
      err.data = data;
      throw err;
    }
    return { data, resp };
  }

  async function loadSettings() {
    const { data } = await api("/api/cpa/settings");
    $("setting-workers").value = data.max_parallel_workers ?? 4;
    $("setting-page-size").value = String(data.page_size ?? 50);
    $("setting-probe-model").value = data.probe_model || "gpt-5";
    $("setting-probe-timeout").value = data.probe_timeout_seconds ?? 15;
    $("setting-probe-workers").value = data.probe_max_workers ?? 0;
    $("setting-import-refresh").checked = data.import_refresh_tokens !== false;
    $("setting-import-refresh-timeout").value = data.import_refresh_timeout_seconds ?? 20;
    $("filter-page-size").value = String(data.page_size ?? 50);
    state.pageSize = Number(data.page_size || 50);
    // 本次并发默认跟随设置；仅当输入框仍是初始 0 时回填
    if (!$("probe-workers-once").dataset.touched) {
      $("probe-workers-once").value = data.probe_max_workers ?? 0;
    }
    applyAutoCleanupSettings(data);
  }

  function applyAutoCleanupSettings(data) {
    if ($("auto-cleanup-scope")) {
      $("auto-cleanup-scope").value = data.auto_cleanup_scope || "all";
    }
    if ($("auto-cleanup-match")) {
      $("auto-cleanup-match").value = data.auto_cleanup_match || "failed";
    }
    if ($("auto-cleanup-keyword")) {
      $("auto-cleanup-keyword").value =
        data.auto_cleanup_keyword ||
        "invalid_grant,revoked,bad-credentials,凭证无效";
    }
    if ($("auto-cleanup-interval")) {
      $("auto-cleanup-interval").value = data.auto_cleanup_interval_seconds ?? 0;
    }
    if ($("auto-cleanup-max-rounds")) {
      $("auto-cleanup-max-rounds").value = data.auto_cleanup_max_rounds ?? 0;
    }
  }

  function collectAutoCleanupSettings() {
    return {
      auto_cleanup_scope: $("auto-cleanup-scope")?.value || "all",
      auto_cleanup_match: $("auto-cleanup-match")?.value || "failed",
      auto_cleanup_keyword:
        ($("auto-cleanup-keyword")?.value || "").trim() ||
        "invalid_grant,revoked,bad-credentials,凭证无效",
      auto_cleanup_interval_seconds: Number($("auto-cleanup-interval")?.value || 0),
      auto_cleanup_max_rounds: Number($("auto-cleanup-max-rounds")?.value || 0),
    };
  }

  async function saveSettings(extra = {}) {
    const payload = {
      max_parallel_workers: Number($("setting-workers").value || 4),
      page_size: Number($("setting-page-size").value || 50),
      probe_model: $("setting-probe-model").value.trim() || "gpt-5",
      probe_timeout_seconds: Number($("setting-probe-timeout").value || 15),
      probe_max_workers: Number($("setting-probe-workers").value || 0),
      import_refresh_tokens: !!$("setting-import-refresh").checked,
      import_refresh_timeout_seconds: Number($("setting-import-refresh-timeout").value || 20),
      ...collectAutoCleanupSettings(),
      ...extra,
    };
    const { data } = await api("/api/cpa/settings", { method: "PUT", body: payload });
    $("filter-page-size").value = String(data.page_size);
    state.pageSize = data.page_size;
    $("setting-probe-workers").value = data.probe_max_workers ?? 0;
    $("setting-import-refresh").checked = data.import_refresh_tokens !== false;
    $("setting-import-refresh-timeout").value = data.import_refresh_timeout_seconds ?? 20;
    if (!$("probe-workers-once").dataset.touched) {
      $("probe-workers-once").value = data.probe_max_workers ?? 0;
    }
    applyAutoCleanupSettings(data);
    if (!extra.silent) toast("设置已保存", "ok");
    return data;
  }

  function fillProfileSelect(profiles) {
    state.profiles = profiles || [];
    const sel = $("profile-select");
    const current = sel.value;
    sel.innerHTML = '<option value="">选择配置…</option>';
    for (const p of state.profiles) {
      const opt = document.createElement("option");
      opt.value = p.name;
      opt.textContent = `${p.name} (${p.base_url})`;
      sel.appendChild(opt);
    }
    if (current) sel.value = current;
  }

  async function loadProfiles() {
    const { data } = await api("/api/cpa/profiles");
    const profiles = data.profiles || [];
    fillProfileSelect(profiles);
    if (profiles.length) {
      const sorted = [...profiles].sort((a, b) =>
        String(b.last_used_at || "").localeCompare(String(a.last_used_at || ""))
      );
      applyProfile(sorted[0]);
      $("profile-select").value = sorted[0].name;
    }
  }

  function applyProfile(profile) {
    if (!profile) return;
    $("profile-name").value = profile.name || "default";
    $("base-url").value = profile.base_url || "";
    $("secret-key").value = profile.secret_key || "";
  }

  async function saveProfile() {
    const name = $("profile-name").value.trim() || "default";
    const base_url = $("base-url").value.trim();
    const secret_key = $("secret-key").value;
    if (!base_url) {
      toast("请填写 Base URL", "error");
      return;
    }
    const map = new Map(state.profiles.map((p) => [p.name, { ...p }]));
    map.set(name, {
      name,
      base_url,
      secret_key,
      last_used_at: map.get(name)?.last_used_at || "",
    });
    const profiles = Array.from(map.values());
    const { data } = await api("/api/cpa/profiles", {
      method: "PUT",
      body: { profiles },
    });
    fillProfileSelect(data.profiles || profiles);
    $("profile-select").value = name;
    toast("配置已保存", "ok");
  }

  async function connect() {
    const payload = {
      name: $("profile-name").value.trim() || "default",
      base_url: $("base-url").value.trim(),
      secret_key: $("secret-key").value,
    };
    if (!payload.base_url) {
      setConnectMessage("请填写 Base URL", "error");
      return;
    }
    setBusy(true, "连接中…");
    setConnectMessage("正在连接…");
    try {
      const { data } = await api("/api/cpa/connect", { method: "POST", body: payload });
      setConnected(true, `已连接 · ${data.total ?? 0} 条`);
      setConnectMessage(`连接成功，共 ${data.total ?? 0} 条凭证`, "ok");
      // 连接成功后把配置写回，方便下次打开
      try {
        await saveProfileQuiet(payload);
      } catch (_) {
        /* ignore */
      }
      state.page = 1;
      state.selected.clear();
      await loadCredentials();
    } catch (err) {
      setConnected(false, "未连接");
      setConnectMessage(err.message || "连接失败", "error");
    } finally {
      setBusy(false);
    }
  }

  async function saveProfileQuiet(payload) {
    const map = new Map(state.profiles.map((p) => [p.name, { ...p }]));
    map.set(payload.name, {
      name: payload.name,
      base_url: payload.base_url,
      secret_key: payload.secret_key,
      last_used_at: new Date().toISOString(),
    });
    const profiles = Array.from(map.values());
    const { data } = await api("/api/cpa/profiles", {
      method: "PUT",
      body: { profiles },
    });
    fillProfileSelect(data.profiles || profiles);
  }

  async function refreshList() {
    if (!state.connected) return;
    setBusy(true, "刷新中…");
    try {
      const { data } = await api("/api/cpa/refresh", { method: "POST" });
      setConnected(true, `已连接 · ${data.total ?? 0} 条`);
      toast(`已刷新，共 ${data.total ?? 0} 条`, "ok");
      await loadCredentials();
    } catch (err) {
      toast(err.message || "刷新失败", "error");
      if (err.status === 400) setConnected(false);
    } finally {
      setBusy(false);
    }
  }

  function filterQuery() {
    const params = new URLSearchParams();
    params.set("page", String(state.page));
    params.set("page_size", String($("filter-page-size").value || state.pageSize || 50));
    params.set("search_text", $("filter-search").value.trim());
    params.set("status", $("filter-status").value || "全部");
    params.set("provider", $("filter-provider").value || "全部");
    params.set("exportable", $("filter-exportable").value || "全部");
    params.set("health", $("filter-health").value || "全部");
    return params;
  }

  function healthClass(display) {
    if (display === "健康") return "ok";
    if (display === "失败") return "fail";
    if (display === "不确定") return "warn";
    return "muted";
  }

  function statusClass(display) {
    if (display === "活跃") return "ok";
    if (display === "已停用") return "fail";
    if (display === "暂不可用") return "warn";
    return "muted";
  }

  function updateSelectionSummary() {
    $("selection-summary").textContent = `已选 ${state.selected.size}`;
  }

  function syncCheckAll() {
    const boxes = Array.from(document.querySelectorAll(".row-check"));
    const all = boxes.length > 0 && boxes.every((b) => b.checked);
    $("check-all").checked = all;
  }

  function renderTable(items) {
    const body = $("credentials-body");
    body.innerHTML = "";
    state.currentItems = items || [];
    if (!items || !items.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="9">无匹配凭证</td></tr>';
      $("check-all").checked = false;
      return;
    }
    const frag = document.createDocumentFragment();
    for (const item of items) {
      const tr = document.createElement("tr");
      tr.dataset.name = item.name;
      if (state.selected.has(item.name)) tr.classList.add("selected");
      tr.innerHTML = `
        <td class="col-check"><input type="checkbox" class="row-check" data-name="${escapeAttr(item.name)}" ${state.selected.has(item.name) ? "checked" : ""} /></td>
        <td title="${escapeAttr(item.name)}">${escapeHtml(item.name)}</td>
        <td>${escapeHtml(item.provider || "-")}</td>
        <td><span class="tag ${statusClass(item.status_display)}">${escapeHtml(item.status_display || "-")}</span></td>
        <td>${escapeHtml(item.email_masked || "-")}</td>
        <td>${item.can_export ? "是" : "否"}</td>
        <td><span class="tag ${healthClass(item.health_display)}">${escapeHtml(item.health_display || "未测")}</span></td>
        <td>${escapeHtml(item.updated_at || "-")}</td>
        <td>${escapeHtml(item.last_refresh || "-")}</td>
      `;
      frag.appendChild(tr);
    }
    body.appendChild(frag);
    syncCheckAll();
  }

  function renderPagination() {
    $("page-info").textContent = `第 ${state.totalPages ? state.page : 0} / ${state.totalPages} 页`;
    $("page-prev").disabled = state.page <= 1 || state.totalPages === 0;
    $("page-next").disabled = state.page >= state.totalPages || state.totalPages === 0;
    $("list-summary").textContent = state.connected
      ? `共 ${state.total} 条 · 本页 ${state.currentItems.length} 条`
      : "未加载";
  }

  function mergeProviders(items) {
    const sel = $("filter-provider");
    const current = sel.value || "全部";
    const known = new Set(
      Array.from(sel.options)
        .map((o) => o.value)
        .filter((v) => v && v !== "全部")
    );
    for (const item of items || []) {
      if (item.provider) known.add(item.provider);
    }
    const values = Array.from(known).sort((a, b) => a.localeCompare(b));
    sel.innerHTML = '<option value="全部">全部</option>';
    for (const v of values) {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      sel.appendChild(opt);
    }
    sel.value = values.includes(current) || current === "全部" ? current : "全部";
  }

  async function loadCredentials() {
    if (!state.connected) return;
    try {
      const { data } = await api(`/api/cpa/credentials?${filterQuery().toString()}`);
      state.page = data.page || 1;
      state.totalPages = data.total_pages || 0;
      state.total = data.total || 0;
      state.pageSize = data.page_size || state.pageSize;
      // 清理不在全集选择集的项会在用户操作时自然处理；这里保留跨页选择
      renderTable(data.items || []);
      mergeProviders(data.items || []);
      renderPagination();
      updateSelectionSummary();
      setConnected(true, `已连接 · ${state.total} 条`);
    } catch (err) {
      toast(err.message || "加载列表失败", "error");
      if (err.status === 400) {
        setConnected(false);
        renderTable([]);
        state.total = 0;
        state.totalPages = 0;
        renderPagination();
      }
    }
  }

  async function showDetail(name) {
    if (!name || !state.connected) return;
    try {
      const { data } = await api(`/api/cpa/credentials/detail?name=${encodeURIComponent(name)}`);
      $("detail-empty").hidden = true;
      const dl = $("detail-content");
      dl.hidden = false;
      const rows = [
        ["名称", data.name],
        ["Provider", data.provider],
        ["状态", data.status_display],
        ["邮箱", data.email_masked],
        ["账号", data.account || "-"],
        ["可导出", data.can_export ? "是" : "否"],
        ["健康", data.health_display],
        ["健康详情", data.health_detail || "-"],
        ["探测时间", data.health_checked_at || "-"],
        ["来源", data.source || "-"],
        ["优先级", data.priority ?? "-"],
        ["备注", data.note || "-"],
        ["代理", data.proxy_url || "-"],
        ["更新时间", data.updated_at || "-"],
        ["上次刷新", data.last_refresh || "-"],
        ["创建时间", data.created_at || "-"],
      ];
      dl.innerHTML = rows
        .map(
          ([k, v]) =>
            `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v ?? "-"))}</dd>`
        )
        .join("");
    } catch (err) {
      toast(err.message || "加载详情失败", "error");
    }
  }

  function selectedNames() {
    return Array.from(state.selected);
  }

  function requireSelection() {
    const names = selectedNames();
    if (!names.length) {
      toast("请先勾选凭证", "error");
      return null;
    }
    return names;
  }

  function currentPageNames() {
    return (state.currentItems || []).map((item) => item.name).filter(Boolean);
  }

  async function fetchFilteredNames({ ignoreFilters = false } = {}) {
    // 分页拉全量名称；ignoreFilters=true 时遍历号池全部。
    const params = ignoreFilters
      ? new URLSearchParams({
          page: "1",
          page_size: "100",
          search_text: "",
          status: "全部",
          provider: "全部",
          exportable: "全部",
          health: "全部",
        })
      : filterQuery();
    params.set("page", "1");
    params.set("page_size", "100");
    const first = await api(`/api/cpa/credentials?${params.toString()}`);
    const total = Number(first.data.total || 0);
    const pageSize = 100;
    let names = (first.data.items || []).map((x) => x.name).filter(Boolean);
    const totalPages = Math.max(1, Math.ceil(total / pageSize) || 1);
    if (totalPages > 1) {
      for (let page = 2; page <= totalPages; page += 1) {
        params.set("page", String(page));
        const more = await api(`/api/cpa/credentials?${params.toString()}`);
        names = names.concat((more.data.items || []).map((x) => x.name).filter(Boolean));
      }
    }
    const seen = new Set();
    return names.filter((name) => {
      if (seen.has(name)) return false;
      seen.add(name);
      return true;
    });
  }

  function onceProbeWorkers() {
    const raw = Number($("probe-workers-once").value || 0);
    if (!Number.isFinite(raw) || raw <= 0) return 0;
    return Math.max(1, Math.min(32, Math.floor(raw)));
  }

  async function resolveProbeNames(mode, options = {}) {
    const confirmLarge = options.confirmLarge !== false;
    if (mode === "selected") {
      return requireSelection();
    }
    if (mode === "page") {
      const names = currentPageNames();
      if (!names.length) {
        toast("当前页没有可探测凭证", "error");
        return null;
      }
      return names;
    }
    if (mode === "filtered" || mode === "all") {
      const ignoreFilters = mode === "all";
      setBusy(true, ignoreFilters ? "收集全部凭证…" : "收集筛选结果…");
      try {
        const names = await fetchFilteredNames({ ignoreFilters });
        if (!names.length) {
          toast(ignoreFilters ? "号池为空" : "当前筛选结果为空", "error");
          return null;
        }
        const label = ignoreFilters ? "全部凭证" : "筛选结果";
        if (
          confirmLarge &&
          !window.confirm(
            `将探测${label}共 ${names.length} 条（并发 ${onceProbeWorkers() || "跟随设置"}），确认继续？`
          )
        ) {
          return null;
        }
        return names;
      } catch (err) {
        toast(err.message || "获取凭证列表失败", "error");
        return null;
      } finally {
        setBusy(false);
      }
    }
    toast("未知探测范围", "error");
    return null;
  }

  function resultClass(result) {
    if (result === "成功" || result === "部分成功") return "ok";
    if (result === "跳过" || result === "不确定") return result === "跳过" ? "muted" : "warn";
    return "fail";
  }

  function isBadJobResult(result) {
    const text = String(result || "");
    return text === "失败" || text === "不确定";
  }

  function filteredJobResults() {
    const keyword = ($("job-filter-text")?.value || "").trim().toLowerCase();
    const resultFilter = $("job-filter-result")?.value || "全部";
    const badOnly = !!$("job-filter-bad-only")?.checked;
    return (state.jobResults || []).filter((item) => {
      const result = String(item.result || "");
      const name = String(item.name || "");
      const detail = String(item.detail || "");
      if (resultFilter !== "全部" && result !== resultFilter) return false;
      if (badOnly && !isBadJobResult(result)) return false;
      if (keyword) {
        const hay = `${name} ${detail} ${result}`.toLowerCase();
        if (!hay.includes(keyword)) return false;
      }
      return true;
    });
  }

  function updateJobSelectionSummary() {
    const el = $("job-selected-summary");
    if (el) el.textContent = `已选 ${state.jobSelected.size}`;
  }

  function syncJobCheckAll(visible) {
    const box = $("job-check-all");
    if (!box) return;
    const names = visible.map((item) => item.name).filter(Boolean);
    box.checked = names.length > 0 && names.every((name) => state.jobSelected.has(name));
  }

  function renderJobResultsTable() {
    const body = $("job-results-body");
    if (!body) return;
    const visible = filteredJobResults();
    const summary = $("job-filter-summary");
    if (summary) {
      summary.textContent = `共 ${state.jobResults.length} 条，筛选后 ${visible.length} 条，已选 ${state.jobSelected.size}`;
    }
    if (!state.jobResults.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="4">任务结果会显示在这里</td></tr>';
      syncJobCheckAll([]);
      updateJobSelectionSummary();
      return;
    }
    if (!visible.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="4">无匹配结果</td></tr>';
      syncJobCheckAll([]);
      updateJobSelectionSummary();
      return;
    }
    body.innerHTML = visible
      .map((r) => {
        const name = r.name || "-";
        const checked = state.jobSelected.has(name) ? "checked" : "";
        const cls = resultClass(r.result);
        return `<tr data-name="${escapeAttr(name)}">
          <td class="col-check"><input type="checkbox" class="job-row-check" data-name="${escapeAttr(name)}" ${checked} /></td>
          <td title="${escapeAttr(name)}">${escapeHtml(name)}</td>
          <td><span class="tag ${cls}">${escapeHtml(r.result || "-")}</span></td>
          <td class="detail-cell" title="${escapeAttr(r.detail || "")}">${escapeHtml(r.detail || "-")}</td>
        </tr>`;
      })
      .join("");
    syncJobCheckAll(visible);
    updateJobSelectionSummary();
  }

  function setJobUI({ status, current, total, message, results, replaceResults = true, kind = "" }) {
    $("job-status").textContent = status || "空闲";
    $("job-message").textContent = message || "";
    const pct = total ? Math.min(100, Math.round((Number(current || 0) / Number(total)) * 100)) : 0;
    $("job-progress-bar").style.width = `${pct}%`;

    if ($("job-wb-status")) $("job-wb-status").textContent = status || "空闲";
    if ($("job-wb-message")) $("job-wb-message").textContent = message || "";
    if ($("job-wb-progress")) {
      $("job-wb-progress").textContent = `${Number(current || 0)} / ${Number(total || 0)}`;
    }
    if ($("job-wb-progress-bar")) $("job-wb-progress-bar").style.width = `${pct}%`;

    if (Array.isArray(results) && replaceResults) {
      // 仅在需要替换结果集时更新（探测结果）；删除任务默认不覆盖探测列表
      state.jobResults = results.slice();
      if (kind === "probe") {
        state.probeResults = results.slice();
      }
      const valid = new Set(state.jobResults.map((r) => r.name).filter(Boolean));
      state.jobSelected = new Set(Array.from(state.jobSelected).filter((name) => valid.has(name)));
    }
    renderJobResultsTable();
  }

  function restoreProbeResultsView(extraMessage = "") {
    if (!state.probeResults.length) return;
    const remain = state.probeResults.length;
    setJobUI({
      status: "success",
      current: remain,
      total: remain,
      message: extraMessage || `已恢复探测结果（${remain} 条），可继续筛选删除`,
      results: state.probeResults,
      replaceResults: true,
      kind: "probe",
    });
  }

  function removeProbeResultsByNames(names) {
    const drop = new Set((names || []).filter(Boolean));
    if (!drop.size) return;
    state.probeResults = (state.probeResults || []).filter((item) => !drop.has(item.name));
    state.jobResults = (state.jobResults || []).filter((item) => !drop.has(item.name));
    state.jobSelected = new Set(Array.from(state.jobSelected).filter((name) => !drop.has(name)));
  }

  function openJobWorkbench() {
    $("job-workbench-modal").hidden = false;
    document.body.classList.add("job-workbench-open");
    // 打开时优先展示探测结果，方便多轮筛选删除
    if (state.probeResults.length && state.activeJobKind !== "delete-running") {
      state.jobResults = state.probeResults.slice();
    }
    renderJobResultsTable();
  }

  function closeJobWorkbench() {
    $("job-workbench-modal").hidden = true;
    document.body.classList.remove("job-workbench-open");
  }

  function selectedJobNames() {
    return Array.from(state.jobSelected);
  }

  async function deleteSelectedJobCredentials(options = {}) {
    const {
      names: overrideNames = null,
      auto = false,
      confirm = true,
    } = options;
    const names = Array.isArray(overrideNames) ? overrideNames.slice() : selectedJobNames();
    if (!names.length) {
      if (!auto) toast("请先在任务台勾选要删除的凭证", "error");
      return null;
    }
    if (!state.connected) {
      if (!auto) toast("请先连接 CPA", "error");
      return null;
    }
    if (
      confirm &&
      !window.confirm(
        `将删除线上 ${names.length} 条凭证，此操作不可恢复，确认继续？`
      )
    ) {
      return null;
    }

    // 先记住探测结果，删除过程不覆盖
    if (!state.probeResults.length && state.jobResults.length) {
      state.probeResults = state.jobResults.slice();
    }
    const deleting = names.slice();
    state.activeJobKind = "delete-running";
    setBusy(true, "删除中…");
    setJobUI({
      status: "queued",
      current: 0,
      total: deleting.length,
      message: `提交删除任务（${deleting.length} 条）… 探测结果已保留`,
      results: state.probeResults,
      replaceResults: false,
    });
    try {
      const { data } = await api("/api/cpa/credentials/delete", {
        method: "POST",
        body: { names: deleting },
      });
      const job = await pollJob(data.job_id, {
        preserveResults: true,
        onDone: async (doneJob) => {
          const deleted = (doneJob.results || [])
            .filter((r) => r.result === "成功")
            .map((r) => r.name)
            .filter(Boolean);
          const failed = (doneJob.results || []).filter((r) => r.result !== "成功").length;
          removeProbeResultsByNames(deleted);
          state.activeJobKind = "probe";
          restoreProbeResultsView(
            `删除完成：成功 ${deleted.length}，失败 ${failed}；剩余探测结果 ${state.probeResults.length} 条`
          );
          if (!auto) {
            toast(
              doneJob.status === "success" ? "删除完成，已保留未删探测结果" : "删除结束（有失败）",
              doneJob.status === "success" ? "ok" : "error"
            );
          }
          await loadCredentials();
        },
      });
      return job;
    } catch (err) {
      setBusy(false);
      state.activeJobKind = "probe";
      restoreProbeResultsView(`删除失败：${err.message || "未知错误"}；已恢复探测结果`);
      if (!auto) toast(err.message || "删除失败", "error");
      return null;
    }
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  async function pollJob(jobId, { onDone, preserveResults = false } = {}) {
    stopPolling();
    return await new Promise((resolve, reject) => {
      const tick = async () => {
        try {
          const { data } = await api(`/api/cpa/jobs/${encodeURIComponent(jobId)}`);
          setJobUI({
            status: data.status,
            current: data.current,
            total: data.total,
            message: data.message,
            results: data.results,
            // 删除任务只更新进度，不覆盖探测结果表
            replaceResults: !preserveResults,
          });
          if (data.status === "success" || data.status === "failed") {
            setBusy(false);
            if (onDone) await onDone(data);
            resolve(data);
            return;
          }
          state.pollTimer = setTimeout(tick, 800);
        } catch (err) {
          setBusy(false);
          toast(err.message || "任务查询失败", "error");
          reject(err);
        }
      };
      tick();
    });
  }

  async function runHealthCheck(mode = "selected", options = {}) {
    const {
      auto = false,
      openWorkbench = true,
      confirmLarge = true,
    } = options;
    const names = await resolveProbeNames(mode, { confirmLarge: confirmLarge && !auto });
    if (!names) return null;
    const modeLabel =
      mode === "page"
        ? "本页"
        : mode === "filtered"
          ? "筛选结果"
          : mode === "all"
            ? "全部"
            : "已选";
    const workers = onceProbeWorkers();
    const body = { names };
    if (workers > 0) body.max_workers = workers;
    setBusy(true, "探测中…");
    // 新探测开始时清空旧选择；旧探测结果将被本轮覆盖
    state.jobSelected.clear();
    state.activeJobKind = "probe";
    setJobUI({
      status: "queued",
      current: 0,
      total: names.length,
      message: `提交健康探测（${modeLabel} ${names.length} 条，并发 ${workers || "跟随设置"}）…`,
      results: [],
      replaceResults: true,
      kind: "probe",
    });
    try {
      const { data } = await api("/api/cpa/health-check", {
        method: "POST",
        body,
      });
      const job = await pollJob(data.job_id, {
        onDone: async (doneJob) => {
          state.probeResults = (doneJob.results || []).slice();
          state.jobResults = state.probeResults.slice();
          state.activeJobKind = "probe";
          if (!auto) {
            toast(
              doneJob.status === "success" ? "健康探测完成" : "健康探测结束（有失败）",
              doneJob.status === "success" ? "ok" : "error"
            );
          }
          if (openWorkbench) openJobWorkbench();
          await loadCredentials();
        },
      });
      return job;
    } catch (err) {
      setBusy(false);
      if (!auto) toast(err.message || "健康探测失败", "error");
      setJobUI({
        status: "failed",
        current: 0,
        total: names.length,
        message: err.message,
        results: [],
        replaceResults: true,
        kind: "probe",
      });
      return null;
    }
  }

  function parseKeywordRules(text) {
    return String(text || "")
      .split(/[,，\n]/)
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean);
  }

  function matchAutoCleanupTargets(results, rules) {
    const keywords = parseKeywordRules(rules.keyword);
    const allowUncertain = rules.match === "failed_uncertain";
    return (results || []).filter((item) => {
      const result = String(item.result || "");
      if (result === "失败") {
        // ok
      } else if (allowUncertain && result === "不确定") {
        // ok
      } else {
        return false;
      }
      if (!keywords.length) return true;
      const hay = `${item.name || ""} ${item.detail || ""} ${result}`.toLowerCase();
      return keywords.some((kw) => hay.includes(kw));
    });
  }

  function setAutoCleanupStatus(text) {
    if ($("auto-cleanup-status")) $("auto-cleanup-status").textContent = text;
    if ($("auto-cleanup-log")) $("auto-cleanup-log").textContent = text;
  }

  function setAutoCleanupRunning(running) {
    state.autoCleanup.running = !!running;
    if ($("btn-auto-cleanup-start")) $("btn-auto-cleanup-start").disabled = !!running;
    if ($("btn-auto-cleanup-stop")) $("btn-auto-cleanup-stop").disabled = !running;
    if ($("btn-auto-cleanup-save")) $("btn-auto-cleanup-save").disabled = !!running;
  }

  function stopAutoCleanup(reason = "已停止") {
    state.autoCleanup.stopRequested = true;
    if (state.autoCleanup.timer) {
      clearTimeout(state.autoCleanup.timer);
      state.autoCleanup.timer = null;
    }
    setAutoCleanupRunning(false);
    setAutoCleanupStatus(reason);
  }

  async function runAutoCleanupLoop() {
    const rules = {
      scope: $("auto-cleanup-scope")?.value || "all",
      match: $("auto-cleanup-match")?.value || "failed",
      keyword: $("auto-cleanup-keyword")?.value || "",
      interval: Math.max(0, Number($("auto-cleanup-interval")?.value || 0)),
      maxRounds: Math.max(0, Number($("auto-cleanup-max-rounds")?.value || 0)),
    };
    const probeMode = rules.scope === "filtered" ? "filtered" : "all";
    state.autoCleanup.stopRequested = false;
    state.autoCleanup.round = 0;
    setAutoCleanupRunning(true);
    openJobWorkbench();

    while (!state.autoCleanup.stopRequested) {
      state.autoCleanup.round += 1;
      const round = state.autoCleanup.round;
      if (rules.maxRounds > 0 && round > rules.maxRounds) {
        stopAutoCleanup(`已达最大轮次 ${rules.maxRounds}，自动停止`);
        break;
      }
      setAutoCleanupStatus(`第 ${round} 轮：探测中（${probeMode === "all" ? "全部" : "筛选结果"}）…`);
      const probeJob = await runHealthCheck(probeMode, {
        auto: true,
        openWorkbench: true,
        confirmLarge: false,
      });
      if (state.autoCleanup.stopRequested) {
        stopAutoCleanup("已手动停止");
        break;
      }
      if (!probeJob) {
        stopAutoCleanup(`第 ${round} 轮探测失败，已停止`);
        break;
      }
      const targets = matchAutoCleanupTargets(state.probeResults, rules);
      if (!targets.length) {
        stopAutoCleanup(`第 ${round} 轮无匹配项，自动停止`);
        break;
      }
      const names = targets.map((t) => t.name).filter(Boolean);
      setAutoCleanupStatus(`第 ${round} 轮：匹配 ${names.length} 条，开始删除…`);
      // 直接删，不弹确认（自动模式）
      const deleteJob = await deleteSelectedJobCredentials({
        names,
        auto: true,
        confirm: false,
      });
      if (state.autoCleanup.stopRequested) {
        stopAutoCleanup("已手动停止");
        break;
      }
      if (!deleteJob) {
        stopAutoCleanup(`第 ${round} 轮删除失败，已停止`);
        break;
      }
      const deleted = (deleteJob.results || []).filter((r) => r.result === "成功").length;
      setAutoCleanupStatus(
        `第 ${round} 轮完成：匹配 ${names.length}，删除成功 ${deleted}；准备下一轮…`
      );
      if (rules.interval > 0) {
        await new Promise((resolve) => {
          state.autoCleanup.timer = setTimeout(resolve, rules.interval * 1000);
        });
        state.autoCleanup.timer = null;
      }
    }
  }

  async function startAutoCleanup() {
    if (!state.connected) {
      toast("请先连接 CPA", "error");
      return;
    }
    if (state.autoCleanup.running || state.busy) {
      toast("已有任务进行中", "error");
      return;
    }
    const rules = collectAutoCleanupSettings();
    const keywords = parseKeywordRules(rules.auto_cleanup_keyword);
    if (!keywords.length) {
      toast("请至少设置一个关键词规则", "error");
      return;
    }
    if (
      !window.confirm(
        `将按规则自动执行：探测 → 筛选 → 删除\n` +
          `范围：${rules.auto_cleanup_scope}\n` +
          `匹配：${rules.auto_cleanup_match}\n` +
          `关键词：${rules.auto_cleanup_keyword}\n` +
          `间隔：${rules.auto_cleanup_interval_seconds}s，最大轮次：${rules.auto_cleanup_max_rounds || "不限"}\n` +
          `确认开始？`
      )
    ) {
      return;
    }
    try {
      await saveSettings({ silent: true });
    } catch (err) {
      toast(err.message || "保存规则失败", "error");
      return;
    }
    runAutoCleanupLoop().catch((err) => {
      stopAutoCleanup(`自动巡检异常：${err.message || err}`);
    });
  }

  async function downloadExport(path, names, confirmText) {
    if (confirmText && !window.confirm(confirmText)) return;
    const selected = names || requireSelection();
    if (!selected) return;
    setBusy(true, "导出中…");
    setJobUI({
      status: "running",
      current: 0,
      total: selected.length,
      message: "正在导出…",
      results: [],
    });
    try {
      const { data, resp } = await api(path, {
        method: "POST",
        body: { names: selected },
      });
      const success = resp.headers.get("X-Export-Success") || "0";
      const failed = resp.headers.get("X-Export-Failed") || "0";
      const skipped = resp.headers.get("X-Export-Skipped") || "0";
      let results = [];
      const summaryRaw = resp.headers.get("X-Job-Summary");
      if (summaryRaw) {
        try {
          const summary = JSON.parse(summaryRaw);
          results = summary.results || [];
        } catch (_) {
          /* ignore */
        }
      }
      setJobUI({
        status: Number(failed) > 0 ? "failed" : "success",
        current: selected.length,
        total: selected.length,
        message: `导出完成：成功 ${success} / 失败 ${failed} / 跳过 ${skipped}`,
        results,
      });
      // 触发浏览器下载
      const blob = data instanceof Blob ? data : new Blob([data], { type: "application/zip" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "credentials-export.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast(`导出完成：成功 ${success}，失败 ${failed}，跳过 ${skipped}`, Number(failed) > 0 ? "error" : "ok");
      if (path.includes("export-delete")) {
        state.selected.clear();
        updateSelectionSummary();
        await loadCredentials();
      }
    } catch (err) {
      toast(err.message || "导出失败", "error");
      setJobUI({
        status: "failed",
        current: 0,
        total: selected.length,
        message: err.message || "导出失败",
        results: [],
      });
    } finally {
      setBusy(false);
    }
  }

  function openImportModal(items) {
    state.importPreview = items || [];
    const body = $("import-preview-body");
    body.innerHTML = "";
    let importCount = 0;
    let skipCount = 0;
    let expiredRefreshCount = 0;
    for (const item of state.importPreview) {
      const tr = document.createElement("tr");
      const actions = (item.available_actions || ["import", "skip"]).slice();
      const options = actions
        .map(
          (a) =>
            `<option value="${escapeAttr(a)}" ${a === item.planned_action ? "selected" : ""}>${escapeHtml(a)}</option>`
        )
        .join("");
      if ((item.planned_action || "") === "skip") skipCount += 1;
      else importCount += 1;
      const summary = String(item.summary || "");
      if (summary.includes("refresh_token") || summary.includes("刷新")) {
        expiredRefreshCount += 1;
      }
      tr.innerHTML = `
        <td>${escapeHtml(item.source_name)}</td>
        <td><input class="import-target" data-source="${escapeAttr(item.source_name)}" value="${escapeAttr(item.target_name || item.source_name)}" /></td>
        <td>${escapeHtml(item.provider || "-")}</td>
        <td>${escapeHtml(item.email_masked || "-")}</td>
        <td>${item.valid ? "是" : "否"}</td>
        <td>${escapeHtml(item.duplicate_type || "-")}</td>
        <td><select class="import-action" data-source="${escapeAttr(item.source_name)}">${options}</select></td>
        <td title="${escapeAttr(item.summary || "")}">${escapeHtml(item.summary || "")}</td>
      `;
      body.appendChild(tr);
    }
    const refreshHint =
      expiredRefreshCount > 0
        ? `；其中约 ${expiredRefreshCount} 条过期但可刷新（确认导入后才会请求 token 端点）`
        : "；确认导入后，对含 refresh_token 的项会按并发刷新";
    $("import-preview-summary").textContent =
      `共 ${state.importPreview.length} 项（默认导入 ${importCount} / 跳过 ${skipCount}）` + refreshHint;
    $("import-modal").hidden = false;
  }

  function closeImportModal() {
    $("import-modal").hidden = true;
  }

  function openPasteImportModal() {
    $("paste-import-text").value = "";
    $("paste-import-modal").hidden = false;
    setTimeout(() => $("paste-import-text").focus(), 0);
  }

  function closePasteImportModal() {
    $("paste-import-modal").hidden = true;
  }

  function setPathImportStatus(text) {
    $("path-import-status").textContent = text || "路径与拖放二选一即可";
  }

  function renderPathDropList(files) {
    const list = $("path-drop-list");
    list.innerHTML = "";
    if (!files.length) {
      list.hidden = true;
      return;
    }
    list.hidden = false;
    for (const f of files.slice(0, 30)) {
      const li = document.createElement("li");
      li.textContent = f.name;
      list.appendChild(li);
    }
    if (files.length > 30) {
      const li = document.createElement("li");
      li.textContent = `… 另有 ${files.length - 30} 个文件`;
      list.appendChild(li);
    }
  }

  function setPathDropFiles(fileList) {
    const files = Array.from(fileList || []).filter((f) =>
      String(f.name || "").toLowerCase().endsWith(".json")
    );
    state.pathDropFiles = files;
    renderPathDropList(files);
    if (files.length) {
      setPathImportStatus(`已选择 ${files.length} 个拖放文件（优先于路径）`);
    } else {
      setPathImportStatus("路径与拖放二选一即可");
    }
  }

  function openPathImportModal() {
    $("path-import-input").value = "";
    $("path-import-file").value = "";
    setPathDropFiles([]);
    $("path-dropzone").classList.remove("dragover");
    $("path-import-modal").hidden = false;
    setTimeout(() => $("path-import-input").focus(), 0);
  }

  function closePathImportModal() {
    $("path-import-modal").hidden = true;
    $("path-dropzone").classList.remove("dragover");
    setPathDropFiles([]);
  }

  async function handleImportFiles(fileList, { closePathModal = false } = {}) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    setBusy(true, "预检中…");
    try {
      const { data } = await api("/api/cpa/import/preview", { method: "POST", body: form });
      if (closePathModal) closePathImportModal();
      openImportModal(data.items || []);
      toast(`已预检 ${data.total || (data.items || []).length} 项`, "ok");
    } catch (err) {
      toast(err.message || "导入预检失败", "error");
    } finally {
      setBusy(false);
      $("import-file").value = "";
      $("path-import-file").value = "";
    }
  }

  async function handlePasteImportPreview() {
    const text = ($("paste-import-text").value || "").trim();
    if (!text) {
      toast("请先粘贴凭证文本", "error");
      return;
    }
    setBusy(true, "识别中…");
    try {
      const { data } = await api("/api/cpa/import/preview-text", {
        method: "POST",
        body: { text },
      });
      closePasteImportModal();
      openImportModal(data.items || []);
      toast(`已识别 ${data.total || (data.items || []).length} 条`, "ok");
    } catch (err) {
      toast(err.message || "粘贴识别失败", "error");
    } finally {
      setBusy(false);
    }
  }

  async function handlePathImportPreview() {
    const dropped = state.pathDropFiles || [];
    if (dropped.length) {
      await handleImportFiles(dropped, { closePathModal: true });
      return;
    }
    const path = ($("path-import-input").value || "").trim();
    if (!path) {
      toast("请填写本机路径，或拖放 JSON 文件", "error");
      return;
    }
    setBusy(true, "读取路径…");
    try {
      const { data } = await api("/api/cpa/import/preview-path", {
        method: "POST",
        body: { path },
      });
      closePathImportModal();
      openImportModal(data.items || []);
      toast(`已从路径预检 ${data.total || (data.items || []).length} 项`, "ok");
    } catch (err) {
      toast(err.message || "路径预检失败", "error");
    } finally {
      setBusy(false);
    }
  }

  async function executeImport() {
    if (!state.importPreview.length) {
      toast("没有可导入项", "error");
      return;
    }
    // 覆盖类动作二次确认
    const actions = Array.from(document.querySelectorAll(".import-action")).map((el) => ({
      source_name: el.dataset.source,
      planned_action: el.value,
    }));
    if (actions.some((a) => a.planned_action === "overwrite")) {
      if (!window.confirm("导入将覆盖同名凭证，确认继续？")) return;
    }
    const targets = {};
    for (const el of document.querySelectorAll(".import-target")) {
      targets[el.dataset.source] = el.value.trim();
    }
    const payload = {
      items: actions.map((a) => ({
        source_name: a.source_name,
        planned_action: a.planned_action,
        target_name: targets[a.source_name] || a.source_name,
      })),
    };
    setBusy(true, "导入中…");
    setJobUI({
      status: "queued",
      current: 0,
      total: payload.items.length,
      message: "提交导入任务…",
      results: [],
    });
    try {
      const { data } = await api("/api/cpa/import/execute", {
        method: "POST",
        body: payload,
      });
      closeImportModal();
      await pollJob(data.job_id, {
        onDone: async (job) => {
          toast(job.status === "success" ? "导入完成" : "导入结束（有失败）", job.status === "success" ? "ok" : "error");
          await loadCredentials();
        },
      });
    } catch (err) {
      setBusy(false);
      toast(err.message || "导入执行失败", "error");
      setJobUI({
        status: "failed",
        current: 0,
        total: payload.items.length,
        message: err.message || "导入执行失败",
        results: [],
      });
    }
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function escapeAttr(value) {
    return escapeHtml(value).replaceAll("`", "&#96;");
  }

  function bindEvents() {
    $("btn-connect").addEventListener("click", () => connect());
    $("btn-save-profile").addEventListener("click", () => {
      saveProfile().catch((err) => toast(err.message || "保存失败", "error"));
    });
    $("btn-save-settings").addEventListener("click", () => {
      saveSettings().catch((err) => toast(err.message || "保存失败", "error"));
    });
    $("profile-select").addEventListener("change", () => {
      const name = $("profile-select").value;
      const profile = state.profiles.find((p) => p.name === name);
      applyProfile(profile);
    });

    $("btn-refresh").addEventListener("click", () => refreshList());
    $("btn-import").addEventListener("click", () => $("import-file").click());
    $("btn-import-path").addEventListener("click", () => openPathImportModal());
    $("btn-import-paste").addEventListener("click", () => openPasteImportModal());
    $("import-file").addEventListener("change", (e) => handleImportFiles(e.target.files));
    $("btn-export").addEventListener("click", () => downloadExport("/api/cpa/export"));
    $("btn-export-delete").addEventListener("click", () =>
      downloadExport("/api/cpa/export-delete", null, "将导出并删除远端凭证，确认继续？")
    );
    $("btn-health").addEventListener("click", () => runHealthCheck("selected"));
    $("btn-health-page").addEventListener("click", () => runHealthCheck("page"));
    $("btn-health-filtered").addEventListener("click", () => runHealthCheck("filtered"));
    $("btn-health-all").addEventListener("click", () => runHealthCheck("all"));
    $("probe-workers-once").addEventListener("input", () => {
      $("probe-workers-once").dataset.touched = "1";
    });

    $("btn-open-job-workbench").addEventListener("click", () => openJobWorkbench());
    $("btn-open-job-workbench-2").addEventListener("click", () => openJobWorkbench());
    $("btn-open-job-workbench-top").addEventListener("click", () => openJobWorkbench());
    $("job-workbench-close").addEventListener("click", closeJobWorkbench);
    $("btn-job-workbench-close").addEventListener("click", closeJobWorkbench);
    $("job-workbench-modal").addEventListener("click", (e) => {
      if (e.target === $("job-workbench-modal")) closeJobWorkbench();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("job-workbench-modal").hidden) {
        closeJobWorkbench();
      }
    });
    $("job-filter-text").addEventListener("input", () => {
      clearTimeout(state._jobFilterTimer);
      state._jobFilterTimer = setTimeout(() => renderJobResultsTable(), 200);
    });
    $("job-filter-result").addEventListener("change", () => renderJobResultsTable());
    $("job-filter-bad-only").addEventListener("change", () => renderJobResultsTable());
    $("btn-job-select-filtered").addEventListener("click", () => {
      for (const item of filteredJobResults()) {
        if (item.name) state.jobSelected.add(item.name);
      }
      renderJobResultsTable();
    });
    $("btn-job-clear-selected").addEventListener("click", () => {
      state.jobSelected.clear();
      renderJobResultsTable();
    });
    $("btn-job-delete-selected").addEventListener("click", () => {
      deleteSelectedJobCredentials().catch((err) =>
        toast(err.message || "删除失败", "error")
      );
    });
    $("btn-auto-cleanup-save").addEventListener("click", () => {
      saveSettings()
        .then(() => toast("自动巡检规则已保存", "ok"))
        .catch((err) => toast(err.message || "保存失败", "error"));
    });
    $("btn-auto-cleanup-start").addEventListener("click", () => {
      startAutoCleanup().catch((err) => toast(err.message || "启动失败", "error"));
    });
    $("btn-auto-cleanup-stop").addEventListener("click", () => {
      stopAutoCleanup("已手动停止");
      toast("已请求停止自动巡检", "ok");
    });
    $("job-check-all").addEventListener("change", (e) => {
      const checked = e.target.checked;
      const visible = filteredJobResults();
      for (const item of visible) {
        if (!item.name) continue;
        if (checked) state.jobSelected.add(item.name);
        else state.jobSelected.delete(item.name);
      }
      renderJobResultsTable();
    });
    $("job-results-body").addEventListener("click", (e) => {
      const target = e.target;
      if (target && target.classList && target.classList.contains("job-row-check")) {
        const name = target.dataset.name;
        if (!name) return;
        if (target.checked) state.jobSelected.add(name);
        else state.jobSelected.delete(name);
        updateJobSelectionSummary();
        syncJobCheckAll(filteredJobResults());
      }
    });

    const filterIds = [
      "filter-search",
      "filter-status",
      "filter-provider",
      "filter-exportable",
      "filter-health",
      "filter-page-size",
    ];
    for (const id of filterIds) {
      const el = $(id);
      const handler = () => {
        state.page = 1;
        loadCredentials();
      };
      el.addEventListener("change", handler);
      if (el.tagName === "INPUT") {
        let t = null;
        el.addEventListener("input", () => {
          clearTimeout(t);
          t = setTimeout(handler, 300);
        });
      }
    }

    $("page-prev").addEventListener("click", () => {
      if (state.page > 1) {
        state.page -= 1;
        loadCredentials();
      }
    });
    $("page-next").addEventListener("click", () => {
      if (state.page < state.totalPages) {
        state.page += 1;
        loadCredentials();
      }
    });

    $("check-all").addEventListener("change", (e) => {
      const checked = e.target.checked;
      for (const item of state.currentItems) {
        if (checked) state.selected.add(item.name);
        else state.selected.delete(item.name);
      }
      for (const box of document.querySelectorAll(".row-check")) {
        box.checked = checked;
        const tr = box.closest("tr");
        if (tr) tr.classList.toggle("selected", checked);
      }
      updateSelectionSummary();
    });

    $("credentials-body").addEventListener("click", (e) => {
      const target = e.target;
      if (target && target.classList && target.classList.contains("row-check")) {
        const name = target.dataset.name;
        if (target.checked) state.selected.add(name);
        else state.selected.delete(name);
        const tr = target.closest("tr");
        if (tr) tr.classList.toggle("selected", target.checked);
        updateSelectionSummary();
        syncCheckAll();
        e.stopPropagation();
        return;
      }
      const tr = target.closest("tr");
      if (!tr || !tr.dataset.name) return;
      showDetail(tr.dataset.name);
    });

    $("import-modal-close").addEventListener("click", closeImportModal);
    $("btn-import-cancel").addEventListener("click", closeImportModal);
    $("btn-import-execute").addEventListener("click", () => executeImport());
    $("import-modal").addEventListener("click", (e) => {
      if (e.target === $("import-modal")) closeImportModal();
    });
    $("paste-import-close").addEventListener("click", closePasteImportModal);
    $("btn-paste-import-cancel").addEventListener("click", closePasteImportModal);
    $("btn-paste-import-preview").addEventListener("click", () => handlePasteImportPreview());
    $("paste-import-modal").addEventListener("click", (e) => {
      if (e.target === $("paste-import-modal")) closePasteImportModal();
    });

    $("path-import-close").addEventListener("click", closePathImportModal);
    $("btn-path-import-cancel").addEventListener("click", closePathImportModal);
    $("btn-path-import-preview").addEventListener("click", () => handlePathImportPreview());
    $("path-import-modal").addEventListener("click", (e) => {
      if (e.target === $("path-import-modal")) closePathImportModal();
    });
    $("btn-path-browse").addEventListener("click", () => $("path-import-file").click());
    $("path-import-file").addEventListener("change", (e) => {
      setPathDropFiles(e.target.files);
    });
    $("path-import-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handlePathImportPreview();
      }
    });

    const dropzone = $("path-dropzone");
    dropzone.addEventListener("click", () => $("path-import-file").click());
    dropzone.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        $("path-import-file").click();
      }
    });
    ["dragenter", "dragover"].forEach((evt) => {
      dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.add("dragover");
      });
    });
    ["dragleave", "dragend"].forEach((evt) => {
      dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.remove("dragover");
      });
    });
    dropzone.addEventListener("drop", (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove("dragover");
      const files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) setPathDropFiles(files);
    });
  }

  async function boot() {
    bindEvents();
    setConnected(false);
    setJobUI({ status: "空闲", current: 0, total: 0, message: "尚无批量任务", results: [] });
    try {
      await Promise.all([loadSettings(), loadProfiles()]);
    } catch (err) {
      toast(err.message || "初始化失败", "error");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
