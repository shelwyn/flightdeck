"use strict";

const POLL_MS = 3000;
let timer = null;
let boardTimer = null;
let selectedProject = null;   // Mission Control: project shown on the board
let selectedIteration = null; // Mission Control: iteration shown on the board
let iterationsCache = [];     // iterations for selectedProject
let creatingTaskKind = "task"; // task modal: "task" | "bug"
let activeProject = null;     // Operations: project the orchestrator is working
let activeIteration = null;   // Operations: iteration being orchestrated
let orchestrating = false;
let projectsCache = [];       // last loaded projects (for edit prefill)
let editingSlug = null;       // non-null while the project form is in edit mode
let editingTaskId = null;     // non-null while the task form is in edit mode
let draggingTaskId = null;    // task id being dragged on the sprint board
let draggingFrom = null;      // its source column (state)
let artifactProject = null;   // Artifacts tab: selected project slug
let artifactFile = null;      // Artifacts tab: selected file path
let artifactOriginal = "";    // content when file was opened / last saved
let artifactEditing = false;
let artifactCanEdit = false;
let artConsoleHistory = [];
let artConsoleHistIdx = -1;
let modelSetupConfigured = false;
let modelTestPassed = false;

const $ = (id) => document.getElementById(id);
const BOARD_COLUMNS = ["Todo", "In Progress", "Completed", "Cancelled"];
const BOARD_RENDER_ORDER = [
  "Todo", "In Progress", "Completed", "__testing__", "Cancelled",
];
let lastApiState = null;
const PI_DEFAULT_MODEL = "__pi_default__";

// --- theme (dark / light) ---
const SUN = "\u2600";   // ☀ shown in dark mode (click → light)
const MOON = "\u263E";  // ☾ shown in light mode (click → dark)
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const btn = document.getElementById("theme-toggle");
  if (btn) btn.textContent = theme === "light" ? MOON : SUN;
}
function initTheme() {
  let theme = localStorage.getItem("fd-theme");
  if (!theme) {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light" : "dark";
  }
  applyTheme(theme);
}
function toggleTheme() {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  localStorage.setItem("fd-theme", next);
  applyTheme(next);
}

function fmtNum(n) { return (n || 0).toLocaleString(); }

