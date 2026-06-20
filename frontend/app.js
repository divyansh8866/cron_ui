"use strict";

const state = {
  jobs: [],
  editingId: null,
  logsJobId: null,
  activeRunId: null,
  logTimer: null,
  pollTimer: null,
};

// ----------------------------------------------------------------- auth
function token() { return localStorage.getItem("cron_ui_token") || ""; }

async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (token()) headers["X-Auth-Token"] = token();
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401) {
    const t = prompt("This Cron UI is protected. Enter access token:");
    if (t) { localStorage.setItem("cron_ui_token", t.trim()); return api(path, opts); }
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

// ----------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);

function toast(msg, kind = "") {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast " + kind;
  setTimeout(() => el.classList.add("hidden"), 2600);
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function relTime(ts) {
  if (!ts) return "never";
  const diff = ts * 1000 - Date.now();
  const abs = Math.abs(diff);
  const m = Math.round(abs / 60000), h = Math.round(abs / 3600000), d = Math.round(abs / 86400000);
  let s;
  if (abs < 60000) s = "<1 min";
  else if (m < 60) s = `${m} min`;
  else if (h < 48) s = `${h} h`;
  else s = `${d} d`;
  return diff >= 0 ? `in ${s}` : `${s} ago`;
}

function fmtDuration(a, b) {
  if (!a || !b) return "";
  const sec = Math.max(0, Math.round(b - a));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60), r = sec % 60;
  return `${m}m ${r}s`;
}

// Minimal cron -> human description.
function describeCron(expr) {
  const p = expr.trim().split(/\s+/);
  if (p.length !== 5) return "";
  const [min, hr, dom, mon, dow] = p;
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  if (expr === "* * * * *") return "Every minute";
  if (/^\*\/\d+$/.test(min) && hr === "*" && dom === "*" && mon === "*" && dow === "*")
    return `Every ${min.slice(2)} minutes`;
  if (min === "0" && hr === "*" && dom === "*" && mon === "*" && dow === "*") return "Every hour";
  if (/^\d+$/.test(min) && /^\d+$/.test(hr)) {
    const t = `${hr.padStart(2, "0")}:${min.padStart(2, "0")}`;
    if (dom === "*" && mon === "*" && dow === "*") return `Daily at ${t}`;
    if (dom === "*" && mon === "*" && /^\d$/.test(dow)) return `Weekly on ${days[+dow]} at ${t}`;
    if (/^\d+$/.test(dom) && mon === "*" && dow === "*") return `Monthly on day ${dom} at ${t}`;
  }
  return "";
}

// ----------------------------------------------------------------- rendering
function statusBadge(job) {
  if (!job.enabled) return `<span class="badge off">paused</span>`;
  if (job.is_running) return `<span class="badge running">running</span>`;
  const lr = job.last_run;
  if (!lr) return `<span class="badge none">idle</span>`;
  return `<span class="badge ${lr.status}">${lr.status}</span>`;
}

function jobCard(job) {
  const human = describeCron(job.schedule);
  const lr = job.last_run;
  const live = job.is_running ? `<span class="dot live"></span>` : "";
  return `
  <div class="card ${job.enabled ? "" : "disabled"}" data-id="${job.id}">
    <div class="card-top">
      <div>
        <p class="card-name">${escapeHtml(job.name)}</p>
        <p class="card-cron">${escapeHtml(job.schedule)}</p>
        ${human ? `<p class="card-human">${human}</p>` : ""}
      </div>
      ${statusBadge(job)}
    </div>
    <div class="card-meta">
      <div class="meta-line"><span class="k">Next run</span><span class="v">${live}${job.enabled ? `${fmtTime(job.next_run)} <span class="hint">(${relTime(job.next_run)})</span>` : "—"}</span></div>
      <div class="meta-line"><span class="k">Last run</span><span class="v">${lr ? `${fmtTime(lr.started_at)}` : "never"}</span></div>
      ${lr && lr.finished_at ? `<div class="meta-line"><span class="k">Duration</span><span class="v">${fmtDuration(lr.started_at, lr.finished_at)}${lr.exit_code != null ? ` · exit ${lr.exit_code}` : ""}</span></div>` : ""}
      ${job.kuma_url ? `<div class="meta-line"><span class="k">Uptime Kuma</span><span class="v">📡 connected</span></div>` : ""}
    </div>
    <div class="card-actions">
      <button class="btn small" data-act="run">▶ Run</button>
      <button class="btn small" data-act="logs">Logs</button>
      <button class="btn small" data-act="toggle">${job.enabled ? "Pause" : "Resume"}</button>
      <button class="btn small" data-act="edit">Edit</button>
      <button class="btn small danger" data-act="delete">Delete</button>
    </div>
  </div>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function render() {
  const grid = $("jobGrid");
  if (state.jobs.length === 0) {
    grid.innerHTML = "";
    $("emptyState").classList.remove("hidden");
    return;
  }
  $("emptyState").classList.add("hidden");
  grid.innerHTML = state.jobs.map(jobCard).join("");
}

// ----------------------------------------------------------------- data
async function loadJobs() {
  try {
    state.jobs = await api("/api/jobs");
    render();
  } catch (e) { toast(e.message, "err"); }
}

async function loadHealth() {
  try {
    const h = await api("/api/health");
    $("hostInfo").textContent = `running as uid ${h.uid} · TZ ${h.tz}${h.auth ? " · 🔒 protected" : ""}`;
  } catch (_) {}
}

// ----------------------------------------------------------------- modal
function openModal(job) {
  state.editingId = job ? job.id : null;
  $("modalTitle").textContent = job ? "Edit Job" : "New Job";
  $("f_name").value = job ? job.name : "";
  $("f_schedule").value = job ? job.schedule : "";
  $("f_timezone").value = job && job.timezone ? job.timezone : "";
  $("f_timeout").value = job ? job.timeout || 0 : 0;
  $("f_workdir").value = job && job.working_dir ? job.working_dir : "";
  $("f_kuma").value = job && job.kuma_url ? job.kuma_url : "";
  $("f_env").value = job ? Object.entries(job.env || {}).map(([k, v]) => `${k}=${v}`).join("\n") : "";
  $("f_script").value = job ? job.script || "" : "#!/bin/sh\n";
  $("f_enabled").checked = job ? job.enabled : true;
  $("formError").textContent = "";
  updatePreview();
  $("jobModal").classList.remove("hidden");
  $("f_name").focus();
}

function closeModal() { $("jobModal").classList.add("hidden"); }

function parseEnv(text) {
  const env = {};
  text.split("\n").forEach((line) => {
    line = line.trim();
    if (!line || line.startsWith("#")) return;
    const i = line.indexOf("=");
    if (i > 0) env[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  });
  return env;
}

async function updatePreview() {
  const expr = $("f_schedule").value.trim();
  const prev = $("schedulePreview");
  if (!expr) { prev.textContent = ""; prev.className = "schedule-preview"; return; }
  try {
    const r = await api("/api/validate", {
      method: "POST",
      body: JSON.stringify({ schedule: expr, timezone: $("f_timezone").value.trim() || null }),
    });
    if (r.valid) {
      const human = describeCron(expr);
      prev.textContent = `✓ valid${human ? ` — ${human}` : ""} · next: ${fmtTime(r.next_run)}`;
      prev.className = "schedule-preview ok";
    } else {
      prev.textContent = "✗ invalid cron expression";
      prev.className = "schedule-preview bad";
    }
  } catch (_) {}
}

async function saveJob() {
  const payload = {
    name: $("f_name").value.trim(),
    schedule: $("f_schedule").value.trim(),
    script: $("f_script").value,
    timezone: $("f_timezone").value.trim() || null,
    timeout: parseInt($("f_timeout").value || "0", 10),
    working_dir: $("f_workdir").value.trim() || null,
    kuma_url: $("f_kuma").value.trim() || null,
    env: parseEnv($("f_env").value),
    enabled: $("f_enabled").checked,
  };
  if (!payload.name) { $("formError").textContent = "Name is required"; return; }
  if (!payload.schedule) { $("formError").textContent = "Schedule is required"; return; }
  try {
    if (state.editingId) {
      await api(`/api/jobs/${state.editingId}`, { method: "PATCH", body: JSON.stringify(payload) });
      toast("Job updated", "ok");
    } else {
      await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
      toast("Job created", "ok");
    }
    closeModal();
    loadJobs();
  } catch (e) { $("formError").textContent = e.message; }
}

// ----------------------------------------------------------------- logs
async function openLogs(jobId) {
  state.logsJobId = jobId;
  state.activeRunId = null;
  const job = state.jobs.find((j) => j.id === jobId);
  $("logsTitle").textContent = `Run history — ${job ? job.name : ""}`;
  $("logView").textContent = "Select a run to view output…";
  $("logsModal").classList.remove("hidden");
  await refreshRuns(true);
  state.logTimer = setInterval(refreshLogTick, 2500);
}

function closeLogs() {
  $("logsModal").classList.add("hidden");
  clearInterval(state.logTimer);
  state.logTimer = null;
}

async function refreshRuns(selectFirst = false) {
  try {
    const runs = await api(`/api/jobs/${state.logsJobId}/runs?limit=50`);
    $("runsList").innerHTML = runs.length
      ? runs.map((r) => `
        <div class="run-item ${r.id === state.activeRunId ? "active" : ""}" data-run="${r.id}">
          <div class="row1">
            <span class="time">${fmtTime(r.started_at)}</span>
            <span class="badge ${r.status}">${r.status}</span>
          </div>
          <span class="sub2">${r.trigger}${r.finished_at ? ` · ${fmtDuration(r.started_at, r.finished_at)}` : ""}${r.exit_code != null ? ` · exit ${r.exit_code}` : ""}</span>
        </div>`).join("")
      : `<div class="run-item"><span class="sub2">No runs yet</span></div>`;
    if (selectFirst && runs.length) viewRun(runs[0].id);
  } catch (e) { toast(e.message, "err"); }
}

async function viewRun(runId) {
  state.activeRunId = runId;
  document.querySelectorAll(".run-item").forEach((el) =>
    el.classList.toggle("active", +el.dataset.run === runId));
  try {
    const log = await api(`/api/runs/${runId}/log`);
    const view = $("logView");
    const atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 40;
    view.textContent = log || "(no output)";
    if (atBottom) view.scrollTop = view.scrollHeight;
  } catch (e) { toast(e.message, "err"); }
}

async function refreshLogTick() {
  // Keep the run list fresh, and live-tail the active run.
  await refreshRuns(false);
  if (state.activeRunId) await viewRun(state.activeRunId);
}

// ----------------------------------------------------------------- events
function bind() {
  $("newJobBtn").onclick = () => openModal(null);
  $("newJobBtn2").onclick = () => openModal(null);
  $("refreshBtn").onclick = loadJobs;
  $("closeModal").onclick = closeModal;
  $("cancelModal").onclick = closeModal;
  $("saveJob").onclick = saveJob;
  $("closeLogs").onclick = closeLogs;
  $("f_schedule").addEventListener("input", debounce(updatePreview, 300));
  $("f_timezone").addEventListener("input", debounce(updatePreview, 400));
  $("f_preset").onchange = (e) => {
    if (e.target.value) { $("f_schedule").value = e.target.value; updatePreview(); e.target.value = ""; }
  };
  $("kumaTest").onclick = async () => {
    const url = $("f_kuma").value.trim();
    if (!url) { toast("Enter a push URL first", "err"); return; }
    try {
      const r = await api("/api/kuma/test", { method: "POST", body: JSON.stringify({ kuma_url: url }) });
      toast(r.ok ? "Kuma heartbeat sent ✓" : "Kuma push failed — check URL/network", r.ok ? "ok" : "err");
    } catch (e) { toast(e.message, "err"); }
  };

  $("jobGrid").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const card = e.target.closest(".card");
    const id = +card.dataset.id;
    const job = state.jobs.find((j) => j.id === id);
    const act = btn.dataset.act;
    try {
      if (act === "run") { await api(`/api/jobs/${id}/run`, { method: "POST" }); toast("Run started", "ok"); setTimeout(loadJobs, 600); }
      else if (act === "logs") openLogs(id);
      else if (act === "toggle") { await api(`/api/jobs/${id}/toggle`, { method: "POST" }); loadJobs(); }
      else if (act === "edit") openModal(job);
      else if (act === "delete") {
        if (confirm(`Delete job "${job.name}"? This removes its history.`)) {
          await api(`/api/jobs/${id}`, { method: "DELETE" }); toast("Job deleted", "ok"); loadJobs();
        }
      }
    } catch (err) { toast(err.message, "err"); }
  });

  $("runsList").addEventListener("click", (e) => {
    const item = e.target.closest("[data-run]");
    if (item) viewRun(+item.dataset.run);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeModal(); closeLogs(); }
  });
  // Close modals on backdrop click.
  $("jobModal").addEventListener("click", (e) => { if (e.target.id === "jobModal") closeModal(); });
  $("logsModal").addEventListener("click", (e) => { if (e.target.id === "logsModal") closeLogs(); });
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// ----------------------------------------------------------------- boot
bind();
loadHealth();
loadJobs();
state.pollTimer = setInterval(loadJobs, 10000);