function fmtRuntime(seconds) {
  seconds = Math.round(seconds || 0);
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleTimeString();
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const COPY_ICON = `<svg class="copy-icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><rect x="5" y="5" width="9" height="9" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.25"/><path d="M3 11V3.5A1.5 1.5 0 0 1 4.5 2H11" fill="none" stroke="currentColor" stroke-width="1.25"/></svg>`;
const EDIT_ICON = `<svg class="edit-icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M11.5 2.5a1.4 1.4 0 0 1 2 2L6 12l-3 1 1-3 7.5-7.5Z" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linejoin="round"/></svg>`;
const DELETE_ICON = `<svg class="delete-icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M3 4.5h10M6 4.5V3.5h4v1M5 4.5v8h6v-8" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const MENU_ICON = `<svg class="menu-icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M2.5 4h11M2.5 8h11M2.5 12h11" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linecap="round"/></svg>`;

const TERMINAL_COLUMNS = new Set(["Completed", "Cancelled"]);
const BACKWARD_COLUMNS = new Set(["Todo", "In Progress"]);

const STATUS_TICK_SVG =
  '<svg class="status-tick" viewBox="0 0 12 12" aria-hidden="true"><path d="M2 6.5l2.75 2.75L10 3.5" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function columnStatusKind(columnName) {
  const n = String(columnName || "").toLowerCase();
  if (n === "todo") return "todo";
  if (n === "in progress") return "progress";
  if (n === "completed") return "done";
  return "";
}

function testingStatusKind(status) {
  if (status === "running") return "progress";
  if (status === "passed") return "done";
  if (status === "standby" || status === "idle") return "todo";
  return "todo";
}

function cardStatusDot(kind) {
  if (!kind) return "";
  if (kind === "done") {
    return `<span class="card-status-indicator card-status-done" aria-hidden="true">${STATUS_TICK_SVG}</span>`;
  }
  return `<span class="card-status-indicator card-status-${kind}" aria-hidden="true"></span>`;
}

function cardStatusFooter(kind, planRank) {
  const rank =
    planRank != null
      ? `<span class="card-plan-rank" title="Planned agent order">${planRank}</span>`
      : "";
  const dot = cardStatusDot(kind);
  if (!rank && !dot) return "";
  return `<div class="card-status-footer">${rank}${dot}</div>`;
}

function cardStatusIndicator(kind) {
  return cardStatusDot(kind);
}

function applyCardStatusFooter(el, kind, planRank) {
  if (!el) return;
  const html = cardStatusFooter(kind, planRank);
  const footer = el.querySelector(".card-status-footer");
  if (!html) {
    footer?.remove();
    return;
  }
  if (footer) {
    footer.outerHTML = html;
  } else {
    el.insertAdjacentHTML("beforeend", html);
  }
}

function applyCardStatusIndicator(el, kind) {
  applyCardStatusFooter(el, kind, null);
}

function buildPlanRankMap(columns) {
  const tasks = [];
  for (const [col, list] of Object.entries(columns || {})) {
    if (col === "Cancelled") continue;
    for (const t of list) {
      if (typeof t.plan_order === "number") tasks.push(t);
    }
  }
  tasks.sort((a, b) => {
    const ao = a.plan_order;
    const bo = b.plan_order;
    if (ao !== bo) return ao - bo;
    const ap = typeof a.priority === "number" ? a.priority : 9999;
    const bp = typeof b.priority === "number" ? b.priority : 9999;
    if (ap !== bp) return ap - bp;
    const ac = a.created_at || "";
    const bc = b.created_at || "";
    if (ac !== bc) return ac.localeCompare(bc);
    return (a.identifier || "").localeCompare(b.identifier || "");
  });
  const map = new Map();
  tasks.forEach((t, i) => map.set(t.id, i + 1));
  return map;
}

function setModalBusy(cardEl, loaderEl, textEl, busy, message) {
  if (!cardEl || !loaderEl) return;
  cardEl.classList.toggle("is-busy", busy);
  loaderEl.classList.toggle("hidden", !busy);
  loaderEl.setAttribute("aria-hidden", busy ? "false" : "true");
  if (textEl && message) textEl.textContent = message;
}

function taskCopyText(t) {
  const lines = [`${t.identifier}: ${t.title}`];
  if (t.description) lines.push("", t.description);
  if (t.agent_name) lines.push("", `Agent: ${t.agent_name}`);
  return lines.join("\n");
}

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
  }
  if (btn) {
    btn.classList.add("copied");
    btn.title = "Copied!";
    setTimeout(() => {
      btn.classList.remove("copied");
      btn.title = "Copy task details";
    }, 1500);
  }
}

function setConn(ok) {
  const dot = $("status-dot");
  dot.classList.toggle("ok", ok);
  dot.classList.toggle("err", !ok);
}

function clientLog(level, message, context) {
  const payload = {
    level: level || "info",
    message: String(message || ""),
    context: context || undefined,
  };
  fetch("/api/v1/logs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    keepalive: true,
  }).catch(() => {});
}

async function api(path, opts) {
  const res = await fetch(path, { cache: "no-store", ...(opts || {}) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = (data && data.error && data.error.message) || String(res.status);
    clientLog("error", "API request failed", {
      method: (opts && opts.method) || "GET",
      path,
      status: res.status,
      error: msg,
    });
    throw new Error(msg);
  }
  return data;
}

function row(cells, onClick) {
  const tr = document.createElement("tr");
  tr.className = "row";
  for (const c of cells) {
    const td = document.createElement("td");
    if (c && c.html !== undefined) {
      td.innerHTML = c.html;
      if (c.cls) td.className = c.cls;
    } else {
      td.textContent = c == null ? "—" : c;
    }
    tr.appendChild(td);
  }
  if (onClick) tr.addEventListener("click", onClick);
  return tr;
}

function emptyRow(cols, text) {
  const tr = document.createElement("tr");
  tr.className = "empty";
  const td = document.createElement("td");
  td.colSpan = cols;
  td.textContent = text;
  tr.appendChild(td);
  return tr;
}

// Force a slug to a single lowercase word: spaces -> hyphen, drop invalid chars.
function slugify(s) {
  return String(s || "")
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^a-z0-9-]/g, "")
    .replace(/-+/g, "-");
}

// Promise-based dialog modals (replace window.alert / window.confirm).
function showDialogModal({
  title = "Confirm",
  message = "",
  html = false,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  danger = false,
  alert = false,
} = {}) {
  return new Promise((resolve) => {
    const modal = $("modal");
    const okBtn = $("modal-confirm");
    const cancelBtn = $("modal-cancel");
    const msgEl = $("modal-message");
    $("modal-title").textContent = title;
    if (html) msgEl.innerHTML = message;
    else msgEl.textContent = message;
    okBtn.textContent = confirmLabel;
    okBtn.className = danger ? "danger" : "primary";
    cancelBtn.textContent = cancelLabel;
    cancelBtn.classList.toggle("hidden", alert);
    modal.classList.remove("hidden");
    okBtn.focus();

    function cleanup(result) {
      modal.classList.add("hidden");
      cancelBtn.classList.remove("hidden");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      modal.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onKey);
      resolve(result);
    }
    const onOk = () => cleanup(alert ? undefined : true);
    const onCancel = () => cleanup(false);
    const onBackdrop = (e) => {
      if (e.target === modal) cleanup(alert ? undefined : false);
    };
    const onKey = (e) => {
      if (e.key === "Escape") cleanup(alert ? undefined : false);
      else if (e.key === "Enter") cleanup(alert ? undefined : true);
    };
    okBtn.addEventListener("click", onOk);
    if (!alert) cancelBtn.addEventListener("click", onCancel);
    modal.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onKey);
  });
}

function confirmModal({ title = "Confirm", message = "", html = false, confirmLabel = "Confirm", danger = true } = {}) {
  return showDialogModal({ title, message, html, confirmLabel, danger, alert: false });
}

function alertModal({ title = "Notice", message = "", html = false, okLabel = "OK" } = {}) {
  return showDialogModal({ title, message, html, confirmLabel: okLabel, alert: true, danger: false });
}

// --- tabs ---
function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-pane").forEach((p) => p.classList.toggle("hidden", p.id !== "tab-" + name));
  document.body.classList.toggle("tab-artifacts-active", name === "artifacts");
  if (name === "artifacts") loadArtifactProjects();
  if (name === "git") loadGitProjects();
}

// --- projects (Mission Control) ---
async function loadProjects() {
  let data;
  try { data = await api("/api/v1/projects"); } catch (e) { return; }
  const projects = data.projects || [];
  projectsCache = projects;

  // Reveal the GIT history tab only when at least one project uses Git.
  const anyGit = projects.some((p) => p.needs_git);
  const gitTabBtn = $("tab-git-btn");
  if (gitTabBtn) {
    gitTabBtn.classList.toggle("hidden", !anyGit);
    if (!anyGit && gitTabBtn.classList.contains("active")) switchTab("mission");
  }

  // Operations dropdown
  const select = $("project-select");
  const prev = select.value;
  select.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = projects.length ? "Select Project" : "No projects yet";
  select.appendChild(placeholder);
  for (const p of projects) {
    const opt = document.createElement("option");
    opt.value = p.slug;
    opt.textContent = `${p.title} (${p.slug})`;
    select.appendChild(opt);
  }
  if (prev && projects.some((p) => p.slug === prev)) select.value = prev;
  else if (activeProject && projects.some((p) => p.slug === activeProject)) select.value = activeProject;
  else select.value = "";
  syncProjectSelectLock();

  // Mission Control project list
  const list = $("projects-list");
  list.replaceChildren();
  if (!projects.length) {
    const n = document.createElement("div");
    n.className = "empty-note";
    n.textContent = "No projects yet. Create one to begin.";
    list.appendChild(n);
    return;
  }
  for (const p of projects) {
    const card = document.createElement("div");
    card.className = "project-card";
    if (p.slug === selectedProject) card.classList.add("selected");
    if (p.slug === activeProject && orchestrating) card.classList.add("active");
    const c = p.counts || {};
    card.innerHTML = `
      <div class="pc-title">${esc(p.title)}</div>
      <div class="pc-slug mono">${esc(p.slug)}</div>
      ${p.workspace_path ? `<div class="pc-ws mono muted">${esc(p.workspace_path)}</div>` : ""}
      <div class="pc-counts">
        <span class="dot-todo"></span>${c["Todo"] || 0}
        <span class="dot-prog"></span>${c["In Progress"] || 0}
        <span class="dot-done"></span>${c["Completed"] || 0}
      </div>
      <div class="pc-actions">
        <button class="link pc-edit" type="button">Edit</button>
        <button class="link pc-del" type="button">Delete</button>
      </div>`;
    card.addEventListener("click", () => selectProject(p.slug));
    card.querySelector(".pc-edit").addEventListener("click", (ev) => {
      ev.stopPropagation();
      startEditProject(p.slug);
    });
    card.querySelector(".pc-del").addEventListener("click", (ev) => {
      ev.stopPropagation();
      deleteProject(p.slug);
    });
    list.appendChild(card);
  }
}

function selectProject(slug, preferredIteration) {
  selectedProject = slug;
  if (!preferredIteration) selectedIteration = null;
  iterationsCache = [];
  $("board-slug").textContent = slug ? `· ${slug}` : "";
  $("iteration-bar").classList.toggle("hidden", !slug);
  updateIterationActions();
  loadProjects();
  loadIterations(preferredIteration).then(() => loadBoard());
}

async function loadIterations(preferredId) {
  const select = $("iteration-select");
  if (!selectedProject) {
    select.replaceChildren();
    iterationsCache = [];
    selectedIteration = null;
    updateIterationActions();
    return;
  }
  let data;
  try {
    data = await api(`/api/v1/projects/${encodeURIComponent(selectedProject)}/iterations`);
  } catch (e) {
    return;
  }
  iterationsCache = data.iterations || [];
  select.replaceChildren();
  for (const it of iterationsCache) {
    const opt = document.createElement("option");
    opt.value = it.id;
    opt.textContent = `${it.title} (${it.state})`;
    select.appendChild(opt);
  }
  const locked = orchestrating || statePlanning;
  const keepSelection =
    selectedIteration && iterationsCache.some((i) => i.id === selectedIteration)
      ? selectedIteration
      : null;
  const pick =
    preferredId ||
    keepSelection ||
    (locked && activeIteration) ||
    iterationsCache.find((i) => i.state === "planning")?.id ||
    iterationsCache[iterationsCache.length - 1]?.id ||
    null;
  if (pick && iterationsCache.some((i) => i.id === pick)) {
    selectedIteration = pick;
    select.value = pick;
  } else if (iterationsCache.length) {
    selectedIteration = iterationsCache[iterationsCache.length - 1].id;
    select.value = selectedIteration;
  } else {
    selectedIteration = null;
  }
  updateIterationActions();
}

function currentIteration() {
  return iterationsCache.find((i) => i.id === selectedIteration) || null;
}

function syncIterationSelectLabels() {
  const select = $("iteration-select");
  if (!select) return;
  for (const opt of select.options) {
    const it = iterationsCache.find((i) => i.id === opt.value);
    if (it) opt.textContent = `${it.title} (${it.state})`;
  }
}

function patchIterationCache(iteration) {
  if (!iteration || !iteration.id) return;
  const idx = iterationsCache.findIndex((i) => i.id === iteration.id);
  if (idx >= 0) iterationsCache[idx] = { ...iterationsCache[idx], ...iteration };
  else iterationsCache.push(iteration);
}

function updateIterationActions() {
  const iter = currentIteration();
  const locked = orchestrating || statePlanning;
  const canNewIteration =
    !!selectedProject &&
    !locked &&
    !iterationsCache.some((i) => i.state === "planning" || i.state === "running" || i.state === "testing");
  $("toggle-new-iteration").disabled = !canNewIteration;
  $("toggle-new-task").disabled = !iter || locked || iter.state !== "planning";
  $("toggle-new-bug").disabled = !iter || locked || iter.state !== "completed";
  const hasTesting = !!(iter && (iter.testing_instructions || "").trim());
  $("toggle-testing-instructions").disabled = !iter || locked || iter.state !== "planning";
  $("toggle-testing-instructions").classList.toggle("has-instructions", hasTesting);
  renderTestingInstructions();
  const badge = $("iteration-state");
  if (!iter) {
    badge.textContent = "—";
    badge.className = "pill iter-planning";
    return;
  }
  badge.textContent = iter.state;
  badge.className = `pill iter-${iter.state}`;
  syncIterationSelectLabels();
}

function openIterationModal() {
  if (!selectedProject) return;
  const n = iterationsCache.length + 1;
  $("iter-title").value = `Iteration ${n}`;
  $("iter-error").textContent = "";
  $("iteration-modal").classList.remove("hidden");
  $("iter-title").focus();
}

function closeIterationModal() {
  $("iteration-modal").classList.add("hidden");
  $("iter-error").textContent = "";
}

function resetProjectForm() {
  editingSlug = null;
  $("np-title").value = "";
  $("np-slug").value = "";
  $("np-slug").disabled = false;
  $("np-slug").placeholder = "project-slug (required, one word)";
  $("np-desc").value = "";
  $("np-git").checked = false;
  setSelectedWorkspace("");
  $("np-error").textContent = "";
  $("np-submit").textContent = "Create project";
  $("np-modal-title").textContent = "New project";
}

function openProjectModal() {
  $("project-modal").classList.remove("hidden");
  $("np-title").focus();
}

function closeProjectModal() {
  $("project-modal").classList.add("hidden");
  $("np-error").textContent = "";
  const card = $("project-modal").querySelector(".modal-card");
  setModalBusy(card, $("project-modal-loader"), $("project-modal-loader-text"), false);
}

function startEditProject(slug) {
  const p = projectsCache.find((x) => x.slug === slug);
  if (!p) return;
  editingSlug = slug;
  $("np-title").value = p.title || "";
  $("np-slug").value = p.slug;
  $("np-slug").disabled = true;            // slug is the identity; not editable
  $("np-desc").value = p.description || "";
  $("np-git").checked = !!p.needs_git;
  setSelectedWorkspace(p.workspace_path || "");
  $("np-error").textContent = "";
  $("np-submit").textContent = "Save changes";
  $("np-modal-title").textContent = "Edit project";
  openProjectModal();
}

function projectDirLabel(proj, slug) {
  const s = slug || (proj && proj.slug) || "";
  if (proj && proj.workspace_path) return esc(proj.workspace_path);
  return `WORKSPACES/${esc(s)}/`;
}

function projectDirPlain(proj, slug) {
  const s = slug || (proj && proj.slug) || "";
  if (proj && proj.workspace_path) return proj.workspace_path;
  return `WORKSPACES/${s}/`;
}

// --- workspace folder picker (server-side directories) ---
let dirBrowsePath = null;
let selectedWorkspacePath = "";

function setSelectedWorkspace(path) {
  selectedWorkspacePath = (path || "").trim();
  syncWorkspaceField();
}

function syncWorkspaceField() {
  const input = $("np-workspace");
  const clearBtn = $("np-workspace-clear");
  if (selectedWorkspacePath) {
    input.value = selectedWorkspacePath;
    clearBtn.classList.remove("hidden");
  } else {
    input.value = "";
    const slug = slugify($("np-slug").value);
    input.placeholder = slug ? `Default: WORKSPACES/${slug}/` : "Default: WORKSPACES/<slug>/";
    clearBtn.classList.add("hidden");
  }
}

async function loadDirBrowse(path) {
  const q = path ? `?path=${encodeURIComponent(path)}` : "";
  const data = await api(`/api/v1/fs/directories${q}`);
  dirBrowsePath = data.path;
  $("dir-current").textContent = data.path;
  $("dir-up").disabled = !data.parent;
  const list = $("dir-list");
  list.replaceChildren();
  if (!data.directories.length) {
    const empty = document.createElement("div");
    empty.className = "dir-empty";
    empty.textContent = "No subfolders — you can select this folder.";
    list.appendChild(empty);
    return;
  }
  for (const d of data.directories) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "dir-item";
    btn.textContent = d.name;
    btn.addEventListener("click", () => loadDirBrowse(d.path));
    list.appendChild(btn);
  }
}

function openDirModal() {
  $("dir-modal").classList.remove("hidden");
  loadDirBrowse(selectedWorkspacePath || null).catch((e) => {
    $("dir-list").innerHTML = `<div class="dir-empty">${esc(e.message)}</div>`;
  });
}

function closeDirModal() {
  $("dir-modal").classList.add("hidden");
}

async function deleteProject(slug) {
  const ok = await confirmModal({
    title: "Delete project",
    message: `Delete project "${slug}" and all its tasks? This cannot be undone.`,
    confirmLabel: "Delete",
    danger: true,
  });
  if (!ok) return;
  try {
    await api(`/api/v1/projects/${encodeURIComponent(slug)}`, { method: "DELETE" });
  } catch (e) {
    await alertModal({ title: "Could not delete", message: e.message });
    return;
  }
  if (selectedProject === slug) {
    selectedProject = null;
    selectedIteration = null;
    $("board-slug").textContent = "";
    $("iteration-bar").classList.add("hidden");
    updateIterationActions();
  }
  if (editingSlug === slug) { resetProjectForm(); closeProjectModal(); }
  await loadProjects();
  await loadBoard();
  poll();
}

async function deleteTask(task) {
  if (!selectedProject || !task) return;
  const ok = await confirmModal({
    title: "Delete task",
    message: `Delete "${task.identifier}: ${task.title}"? This cannot be undone.`,
    confirmLabel: "Delete",
    danger: true,
  });
  if (!ok) return;
  try {
    await api(
      `/api/v1/projects/${encodeURIComponent(selectedProject)}/tasks/${encodeURIComponent(task.id)}`,
      { method: "DELETE" }
    );
  } catch (e) {
    await alertModal({ title: "Could not delete task", message: e.message });
    return;
  }
  if (editingTaskId === task.id) closeTaskModal();
  await loadBoard();
  await loadProjects();
  poll();
}

function downstreamPlanningIteration(afterIterationId) {
  const sorted = [...iterationsCache].sort((a, b) => (a.seq || 0) - (b.seq || 0));
  const idx = sorted.findIndex((i) => i.id === afterIterationId);
  if (idx < 0) return null;
  return sorted.slice(idx + 1).find((i) => i.state === "planning") || null;
}

function downstreamCopyTargetLabel(iter) {
  if (!iter) return null;
  const downstream = downstreamPlanningIteration(iter.id);
  if (downstream) return downstream.title;
  if (iter.state === "completed") return `Iteration ${(iter.seq || 0) + 1}`;
  return null;
}

function canCopyDownstream(task, columnName) {
  if ((task.kind || "task") === "bug") return false;
  if (columnName !== "Completed") return false;
  const iter = currentIteration();
  if (!iter || orchestrating || statePlanning) return false;
  if (downstreamPlanningIteration(iter.id)) return true;
  return iter.state === "completed";
}

function closeAllTaskMenus() {
  document.querySelectorAll(".task-menu.open").forEach((m) => m.classList.remove("open"));
}

async function copyTaskDownstream(task) {
  closeAllTaskMenus();
  if (!selectedProject || !task) return;
  const targetLabel = downstreamCopyTargetLabel(currentIteration()) || "the next iteration";
  const ok = await confirmModal({
    title: "Copy to next iteration",
    html: true,
    message: `Copy <span class="mono">${esc(task.identifier)}</span> into <strong>${esc(targetLabel)}</strong> as a new Todo task? The original stays in this iteration.`,
    confirmLabel: "Copy task",
  });
  if (!ok) return;
  try {
    const result = await api(
      `/api/v1/projects/${encodeURIComponent(selectedProject)}/tasks/${encodeURIComponent(task.id)}/copy-downstream`,
      { method: "POST" }
    );
    if (result.created_iteration) await loadIterations(result.iteration.id);
    else await loadIterations();
    await loadBoard();
    await loadProjects();
    await alertModal({
      title: "Task copied",
      html: true,
      message: `Created <span class="mono">${esc(result.task.identifier)}</span> in ${esc(result.iteration.title)}.`,
    });
  } catch (e) {
    await alertModal({ title: "Could not copy task", message: e.message });
  }
}

function canMoveTask(fromState, toState) {
  if (!fromState || !toState || fromState === toState) return false;
  if (TERMINAL_COLUMNS.has(fromState) && BACKWARD_COLUMNS.has(toState)) return false;
  return true;
}

// --- board (Mission Control) ---
async function loadBoard() {
  const board = $("board");
  if (!selectedProject || !selectedIteration) {
    board.replaceChildren();
    const hint = document.createElement("div");
    hint.className = "board-hint";
    hint.textContent = selectedProject
      ? "Create or select an iteration to view its board."
      : "Select a project to view its sprint board.";
    board.appendChild(hint);
    return;
  }
  let data;
  try {
    const q = `?iteration=${encodeURIComponent(selectedIteration)}`;
    data = await api(`/api/v1/projects/${encodeURIComponent(selectedProject)}/board${q}`);
  } catch (e) { return; }
  if (data.iteration) {
    patchIterationCache(data.iteration);
    updateIterationActions();
  }
  const columns = data.columns || {};
  const planRankMap = buildPlanRankMap(columns);
  board.replaceChildren();
  for (const name of BOARD_RENDER_ORDER) {
    if (name === "__testing__") {
      board.appendChild(buildTestingColumn());
      renderTestingDock(lastApiState);
      renderTestingInstructions();
      continue;
    }
    const tasks = columns[name] || [];
    if (name === "Cancelled" && !tasks.length) continue;
    const col = document.createElement("div");
    col.className = "column col-" + name.replace(/\s+/g, "-").toLowerCase();
    col.dataset.state = name;
    col.innerHTML = `<div class="col-head">${name}<span class="count">${tasks.length}</span></div>`;
    const body = document.createElement("div");
    body.className = "col-body";
    for (const t of tasks) {
      const card = document.createElement("div");
      card.className = "task-card";
      const isTerminal = TERMINAL_COLUMNS.has(name);
      card.draggable = !isTerminal;
      card.dataset.taskId = t.id;
      card.dataset.state = name;
      const copyEnabled = canCopyDownstream(t, name);
      const copyTarget = downstreamCopyTargetLabel(currentIteration());
      const copyLabel = copyTarget
        ? `Copy to ${copyTarget}`
        : "Copy to next iteration";
      const desc = t.description ? `<div class="tc-desc">${esc(t.description)}</div>` : "";
      const agent = t.agent_name ? `<span class="agent-tag">${esc(t.agent_name)}</span>` : "";
      const role = t.role ? `<span class="role-tag role-${esc(t.role)}">${esc(t.role)}</span>` : "";
      const kind = (t.kind || "task") === "bug"
        ? `<span class="role-tag kind-bug">bug</span>`
        : "";
      const deps = (t.deps || []).map((d) => esc(d.identifier)).filter(Boolean);
      const sub = t.subdir ? `<span class="sub-tag mono">${esc(t.subdir)}/</span>` : "";
      const depHint = deps.length ? `<div class="tc-deps">after ${deps.join(", ")}</div>` : "";
      card.innerHTML = `
        <div class="tc-head">
          <span class="mono tc-id">${esc(t.identifier)}</span>
          <span class="tc-head-right">${kind}${role}${agent}</span>
        </div>
        <div class="tc-title">${esc(t.title)}</div>${desc}
        <div class="tc-meta">${sub}${depHint}</div>
        <div class="tc-bottom">
        ${cardStatusFooter(columnStatusKind(name), planRankMap.get(t.id))}
        <div class="tc-actions">
          <div class="task-menu-wrap">
            <button type="button" class="menu-btn" title="Task actions" aria-label="Task actions" aria-haspopup="true">${MENU_ICON}</button>
            <div class="task-menu" role="menu">
              <button type="button" class="task-menu-item" data-action="copy-downstream" ${copyEnabled ? "" : "disabled"}>${esc(copyLabel)}</button>
            </div>
          </div>
          <button type="button" class="edit-btn" title="Edit task" aria-label="Edit task">${EDIT_ICON}</button>
          <button type="button" class="copy-btn" title="Copy task details" aria-label="Copy task details">${COPY_ICON}</button>
          <button type="button" class="del-btn" title="Delete task" aria-label="Delete task">${DELETE_ICON}</button>
        </div>
        </div>`;
      const menuWrap = card.querySelector(".task-menu-wrap");
      const menuBtn = card.querySelector(".menu-btn");
      const menu = card.querySelector(".task-menu");
      const copyDownBtn = card.querySelector('[data-action="copy-downstream"]');
      menuBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const wasOpen = menu.classList.contains("open");
        closeAllTaskMenus();
        if (!wasOpen) menu.classList.add("open");
      });
      menuBtn.addEventListener("mousedown", (e) => e.stopPropagation());
      menuBtn.addEventListener("dragstart", (e) => e.preventDefault());
      copyDownBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (!copyDownBtn.disabled) copyTaskDownstream(t);
      });
      copyDownBtn.addEventListener("mousedown", (e) => e.stopPropagation());
      copyDownBtn.addEventListener("dragstart", (e) => e.preventDefault());
      const editBtn = card.querySelector(".edit-btn");
      editBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        startEditTask(t);
      });
      editBtn.addEventListener("mousedown", (e) => e.stopPropagation());
      editBtn.addEventListener("dragstart", (e) => e.preventDefault());
      const copyBtn = card.querySelector(".copy-btn");
      copyBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        copyText(taskCopyText(t), copyBtn);
      });
      copyBtn.addEventListener("mousedown", (e) => e.stopPropagation());
      copyBtn.addEventListener("dragstart", (e) => e.preventDefault());
      const delBtn = card.querySelector(".del-btn");
      delBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteTask(t);
      });
      delBtn.addEventListener("mousedown", (e) => e.stopPropagation());
      delBtn.addEventListener("dragstart", (e) => e.preventDefault());
      if (!isTerminal) {
        card.addEventListener("dragstart", (e) => {
          draggingTaskId = t.id;
          draggingFrom = name;
          e.dataTransfer.effectAllowed = "move";
          e.dataTransfer.setData("text/plain", t.id);
          card.classList.add("dragging");
          closeAllTaskMenus();
        });
        card.addEventListener("dragend", () => {
          card.classList.remove("dragging");
          draggingTaskId = null;
          draggingFrom = null;
        });
      }
      body.appendChild(card);
    }
    if (!tasks.length) {
      const e = document.createElement("div");
      e.className = "col-empty";
      e.textContent = "—";
      body.appendChild(e);
    }
    col.appendChild(body);
    col.addEventListener("dragover", (e) => {
      if (!draggingTaskId || !draggingFrom) return;
      if (!canMoveTask(draggingFrom, name)) {
        e.dataTransfer.dropEffect = "none";
        return;
      }
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      col.classList.add("drag-over");
    });
    col.addEventListener("dragleave", (e) => {
      if (!col.contains(e.relatedTarget)) col.classList.remove("drag-over");
    });
    col.addEventListener("drop", (e) => {
      e.preventDefault();
      col.classList.remove("drag-over");
      const id = draggingTaskId || e.dataTransfer.getData("text/plain");
      if (!canMoveTask(draggingFrom, name)) return;
      moveTask(id, draggingFrom, name);
    });
    board.appendChild(col);
  }
}

const QA_ICON_IDLE = `<svg class="ta-svg" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 2a7 7 0 0 0-7 7v3.1L3.3 16.5A1 1 0 0 0 4.2 18h15.6a1 1 0 0 0 .9-1.5L19 12.1V9a7 7 0 0 0-7-7zm0 2a5 5 0 0 1 5 5v3.08l1.2 2H5.8l1.2-2V9a5 5 0 0 1 5-5zm-1 16h2v2h-2z"/></svg>`;
const QA_ICON_STANDBY = `<svg class="ta-svg" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67z"/></svg>`;

function buildTestingColumn() {
  const col = document.createElement("div");
  col.className = "column col-testing testing-dock";
  col.id = "testing-dock";
  col.innerHTML = `
    <div class="col-head">Verification<span class="count qa-badge">QA</span></div>
    <div class="col-body">
      <div id="testing-agent-card" class="testing-agent-card idle">
        <div id="testing-agent-icon" class="ta-icon">${QA_ICON_IDLE}</div>
        <div class="ta-title">QA Agent</div>
        <div id="testing-status-label" class="ta-status">Idle</div>
        <div id="testing-status-detail" class="ta-detail muted">Adversarial reviewer — activates after all tasks complete.</div>
      </div>
      <div id="qa-instructions-panel" class="qa-instructions-panel">
        <div class="qa-instructions-head">
          <span>Testing instructions</span>
          <button type="button" id="qa-instructions-edit" class="link qa-instructions-edit">Edit</button>
        </div>
        <div id="qa-instructions-body" class="qa-instructions-body empty">No testing instructions yet.</div>
      </div>
    </div>`;
  col.querySelector("#qa-instructions-edit").addEventListener("click", (e) => {
    e.stopPropagation();
    openTestingModal();
  });
  return col;
}

function renderTestingInstructions() {
  const panel = $("qa-instructions-panel");
  const body = $("qa-instructions-body");
  const editBtn = $("qa-instructions-edit");
  if (!panel || !body) return;

  const iter = currentIteration();
  if (!selectedProject || !iter) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");

  const text = (iter.testing_instructions || "").trim();
  const locked = orchestrating || statePlanning;
  const canEdit = iter.state === "planning" && !locked;

  if (text) {
    body.textContent = text;
    body.classList.remove("empty");
  } else {
    body.textContent = "No testing instructions yet. Add instructions before orchestrating.";
    body.classList.add("empty");
  }

  if (editBtn) {
    editBtn.disabled = !canEdit;
    editBtn.textContent = text ? "Edit" : "Add";
    editBtn.title = canEdit
      ? "Edit QA agent instructions"
      : iter.state !== "planning"
        ? "Instructions can only be edited while the iteration is in planning"
        : "Stop orchestration to edit instructions";
  }
}

function renderTestingDock(state) {
  const card = $("testing-agent-card");
  if (!card) return;
  const testing = (state && state.testing) || {};
  const status = testing.status || "idle";
  const scoped = state && selectedProject && state.active_project === selectedProject;
  const label = $("testing-status-label");
  const detail = $("testing-status-detail");
  const icon = $("testing-agent-icon");
  card.className = `testing-agent-card ${status}`;
  if ((!scoped && !orchestrating) && status !== "passed") {
    card.className = "testing-agent-card idle";
    if (label) label.textContent = "Idle";
    if (detail) detail.textContent = "Adversarial reviewer — activates after all tasks complete.";
    if (icon) icon.innerHTML = QA_ICON_IDLE;
    applyCardStatusIndicator(card, "");
    return;
  }
  const labels = {
    idle: "Idle",
    standby: "Standing by",
    running: "Reviewing sprint",
    passed: "Sprint accepted",
  };
  if (label) label.textContent = labels[status] || status;
  if (detail) {
    if (status === "running") {
      detail.textContent = testing.last_event
        ? `Working · ${testing.last_event}`
        : (testing.last_message || "Running checks…");
    } else if (status === "standby") {
      detail.textContent = "Build agents are still working — QA waits its turn.";
    } else if (status === "passed") {
      detail.textContent = testing.last_message || "Quality gate passed.";
    } else {
      detail.textContent = testing.last_message || "Adversarial reviewer — activates after all tasks complete.";
    }
  }
  if (icon) {
    if (status === "running") {
      icon.innerHTML = `<div class="spinner ta-spinner" aria-hidden="true"></div>`;
    } else if (status === "standby") {
      icon.innerHTML = QA_ICON_STANDBY;
    } else if (status === "passed") {
      icon.innerHTML = STATUS_TICK_SVG;
    } else {
      icon.innerHTML = QA_ICON_IDLE;
    }
  }
  const indicatorKind = (!scoped && !orchestrating) && status !== "passed"
    ? ""
    : testingStatusKind((!scoped && !orchestrating) ? "idle" : status);
  applyCardStatusIndicator(card, indicatorKind);
}

function openTestingModal() {
  if (!selectedProject || !selectedIteration) return;
  const iter = currentIteration();
  $("qa-instructions").value = (iter && iter.testing_instructions) || "";
  $("qa-error").textContent = "";
  $("testing-modal").classList.remove("hidden");
  $("qa-instructions").focus();
}

function closeTestingModal() {
  $("testing-modal").classList.add("hidden");
  $("qa-error").textContent = "";
}

async function moveTask(taskId, fromState, toState) {
  if (!taskId || !selectedProject || fromState === toState) return;
  if (!canMoveTask(fromState, toState)) {
    await alertModal({
      title: "Move not allowed",
      message: "Completed tasks cannot be moved back to Todo or In Progress.",
    });
    return;
  }
  try {
    await api(`/api/v1/projects/${encodeURIComponent(selectedProject)}/tasks/${encodeURIComponent(taskId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state: toState }),
    });
  } catch (e) {
    await alertModal({ title: "Could not move task", message: e.message });
    await loadBoard();
    return;
  }
  await loadBoard();
  await loadProjects();
}

// --- artifacts (files produced by agents in WORKSPACES/<slug>/) ---
function fmtBytes(n) {
  n = n || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

async function loadArtifactProjects() {
  let projects = projectsCache;
  if (!projects.length) {
    try { projects = (await api("/api/v1/projects")).projects || []; projectsCache = projects; } catch (e) { return; }
  }
  const select = $("art-project");
  const prev = select.value;
  select.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = projects.length ? "Select Project" : "No projects yet";
  select.appendChild(placeholder);
  if (!projects.length) {
    $("art-files").replaceChildren();
    $("art-files").innerHTML = '<div class="empty-note">No projects yet.</div>';
    return;
  }
  for (const p of projects) {
    const opt = document.createElement("option");
    opt.value = p.slug;
    opt.textContent = `${p.title} (${p.slug})`;
    select.appendChild(opt);
  }
  const pick = (prev && projects.some((p) => p.slug === prev)) ? prev
    : (artifactProject && projects.some((p) => p.slug === artifactProject)) ? artifactProject
    : "";
  select.value = pick;
  updateArtifactBanner(pick);
  loadArtifactFiles(pick);
}

function updateArtifactBanner(slug) {
  const banner = $("art-banner");
  if (!banner) return;
  if (!slug) {
    banner.innerHTML = "Select a project to browse files agents created in its workspace.";
    return;
  }
  const proj = projectsCache.find((p) => p.slug === slug);
  const name = proj ? esc(proj.title) : esc(slug);
  const path = projectDirLabel(proj, slug);
  banner.innerHTML = `Files agents created for <strong>${name}</strong> under <span class="mono">${path}</span>.`;
}

function updateArtConsoleChrome(slug) {
  const input = $("art-console-input");
  const clearBtn = $("art-console-clear");
  const cwdEl = $("art-console-cwd");
  const enabled = !!slug;
  if (input) {
    input.disabled = !enabled;
    input.placeholder = enabled
      ? "Run a command in the project workspace…"
      : "Select a project first…";
  }
  if (clearBtn) clearBtn.disabled = !enabled;
  if (cwdEl) {
    if (!slug) cwdEl.textContent = "Select a project to run commands.";
    else {
      const proj = projectsCache.find((p) => p.slug === slug);
      cwdEl.textContent = projectDirPlain(proj, slug);
    }
  }
}

function appendArtConsole(text, cls) {
  const out = $("art-console-output");
  if (!out || !text) return;
  const block = document.createElement("div");
  block.className = cls ? `art-console-line ${cls}` : "art-console-line";
  block.textContent = text;
  out.appendChild(block);
  out.scrollTop = out.scrollHeight;
}

function clearArtConsoleOutput() {
  const out = $("art-console-output");
  if (out) out.replaceChildren();
}

async function runArtConsoleCommand(command) {
  if (!artifactProject || !command) return;
  const input = $("art-console-input");
  appendArtConsole(`$ ${command}`, "cmd");
  if (input) input.disabled = true;
  try {
    const res = await api(
      `/api/v1/projects/${encodeURIComponent(artifactProject)}/console`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command }),
      }
    );
    if (res.cwd) $("art-console-cwd").textContent = res.cwd;
    const outText = (res.stdout || "").trimEnd();
    const errText = (res.stderr || "").trimEnd();
    if (outText) appendArtConsole(outText);
    if (errText) appendArtConsole(errText, "err");
    const meta = [`exit ${res.exit_code}`];
    if (res.timed_out) meta.push("timed out");
    if (res.truncated) meta.push("output truncated");
    appendArtConsole(`[${meta.join(" · ")}]`, "meta");
    await loadArtifactFiles(artifactProject);
  } catch (e) {
    appendArtConsole(e.message || String(e), "err");
  } finally {
    if (input) {
      input.disabled = false;
      input.focus();
    }
  }
}

async function loadArtifactFiles(slug) {
  artifactProject = slug;
  updateArtifactBanner(slug);
  updateArtConsoleChrome(slug);
  const newBtn = $("art-new-file");
  if (newBtn) newBtn.disabled = !slug;
  const list = $("art-files");
  if (!slug) { list.innerHTML = '<div class="empty-note">Select a project.</div>'; return; }
  let data;
  try { data = await api(`/api/v1/projects/${encodeURIComponent(slug)}/files`); }
  catch (e) { list.innerHTML = '<div class="empty-note">Could not load files.</div>'; return; }
  const files = data.files || [];
  $("art-count").textContent = files.length ? `· ${files.length}` : "";
  list.replaceChildren();
  if (!files.length) {
    list.innerHTML = '<div class="empty-note">No files yet. Agents will populate this once they run.</div>';
    return;
  }
  // group by top-level folder (the task identifier)
  const groups = {};
  for (const f of files) {
    const slash = f.path.indexOf("/");
    const group = slash === -1 ? "(root)" : f.path.slice(0, slash);
    (groups[group] = groups[group] || []).push(f);
  }
  for (const group of Object.keys(groups).sort()) {
    const g = document.createElement("div");
    g.className = "file-group";
    g.innerHTML = `<div class="fg-head mono">${esc(group)}</div>`;
    for (const f of groups[group]) {
      const name = f.path.includes("/") ? f.path.slice(f.path.indexOf("/") + 1) : f.path;
      const item = document.createElement("button");
      item.type = "button";
      item.className = "file-item" + (f.path === artifactFile ? " selected" : "");
      item.innerHTML = `<span class="fi-name mono">${esc(name)}</span><span class="fi-size">${fmtBytes(f.size)}</span>`;
      item.addEventListener("click", () => openArtifactFile(slug, f.path));
      g.appendChild(item);
    }
    list.appendChild(g);
  }
}

async function openArtifactFile(slug, path) {
  artifactFile = path;
  artifactEditing = false;
  artifactCanEdit = false;
  setArtifactEditMode(false);
  setArtifactStatus("");
  document.querySelectorAll("#art-files .file-item").forEach((el) =>
    el.classList.toggle("selected", el.querySelector(".fi-name").textContent ===
      (path.includes("/") ? path.slice(path.indexOf("/") + 1) : path)));
  $("art-file-name").textContent = path;
  $("art-file-meta").textContent = "loading…";
  $("art-file-body").value = "";
  let data;
  try { data = await api(`/api/v1/projects/${encodeURIComponent(slug)}/file?path=${encodeURIComponent(path)}`); }
  catch (e) {
    $("art-file-meta").textContent = "";
    $("art-file-body").value = "Could not open file.";
    $("art-file-actions").classList.add("hidden");
    return;
  }
  const parts = [fmtBytes(data.size)];
  if (data.truncated) parts.push("truncated");
  $("art-file-meta").textContent = parts.join(" · ");
  if (data.binary) {
    $("art-file-body").value = "(binary file — preview and edit not available)";
    $("art-file-actions").classList.add("hidden");
    artifactOriginal = "";
    return;
  }
  if (data.truncated) {
    $("art-file-body").value = data.content || "";
    $("art-file-actions").classList.add("hidden");
    setArtifactStatus("File is too large to edit here (256 KB limit).", true);
    artifactOriginal = data.content || "";
    return;
  }
  const text = data.content ?? "";
  artifactOriginal = text;
  artifactCanEdit = data.editable !== false;
  $("art-file-body").value = text || "(empty file)";
  $("art-file-actions").classList.toggle("hidden", !artifactCanEdit);
  $("art-edit").classList.remove("hidden");
  $("art-save").classList.add("hidden");
  $("art-cancel").classList.add("hidden");
}

function setArtifactEditMode(editing) {
  artifactEditing = editing;
  const body = $("art-file-body");
  body.readOnly = !editing;
  $("art-edit").classList.toggle("hidden", editing || !artifactCanEdit);
  $("art-save").classList.toggle("hidden", !editing);
  $("art-cancel").classList.toggle("hidden", !editing);
  if (editing) body.focus();
}

function setArtifactStatus(msg, isError = false) {
  const el = $("art-file-status");
  el.textContent = msg || "";
  el.classList.toggle("ok", !!msg && !isError);
  el.classList.toggle("err", !!msg && isError);
}

async function saveArtifactFile() {
  if (!artifactProject || !artifactFile || !artifactCanEdit) return;
  const content = $("art-file-body").value;
  $("art-save").disabled = true;
  setArtifactStatus("Saving…");
  try {
    const data = await api(`/api/v1/projects/${encodeURIComponent(artifactProject)}/file`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: artifactFile, content }),
    });
    artifactOriginal = content;
    setArtifactEditMode(false);
    $("art-file-meta").textContent = fmtBytes(data.size);
    setArtifactStatus("Saved.");
    await loadArtifactFiles(artifactProject);
  } catch (e) {
    setArtifactStatus("Save failed: " + e.message, true);
  } finally {
    $("art-save").disabled = false;
  }
}

function cancelArtifactEdit() {
  $("art-file-body").value = artifactOriginal;
  setArtifactEditMode(false);
  setArtifactStatus("");
}

// --- git history ---
let gitProject = null;

async function loadGitProjects() {
  let projects = projectsCache;
  if (!projects.length) {
    try { projects = (await api("/api/v1/projects")).projects || []; projectsCache = projects; } catch (e) { return; }
  }
  const gitProjects = projects.filter((p) => p.needs_git);
  const select = $("git-project");
  const prev = select.value;
  select.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = gitProjects.length ? "Select Project" : "No Git-enabled projects";
  select.appendChild(placeholder);
  for (const p of gitProjects) {
    const opt = document.createElement("option");
    opt.value = p.slug;
    opt.textContent = `${p.title} (${p.slug})`;
    select.appendChild(opt);
  }
  const pick = (prev && gitProjects.some((p) => p.slug === prev)) ? prev
    : (gitProject && gitProjects.some((p) => p.slug === gitProject)) ? gitProject
    : "";
  select.value = pick;
  updateGitBanner(pick);
  loadGitCommits(pick);
}

function updateGitBanner(slug) {
  const banner = $("git-banner");
  if (!slug) {
    banner.innerHTML = "Select a project to view commits made after each sprint.";
    return;
  }
  const proj = projectsCache.find((p) => p.slug === slug);
  const name = proj ? esc(proj.title) : esc(slug);
  const path = projectDirLabel(proj, slug);
  banner.innerHTML = `Commits Flight Deck made for <strong>${name}</strong> in <span class="mono">${path}</span> after each sprint.`;
}

async function loadGitCommits(slug) {
  gitProject = slug;
  updateGitBanner(slug);
  const body = $("git-body");
  body.replaceChildren();
  if (!slug) {
    body.appendChild(emptyRow(4, "Select a Git-enabled project to view commits."));
    $("git-count").textContent = "";
    return;
  }
  let data;
  try { data = await api(`/api/v1/projects/${encodeURIComponent(slug)}/commits`); }
  catch (e) { body.appendChild(emptyRow(4, "Could not load commits.")); return; }
  const commits = data.commits || [];
  $("git-count").textContent = commits.length ? `· ${commits.length}` : "";
  if (!commits.length) {
    body.appendChild(emptyRow(4, "No commits yet — they appear after a sprint finishes."));
    return;
  }
  for (const c of commits) {
    body.appendChild(
      row([
        { html: `<span class="mono">${esc((c.hash || "").slice(0, 8))}</span>`, cls: "mono" },
        c.author || "—",
        fmtTime(c.date),
        c.subject || "—",
      ])
    );
  }
}

// --- execution monitor (Operations) ---
function renderRunning(rows) {
  const body = $("running-body");
  body.replaceChildren();
  if (!rows.length) { body.appendChild(emptyRow(8, "No running sessions")); return; }
  for (const r of rows) {
    const issueCell = r.issue_url
      ? { html: `<a href="${esc(r.issue_url)}" target="_blank" rel="noopener">${esc(r.issue_identifier)}</a>` }
      : r.issue_identifier;
    body.appendChild(
      row(
        [
          issueCell,
          { html: r.agent_name ? `<span class="agent-tag">${esc(r.agent_name)}</span>` : "—" },
          { html: `<span class="pill">${esc(r.state || "—")}</span>` },
          { html: `<span class="mono">${esc(r.session_id || "—")}</span>`, cls: "mono" },
          r.turn_count,
          r.last_event || "—",
          fmtTime(r.started_at),
          { html: `<span class="num">${fmtNum(r.tokens && r.tokens.total_tokens)}</span>`, cls: "num" },
        ],
        () => showDetail(r.issue_identifier)
      )
    );
  }
}

function renderRetry(rows) {
  const body = $("retry-body");
  body.replaceChildren();
  if (!rows.length) { body.appendChild(emptyRow(4, "Retry queue empty")); return; }
  for (const r of rows) {
    body.appendChild(
      row([r.issue_identifier, r.attempt, fmtTime(r.due_at), r.error || "—"],
        () => showDetail(r.issue_identifier))
    );
  }
}

const OUTCOME_CLASS = {
  completed: "ok", normal: "ok", running: "prog",
  failed: "err", canceled: "muted", stopped: "muted", interrupted: "muted",
};

const RUNS_PER_PAGE = 5;
let runsCache = [];
let runsPage = 0;

async function loadRuns() {
  const slug = $("project-select").value;
  if (!slug) { runsCache = []; renderRuns(); return; }
  let data;
  try { data = await api(`/api/v1/projects/${encodeURIComponent(slug)}/runs?limit=200`); } catch (e) { return; }
  runsCache = data.runs || [];
  renderRuns();
}

function renderRuns() {
  const body = $("runs-body");
  const pager = $("runs-pager");
  body.replaceChildren();
  if (!runsCache.length) {
    const msg = $("project-select").value
      ? "No runs recorded yet for this project"
      : "Select a project to view its run history";
    body.appendChild(emptyRow(10, msg));
    pager.classList.add("hidden");
    return;
  }
  const pageCount = Math.ceil(runsCache.length / RUNS_PER_PAGE);
  if (runsPage > pageCount - 1) runsPage = pageCount - 1;
  if (runsPage < 0) runsPage = 0;
  const start = runsPage * RUNS_PER_PAGE;
  for (const r of runsCache.slice(start, start + RUNS_PER_PAGE)) {
    const cls = OUTCOME_CLASS[r.outcome] || "muted";
    const kind = (r.kind || "task") === "bug"
      ? `<span class="role-tag kind-bug">bug</span>`
      : `<span class="role-tag">task</span>`;
    body.appendChild(
      row([
        r.identifier || "—",
        r.iteration || "—",
        { html: kind },
        r.attempt,
        { html: `<span class="tag tag-${cls}">${esc(r.outcome)}</span>` },
        r.turn_count,
        { html: `<span class="num">${fmtNum(r.input_tokens)} / ${fmtNum(r.output_tokens)}</span>`, cls: "num" },
        { html: `<span class="num">${fmtNum(r.total_tokens)}</span>`, cls: "num" },
        { html: `<span class="num">${fmtRuntime(r.runtime_seconds)}</span>`, cls: "num" },
        fmtTime(r.finished_at),
      ])
    );
  }
  // pager
  pager.classList.toggle("hidden", pageCount <= 1);
  $("runs-page").textContent = `Page ${runsPage + 1} of ${pageCount} · ${runsCache.length} runs`;
  $("runs-prev").disabled = runsPage <= 0;
  $("runs-next").disabled = runsPage >= pageCount - 1;
}

function syncProjectSelectLock() {
  const select = $("project-select");
  if (!select) return;
  const lockedProject = (orchestrating || statePlanning) ? (activeProject || planningProject) : null;
  if (lockedProject) {
    if (select.querySelector(`option[value="${CSS.escape(lockedProject)}"]`)) select.value = lockedProject;
    select.disabled = true;
    select.title = `Locked while "${lockedProject}" is running — stop orchestration to switch`;
  } else {
    select.disabled = false;
    select.title = "Project to orchestrate";
  }
}

let statePlanning = false;
let planningProject = null;

function renderTotals(state) {
  const slug = $("project-select").value;
  const scoped = slug && state.active_project === slug;
  $("m-running").textContent = scoped ? state.counts.running : (slug ? 0 : state.counts.running);
  $("m-retrying").textContent = scoped ? state.counts.retrying : (slug ? 0 : state.counts.retrying);
  if (!slug) {
    $("m-in").textContent = "—";
    $("m-out").textContent = "—";
    $("m-total").textContent = "—";
    $("m-runtime").textContent = "—";
  } else {
    const t = state.project_totals || {};
    $("m-in").textContent = fmtNum(t.input_tokens);
    $("m-out").textContent = fmtNum(t.output_tokens);
    $("m-total").textContent = fmtNum(t.total_tokens);
    $("m-runtime").textContent = fmtRuntime(t.seconds_running);
  }
  $("generated").textContent = fmtTime(state.generated_at);
}

function renderOrchStatus(state) {
  const was = orchestrating;
  orchestrating = !!state.orchestrating;
  statePlanning = !!state.planning;
  planningProject = state.planning_project || null;
  activeProject = state.active_project || null;
  activeIteration = state.active_iteration || null;
  if (
    activeProject &&
    activeProject === selectedProject &&
    activeIteration &&
    (orchestrating || statePlanning)
  ) {
    selectedIteration = activeIteration;
    const sel = $("iteration-select");
    if (sel && sel.querySelector(`option[value="${CSS.escape(activeIteration)}"]`)) {
      sel.value = activeIteration;
    }
  }
  updateIterationActions();
  syncProjectSelectLock();

  const pill = $("orch-status");
  const banner = $("orch-banner");
  if (statePlanning) {
    pill.textContent = "planning…";
    pill.className = "pill running";
    banner.textContent = "Planner is sequencing the project's Todo tasks…";
    banner.className = "banner running";
    $("plan-orchestrate").classList.add("hidden");
    $("orchestrate").classList.add("hidden");
    $("stop").classList.remove("hidden");
  } else if (orchestrating) {
    const testing = state.testing || {};
    if (testing.status === "running") {
      pill.textContent = `testing · ${activeProject || "?"}`;
      banner.textContent = `QA agent is reviewing "${activeProject}" — adversarial verification in progress.`;
    } else {
      pill.textContent = `orchestrating · ${activeProject || "?"}`;
      banner.textContent = `Orchestrating "${activeProject}" — pulling Todo tasks and executing.`;
    }
    pill.className = "pill running";
    banner.className = "banner running";
    $("plan-orchestrate").classList.add("hidden");
    $("orchestrate").classList.add("hidden");
    $("stop").classList.remove("hidden");
  } else {
    pill.textContent = "idle";
    pill.className = "pill idle";
    banner.textContent = "Idle — select a project, then Plan & Orchestrate.";
    banner.className = "banner idle";
    $("plan-orchestrate").classList.remove("hidden");
    $("orchestrate").classList.remove("hidden");
    $("stop").classList.add("hidden");
  }
  if (was !== orchestrating || (orchestrating && selectedProject === activeProject)) loadBoard();
  if (was !== orchestrating && !orchestrating && selectedProject) loadIterations();
  updateIterationActions();
  renderTestingDock(state);
  renderTestingInstructions();
}

async function poll() {
  try {
    const slug = $("project-select").value;
    const q = slug ? `?project=${encodeURIComponent(slug)}` : "";
    const state = await api(`/api/v1/state${q}`);
    lastApiState = state;
    setConn(true);
    renderOrchStatus(state);
    renderTotals(state);
    const scoped = slug && state.active_project === slug;
    renderRunning(scoped ? (state.running || []) : (slug ? [] : (state.running || [])));
    renderRetry(scoped ? (state.retrying || []) : (slug ? [] : (state.retrying || [])));
    if (state.model_setup) updateActiveModelLabel(state.model_setup);
    loadRuns();
  } catch (e) {
    setConn(false);
    clientLog("error", "poll failed", { error: e.message || String(e) });
  }
}

async function showDetail(identifier) {
  const panel = $("detail");
  $("detail-title").textContent = identifier;
  $("detail-body").textContent = "loading…";
  panel.classList.remove("hidden");
  try {
    const data = await api(`/api/v1/${encodeURIComponent(identifier)}`);
    $("detail-body").textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    $("detail-body").textContent = "failed to load task detail";
  }
}

function startAuto() {
  stopAuto();
  timer = setInterval(poll, POLL_MS);
  boardTimer = setInterval(() => selectedProject && loadBoard(), POLL_MS * 2);
}
function stopAuto() {
  if (timer) clearInterval(timer);
  if (boardTimer) clearInterval(boardTimer);
  timer = null;
  boardTimer = null;
}

function updateActiveModelLabel(setup) {
  const el = $("active-model-label");
  if (!el || !setup) return;
  el.textContent = setup.configured ? setup.label : "";
  el.title = setup.configured
    ? (setup.model ? `Active model: ${setup.model}` : "Using Pi default model")
    : "";
}

function showAppShell() {
  $("model-setup-screen").classList.add("hidden");
  $("app-shell").classList.remove("hidden");
  modelSetupConfigured = true;
}

function showSetupScreen() {
  stopAuto();
  $("app-shell").classList.add("hidden");
  $("model-setup-screen").classList.remove("hidden");
  modelSetupConfigured = false;
  modelTestPassed = false;
  $("setup-continue").disabled = true;
  $("setup-reply").value = "";
  $("setup-error").textContent = "";
}

function selectedSetupModel() {
  const val = $("setup-model").value;
  if (val === "" || val === PI_DEFAULT_MODEL) return null;
  return val;
}

function resetSetupTestState() {
  modelTestPassed = false;
  $("setup-continue").disabled = true;
  $("setup-reply").value = "";
  $("setup-error").textContent = "";
}

function setSetupBusy(busy, message) {
  const card = $("model-setup-screen").querySelector(".setup-card");
  setModalBusy(card, $("setup-loader"), $("setup-loader-text"), busy, message || "Working…");
  $("setup-test").disabled = busy;
  $("setup-refresh-models").disabled = busy;
  $("setup-continue").disabled = busy || !modelTestPassed;
  $("setup-model").disabled = busy;
}

function fillSetupModelSelect(models, selected) {
  const sel = $("setup-model");
  sel.innerHTML = "";
  const def = document.createElement("option");
  def.value = PI_DEFAULT_MODEL;
  def.textContent = "Pi default (from Pi settings)";
  sel.appendChild(def);
  for (const m of models) {
    const opt = document.createElement("option");
    opt.value = m.ref;
    const suffix = m.is_default ? " — Pi default" : "";
    opt.textContent = `${m.label || m.ref}${suffix}`;
    sel.appendChild(opt);
  }
  const want = selected == null ? PI_DEFAULT_MODEL : selected;
  if ([...sel.options].some((o) => o.value === want)) {
    sel.value = want;
  } else {
    sel.value = PI_DEFAULT_MODEL;
  }
}

function mergeSetupModels(configured, saved) {
  const models = [...(configured || [])];
  const seen = new Set(models.map((m) => m.ref));
  if (saved?.model && !seen.has(saved.model)) {
    models.push({
      ref: saved.model,
      label: saved.label || saved.model,
    });
  }
  return models;
}

async function loadSetupModels(savedStatus) {
  $("setup-models-hint").textContent = "Loading configured models from Pi…";
  setSetupBusy(true, "Loading models…");
  try {
    const data = await api("/api/v1/models?scope=configured");
    const models = mergeSetupModels(data.models || [], savedStatus);
    fillSetupModelSelect(models, selectedSetupModel());
    if (data.error && !models.length) {
      $("setup-models-hint").textContent = data.error;
    } else if (!models.length) {
      $("setup-models-hint").textContent =
        "No configured Pi models — set a default in Pi or add entries to models.json, then Refresh.";
    } else {
      const defaultNote = data.default_ref ? ` Pi default: ${data.default_ref}.` : "";
      $("setup-models-hint").textContent =
        `${models.length} configured model(s) from Pi settings and models.json.${defaultNote}`;
    }
  } catch (err) {
    $("setup-models-hint").textContent = err.message;
  } finally {
    setSetupBusy(false);
  }
}

async function runSetupTest() {
  const prompt = $("setup-prompt").value.trim();
  if (!prompt) {
    $("setup-error").textContent = "Enter a test prompt.";
    return;
  }
  $("setup-error").textContent = "";
  $("setup-reply").value = "";
  modelTestPassed = false;
  $("setup-continue").disabled = true;
  setSetupBusy(true, "Testing model…");
  try {
    const body = { prompt, model: selectedSetupModel() };
    const data = await api("/api/v1/model-setup/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    $("setup-reply").value = data.reply || "";
    modelTestPassed = true;
    $("setup-continue").disabled = false;
  } catch (err) {
    $("setup-error").textContent = err.message;
  } finally {
    setSetupBusy(false);
  }
}

async function confirmSetup(e) {
  e.preventDefault();
  if (!modelTestPassed) {
    $("setup-error").textContent = "Run a successful test before continuing.";
    return;
  }
  setSetupBusy(true, "Saving…");
  try {
    const saved = await api("/api/v1/model-setup/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: selectedSetupModel() }),
    });
    updateActiveModelLabel(saved);
    showAppShell();
    loadProjects();
    poll();
    startAuto();
    clientLog("info", "Model setup saved", { model: saved.model, label: saved.label });
  } catch (err) {
    $("setup-error").textContent = err.message;
  } finally {
    setSetupBusy(false);
  }
}

async function logoutModel() {
  const ok = await confirmModal({
    title: "Log out",
    message: "Log out of the current model and reconfigure?",
    confirmLabel: "Log out",
    danger: false,
  });
  if (!ok) return;
  try {
    await api("/api/v1/model-setup/logout", { method: "POST" });
  } catch (err) {
    clientLog("error", "Model logout failed", { error: err.message });
  }
  showSetupScreen();
  await loadSetupModels();
}

async function bootstrapModelSetup() {
  setSetupBusy(true, "Checking configuration…");
  try {
    const status = await api("/api/v1/model-setup");
    updateActiveModelLabel(status);
    await loadSetupModels(status);
    if (status.configured) {
      showAppShell();
      loadProjects();
      poll();
      startAuto();
      clientLog("info", "Flight Deck dashboard loaded", { model: status.label });
    } else {
      showSetupScreen();
      clientLog("info", "Model setup required");
    }
  } catch (err) {
    showSetupScreen();
    $("setup-error").textContent = err.message;
  } finally {
    setSetupBusy(false);
  }
}

// --- event wiring ---
$("theme-toggle").addEventListener("click", toggleTheme);

document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => switchTab(t.dataset.tab)));

$("refresh").addEventListener("click", async () => {
  try { await fetch("/api/v1/refresh", { method: "POST" }); } catch (e) {}
  poll(); loadProjects(); loadBoard();
});

$("setup-refresh-models").addEventListener("click", loadSetupModels);
$("setup-test").addEventListener("click", runSetupTest);
$("setup-form").addEventListener("submit", confirmSetup);
$("setup-model").addEventListener("change", resetSetupTestState);
$("model-logout").addEventListener("click", logoutModel);

$("auto").addEventListener("change", (e) => {
  if (e.target.checked) startAuto(); else stopAuto();
});

$("project-select").addEventListener("change", (e) => {
  if (orchestrating || statePlanning) return;
  if (e.target.value) selectProject(e.target.value);
  runsPage = 0;
  loadRuns();
  poll();
});

async function startOrchestration(plan) {
  const slug = $("project-select").value;
  const iterationId = selectedIteration;
  if (!slug) {
    await alertModal({
      title: "Project required",
      message: "Create and pick a project first.",
    });
    return;
  }
  if (!iterationId) {
    await alertModal({
      title: "Iteration required",
      message: "Select an iteration on the Mission Control board first.",
    });
    return;
  }
  const iter = iterationsCache.find((i) => i.id === iterationId);
  const instructions = (iter && iter.testing_instructions || "").trim();
  if (iter && iter.state === "planning" && !instructions) {
    await alertModal({
      title: "Testing instructions required",
      message: "Add testing instructions for the QA agent before starting orchestration. Use “Add testing instructions” on the sprint board.",
    });
    return;
  }
  clientLog("info", plan ? "Plan & Orchestrate started" : "Orchestrate started", {
    project: slug,
    iteration: iterationId,
  });
  try {
    await api("/api/v1/orchestrate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_slug: slug, iteration_id: iterationId, plan: !!plan }),
    });
    selectProject(slug, iterationId);
    poll();
  } catch (e) {
    clientLog("error", "Orchestration failed to start", { project: slug, error: e.message });
    await alertModal({ title: "Could not start", message: e.message });
  }
}

$("orchestrate").addEventListener("click", () => startOrchestration(false));
$("plan-orchestrate").addEventListener("click", () => startOrchestration(true));

$("stop").addEventListener("click", async () => {
  try {
    const res = await api("/api/v1/stop", { method: "POST" });
    if (res.iteration) patchIterationCache(res.iteration);
    orchestrating = false;
    statePlanning = false;
    await loadIterations(res.iteration_id || selectedIteration);
    await loadBoard();
    updateIterationActions();
    poll();
  } catch (e) {}
});

$("np-slug").addEventListener("input", (e) => {
  const el = e.target;
  const start = el.selectionStart;
  const before = el.value;
  el.value = slugify(before);
  // keep caret roughly in place when characters were stripped
  const removed = before.length - el.value.length;
  if (start != null) el.setSelectionRange(Math.max(0, start - removed), Math.max(0, start - removed));
  syncWorkspaceField();
});

$("np-browse").addEventListener("click", openDirModal);
$("np-workspace-clear").addEventListener("click", () => setSelectedWorkspace(""));
$("dir-cancel").addEventListener("click", closeDirModal);
$("dir-modal").addEventListener("click", (e) => { if (e.target === $("dir-modal")) closeDirModal(); });
$("dir-up").addEventListener("click", async () => {
  if (!dirBrowsePath) return;
  try {
    const data = await api(`/api/v1/fs/directories?path=${encodeURIComponent(dirBrowsePath)}`);
    if (data.parent) await loadDirBrowse(data.parent);
  } catch (e) { /* ignore */ }
});
$("dir-select").addEventListener("click", () => {
  if (dirBrowsePath) setSelectedWorkspace(dirBrowsePath);
  closeDirModal();
});

$("toggle-new-project").addEventListener("click", () => {
  resetProjectForm();
  openProjectModal();
});

$("np-cancel").addEventListener("click", () => {
  resetProjectForm();
  closeProjectModal();
});

$("project-modal").addEventListener("click", (e) => {
  if (e.target !== $("project-modal")) return;
  if ($("project-modal").querySelector(".modal-card")?.classList.contains("is-busy")) return;
  resetProjectForm();
  closeProjectModal();
});

$("new-project").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("np-error").textContent = "";
  const card = $("project-modal").querySelector(".modal-card");
  const loaderMsg = editingSlug ? "Saving project…" : "Creating project…";
  setModalBusy(card, $("project-modal-loader"), $("project-modal-loader-text"), true, loaderMsg);
  try {
    if (editingSlug) {
      await api(`/api/v1/projects/${encodeURIComponent(editingSlug)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: $("np-title").value,
          description: $("np-desc").value,
          needs_git: $("np-git").checked,
          workspace_path: selectedWorkspacePath || null,
        }),
      });
      const slug = editingSlug;
      resetProjectForm();
      closeProjectModal();
      await loadProjects();
      if (selectedProject === slug) loadBoard();
    } else {
      const project = await api("/api/v1/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: $("np-title").value,
          slug: slugify($("np-slug").value),
          description: $("np-desc").value,
          needs_git: $("np-git").checked,
          workspace_path: selectedWorkspacePath || null,
        }),
      });
      resetProjectForm();
      closeProjectModal();
      await loadProjects();
      selectProject(project.slug);
    }
  } catch (err) { $("np-error").textContent = err.message; }
  finally {
    setModalBusy(card, $("project-modal-loader"), $("project-modal-loader-text"), false);
  }
});

function resetTaskForm() {
  editingTaskId = null;
  $("nt-title").value = "";
  $("nt-desc").value = "";
  $("nt-file").value = "";
  $("nt-error").textContent = "";
  const isBug = creatingTaskKind === "bug";
  const iter = currentIteration();
  const iterLabel = iter ? iter.title : selectedProject || "";
  $("tm-title").innerHTML = isBug
    ? `Add bug <span class="muted mono">· ${esc(iterLabel)}</span>`
    : `Add task <span class="muted mono">· ${esc(iterLabel)}</span>`;
  $("nt-submit").textContent = isBug ? "Add bug to Todo" : "Add to Todo";
  $("nt-file-wrap").classList.remove("hidden");
}

function openTaskModal(kind = "task") {
  if (!selectedProject || !selectedIteration) return;
  creatingTaskKind = kind;
  resetTaskForm();
  $("task-modal").classList.remove("hidden");
  $("nt-title").focus();
}

function startEditTask(task) {
  if (!selectedProject || !task) return;
  editingTaskId = task.id;
  creatingTaskKind = task.kind || "task";
  $("nt-title").value = task.title || "";
  $("nt-desc").value = task.description || "";
  $("nt-file").value = "";
  $("nt-error").textContent = "";
  $("tm-title").innerHTML = `Edit ${creatingTaskKind} <span class="muted mono">· ${esc(task.identifier)}</span>`;
  $("nt-submit").textContent = "Save changes";
  $("nt-file-wrap").classList.add("hidden");
  $("task-modal").classList.remove("hidden");
  $("nt-title").focus();
}

function closeTaskModal() {
  $("task-modal").classList.add("hidden");
  resetTaskForm();
  const card = $("task-modal").querySelector(".modal-card");
  setModalBusy(card, $("task-modal-loader"), $("task-modal-loader-text"), false);
}

$("toggle-new-task").addEventListener("click", () => openTaskModal("task"));
$("toggle-new-bug").addEventListener("click", () => openTaskModal("bug"));
$("toggle-testing-instructions").addEventListener("click", openTestingModal);
$("qa-cancel").addEventListener("click", closeTestingModal);
$("testing-modal").addEventListener("click", (e) => {
  if (e.target === $("testing-modal")) closeTestingModal();
});
$("testing-instructions-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("qa-error").textContent = "";
  if (!selectedProject || !selectedIteration) return;
  const text = $("qa-instructions").value.trim();
  if (!text) {
    $("qa-error").textContent = "Testing instructions are required before orchestration.";
    return;
  }
  try {
    const iteration = await api(
      `/api/v1/projects/${encodeURIComponent(selectedProject)}/iterations/${encodeURIComponent(selectedIteration)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ testing_instructions: text }),
      }
    );
    const idx = iterationsCache.findIndex((i) => i.id === iteration.id);
    if (idx >= 0) iterationsCache[idx] = { ...iterationsCache[idx], ...iteration };
    closeTestingModal();
    updateIterationActions();
    renderTestingInstructions();
    await loadBoard();
  } catch (err) {
    $("qa-error").textContent = err.message || "Could not save testing instructions.";
  }
});
$("toggle-new-iteration").addEventListener("click", openIterationModal);
$("iter-cancel").addEventListener("click", closeIterationModal);
$("iteration-modal").addEventListener("click", (e) => {
  if (e.target === $("iteration-modal")) closeIterationModal();
});
$("iteration-select").addEventListener("change", (e) => {
  selectedIteration = e.target.value || null;
  updateIterationActions();
  loadBoard();
});
$("new-iteration").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("iter-error").textContent = "";
  if (!selectedProject) return;
  try {
    const iteration = await api(`/api/v1/projects/${encodeURIComponent(selectedProject)}/iterations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: $("iter-title").value }),
    });
    closeIterationModal();
    await loadIterations(iteration.id);
    await loadBoard();
    await loadProjects();
  } catch (err) {
    $("iter-error").textContent = err.message;
  }
});
$("nt-cancel").addEventListener("click", closeTaskModal);
$("task-modal").addEventListener("click", (e) => {
  if (e.target !== $("task-modal")) return;
  if ($("task-modal").querySelector(".modal-card")?.classList.contains("is-busy")) return;
  closeTaskModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("task-modal").classList.contains("hidden")) {
    if ($("task-modal").querySelector(".modal-card")?.classList.contains("is-busy")) return;
    closeTaskModal();
  }
  if (!$("project-modal").classList.contains("hidden")) {
    if ($("project-modal").querySelector(".modal-card")?.classList.contains("is-busy")) return;
    resetProjectForm();
    closeProjectModal();
  }
  closeAllTaskMenus();
});
document.addEventListener("click", (e) => {
  if (!e.target.closest(".task-menu-wrap")) closeAllTaskMenus();
});

$("new-task").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("nt-error").textContent = "";
  if (!selectedProject) { $("nt-error").textContent = "pick a project"; return; }
  if (!editingTaskId && !selectedIteration) { $("nt-error").textContent = "pick an iteration"; return; }
  const card = $("task-modal").querySelector(".modal-card");
  const isEdit = !!editingTaskId;
  setModalBusy(
    card,
    $("task-modal-loader"),
    $("task-modal-loader-text"),
    true,
    isEdit ? "Saving changes…" : (creatingTaskKind === "bug" ? "Adding bug…" : "Adding task…")
  );
  const file = $("nt-file").files[0] || null;
  let description = $("nt-desc").value;
  if (!isEdit && file) description += `\n\n[Attached file available in your workspace: ${file.name}]`;
  try {
    if (isEdit) {
      await api(`/api/v1/projects/${encodeURIComponent(selectedProject)}/tasks/${encodeURIComponent(editingTaskId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: $("nt-title").value, description }),
      });
    } else {
      const task = await api(`/api/v1/projects/${encodeURIComponent(selectedProject)}/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: $("nt-title").value,
          description,
          iteration_id: selectedIteration,
          kind: creatingTaskKind,
        }),
      });
      if (file) {
        const fd = new FormData();
        fd.append("file", file);
        const res = await fetch(
          `/api/v1/projects/${encodeURIComponent(selectedProject)}/tasks/${encodeURIComponent(task.id)}/upload`,
          { method: "POST", body: fd }
        );
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          throw new Error("file upload failed: " + ((data.error && data.error.message) || res.status));
        }
      }
    }
    closeTaskModal();
    await loadIterations();
    await loadBoard();
    await loadProjects();
  } catch (err) {
    $("nt-error").textContent = err.message;
  } finally {
    setModalBusy(card, $("task-modal-loader"), $("task-modal-loader-text"), false);
  }
});

$("runs-prev").addEventListener("click", () => { if (runsPage > 0) { runsPage--; renderRuns(); } });
$("runs-next").addEventListener("click", () => { runsPage++; renderRuns(); });

$("art-project").addEventListener("change", (e) => loadArtifactFiles(e.target.value));
$("art-refresh").addEventListener("click", () => artifactProject && loadArtifactFiles(artifactProject));
$("art-edit").addEventListener("click", () => { if (artifactCanEdit) setArtifactEditMode(true); });
$("art-save").addEventListener("click", saveArtifactFile);
$("art-cancel").addEventListener("click", cancelArtifactEdit);

function openArtNewModal() {
  if (!artifactProject) return;
  $("art-new-name").value = "";
  $("art-new-body").value = "";
  $("art-new-error").textContent = "";
  $("art-new-modal").classList.remove("hidden");
  $("art-new-name").focus();
}

function closeArtNewModal() {
  $("art-new-modal").classList.add("hidden");
  $("art-new-error").textContent = "";
}

function normalizeArtPath(name) {
  return name.trim().replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/+/g, "/");
}

async function createArtifactFile(e) {
  e.preventDefault();
  if (!artifactProject) return;
  const path = normalizeArtPath($("art-new-name").value);
  if (!path) {
    $("art-new-error").textContent = "File name is required.";
    return;
  }
  if (path.endsWith("/") || path === "." || path === "..") {
    $("art-new-error").textContent = "Enter a file name, not a folder.";
    return;
  }
  const content = $("art-new-body").value;
  $("art-new-error").textContent = "";
  $("art-new-save").disabled = true;
  try {
    const data = await api(`/api/v1/projects/${encodeURIComponent(artifactProject)}/file`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, content }),
    });
    closeArtNewModal();
    await loadArtifactFiles(artifactProject);
    await openArtifactFile(artifactProject, data.path);
  } catch (err) {
    $("art-new-error").textContent = err.message;
  } finally {
    $("art-new-save").disabled = false;
  }
}

$("art-new-file").addEventListener("click", openArtNewModal);
$("art-new-cancel").addEventListener("click", closeArtNewModal);
$("art-new-modal").addEventListener("click", (e) => { if (e.target === $("art-new-modal")) closeArtNewModal(); });
$("art-new-form").addEventListener("submit", createArtifactFile);

$("art-console-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("art-console-input");
  const command = (input && input.value || "").trim();
  if (!command || !artifactProject) return;
  if (!artConsoleHistory.length || artConsoleHistory[artConsoleHistory.length - 1] !== command) {
    artConsoleHistory.push(command);
  }
  artConsoleHistIdx = artConsoleHistory.length;
  input.value = "";
  await runArtConsoleCommand(command);
});

$("art-console-input").addEventListener("keydown", (e) => {
  if (e.key === "ArrowUp") {
    e.preventDefault();
    if (!artConsoleHistory.length) return;
    if (artConsoleHistIdx <= 0) artConsoleHistIdx = 0;
    else artConsoleHistIdx -= 1;
    e.target.value = artConsoleHistory[artConsoleHistIdx] || "";
  } else if (e.key === "ArrowDown") {
    e.preventDefault();
    if (!artConsoleHistory.length) return;
    if (artConsoleHistIdx >= artConsoleHistory.length - 1) {
      artConsoleHistIdx = artConsoleHistory.length;
      e.target.value = "";
    } else {
      artConsoleHistIdx += 1;
      e.target.value = artConsoleHistory[artConsoleHistIdx] || "";
    }
  }
});

$("art-console-clear").addEventListener("click", () => {
  clearArtConsoleOutput();
  $("art-console-input")?.focus();
});

$("git-project").addEventListener("change", (e) => loadGitCommits(e.target.value));
$("git-refresh").addEventListener("click", () => gitProject && loadGitCommits(gitProject));

$("detail-close").addEventListener("click", () => $("detail").classList.add("hidden"));

initTheme();
bootstrapModelSetup();
