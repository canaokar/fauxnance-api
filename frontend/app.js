// Fauxnance instructor console — client application.
// Plain ES module, no build step, no dependencies. Hash-routed so a deep
// link (e.g. #/classes/cohort_xyz) still works when served as static files.

/* =========================================================================
 * SECTION: API client
 * ========================================================================= */

const API_BASE = window.FAUXNANCE_API;

// Server-enforced ceiling (src/console/service.py). Each issued key writes 3
// DynamoDB items plus one cohort condition check, and TransactWriteItems
// allows 100 — so 25 is a hard limit, not a preference. Keep these in step.
const MAX_STUDENTS_PER_REQUEST = 25;

class ApiError extends Error {
  constructor(code, message, details) {
    super(message || "Something went wrong.");
    this.code = code || "INTERNAL_ERROR";
    this.details = details || {};
  }
}

/**
 * Central HTTP client. Every request that fails with 401 clears the
 * session and routes to the login view here — callers never need to
 * check for 401 themselves.
 */
async function apiRequest(method, path, body, opts) {
  opts = opts || {};
  const headers = { "Content-Type": "application/json" };
  const session = getSession();
  if (session && session.token && !opts.skipAuth) {
    headers.Authorization = "Bearer " + session.token;
  }

  let response;
  try {
    response = await fetch(API_BASE + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (networkErr) {
    throw new ApiError("NETWORK_ERROR", "Could not reach the server. Check your connection.");
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch (parseErr) {
    payload = null;
  }

  if (response.status === 401) {
    clearSession();
    const message =
      (payload && payload.error && payload.error.message) || "Your session has expired. Please sign in again.";
    if (location.hash !== "#/login") {
      location.hash = "#/login";
    } else {
      render();
    }
    throw new ApiError((payload && payload.error && payload.error.code) || "UNAUTHENTICATED", message);
  }

  if (!response.ok) {
    const err = (payload && payload.error) || {};
    throw new ApiError(err.code, err.message || "Request failed.", err.details);
  }

  return payload && payload.data;
}

const api = {
  login: (email, password) => apiRequest("POST", "/v1/console/login", { email, password }, { skipAuth: true }),
  logout: () => apiRequest("POST", "/v1/console/logout", {}),
  me: () => apiRequest("GET", "/v1/console/me"),
  listUsers: () => apiRequest("GET", "/v1/console/users"),
  createUser: (body) => apiRequest("POST", "/v1/console/users", body),
  patchUser: (userId, body) => apiRequest("PATCH", "/v1/console/users/" + encodeURIComponent(userId), body),
  listClasses: () => apiRequest("GET", "/v1/console/classes"),
  createClass: (body) => apiRequest("POST", "/v1/console/classes", body),
  getClass: (cohortId) => apiRequest("GET", "/v1/console/classes/" + encodeURIComponent(cohortId)),
  addInstructor: (cohortId, userId) =>
    apiRequest("POST", "/v1/console/classes/" + encodeURIComponent(cohortId) + "/instructors", { userId }),
  removeInstructor: (cohortId, userId) =>
    apiRequest(
      "DELETE",
      "/v1/console/classes/" + encodeURIComponent(cohortId) + "/instructors/" + encodeURIComponent(userId)
    ),
  addStudents: (cohortId, students) =>
    apiRequest("POST", "/v1/console/classes/" + encodeURIComponent(cohortId) + "/students", { students }),
  revokeStudent: (cohortId, keyId) =>
    apiRequest(
      "DELETE",
      "/v1/console/classes/" + encodeURIComponent(cohortId) + "/students/" + encodeURIComponent(keyId)
    ),
};

/* =========================================================================
 * SECTION: Session / state
 * ========================================================================= */

const SESSION_KEY = "fauxnance.session";

function getSession() {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || !parsed.token || !parsed.user) return null;
    return parsed;
  } catch (err) {
    return null;
  }
}

function setSession(session) {
  sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
}

function clearSession() {
  sessionStorage.removeItem(SESSION_KEY);
}

function currentUser() {
  const session = getSession();
  return session ? session.user : null;
}

/* =========================================================================
 * SECTION: escapeHtml + small DOM helpers
 * ========================================================================= */

const ESCAPE_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value).replace(/[&<>"']/g, (ch) => ESCAPE_MAP[ch]);
}

function qs(sel, root) {
  return (root || document).querySelector(sel);
}

function qsa(sel, root) {
  return Array.from((root || document).querySelectorAll(sel));
}

function on(el, event, handler) {
  if (el) el.addEventListener(event, handler);
}

function initials(name) {
  const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function fmtDate(iso) {
  if (!iso) return "—";
  const str = String(iso);
  return str.length >= 10 ? str.slice(0, 10) : str;
}

function fmtNumber(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "0";
  return Number(n).toLocaleString("en-US");
}

function daysUntil(iso) {
  if (!iso) return null;
  const target = new Date(String(iso).slice(0, 10) + "T00:00:00Z");
  if (Number.isNaN(target.getTime())) return null;
  const now = new Date();
  const today = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  return Math.round((target - today) / (1000 * 60 * 60 * 24));
}

function classBadge(klass) {
  const status = klass.status || "active";
  if (status !== "active") {
    if (status === "expired") return { cls: "badge-danger", text: "Expired" };
    if (status === "expiring_soon") return { cls: "badge-warn", text: "Expiring soon" };
    return { cls: "badge", text: status };
  }
  const days = daysUntil(klass.expiresAt);
  if (days !== null && days < 0) return { cls: "badge-danger", text: "Expired" };
  if (days !== null && days <= 14) return { cls: "badge-warn", text: "Expiring soon" };
  return { cls: "badge-ok", text: "Active" };
}

function meterInfo(usedToday, quota) {
  const used = Number(usedToday) || 0;
  const cap = Number(quota) || 0;
  const pct = cap > 0 ? Math.min(100, Math.round((used / cap) * 100)) : 0;
  let cls = "";
  if (pct >= 90) cls = "is-danger";
  else if (pct >= 50) cls = "is-warn";
  return { pct, cls, used, cap };
}

function csvEscape(value) {
  let str = String(value === null || value === undefined ? "" : value);
  // Neutralise spreadsheet formula injection: a cell opened in Excel or Sheets
  // that begins with one of these is evaluated, not shown. Instructors open
  // these exports directly, so prefix a quote to force it back to text.
  if (/^[=+\-@\t\r]/.test(str)) {
    str = "'" + str;
  }
  if (/[",\r\n]/.test(str)) {
    return '"' + str.replace(/"/g, '""') + '"';
  }
  return str;
}

function downloadCsv(filename, rows) {
  const csv = rows.map((row) => row.map(csvEscape).join(",")).join("\r\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  // No try/catch here that eats the error — a caller that needs to know
  // whether the download actually ran (see wireKeysIssuedModal) relies on
  // this throwing instead of failing silently.
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

/* -------- generic confirm modal (never window.confirm/alert/prompt) -------- */

let confirmRoot = null;

function ensureConfirmRoot() {
  if (!confirmRoot) {
    confirmRoot = document.createElement("div");
    confirmRoot.id = "confirm-root";
    document.body.appendChild(confirmRoot);
  }
  return confirmRoot;
}

function hideConfirm() {
  if (confirmRoot) confirmRoot.innerHTML = "";
}

function showConfirm({ title, body, confirmLabel, onConfirm }) {
  const root = ensureConfirmRoot();
  root.innerHTML =
    '<div class="modal-backdrop" data-role="confirm-backdrop">' +
    '<div class="modal">' +
    '<div class="modal-head"><h2 class="modal-title">' +
    escapeHtml(title) +
    "</h2></div>" +
    '<div class="modal-body"><p class="alert-body">' +
    escapeHtml(body) +
    "</p></div>" +
    '<div class="modal-foot">' +
    '<button class="btn" type="button" data-role="confirm-cancel">Cancel</button>' +
    '<button class="btn btn-danger" type="button" data-role="confirm-ok">' +
    escapeHtml(confirmLabel || "Confirm") +
    "</button>" +
    "</div></div></div>";

  on(qs('[data-role="confirm-cancel"]', root), "click", hideConfirm);
  on(qs('[data-role="confirm-backdrop"]', root), "click", (evt) => {
    if (evt.target === evt.currentTarget) hideConfirm();
  });
  on(qs('[data-role="confirm-ok"]', root), "click", async (evt) => {
    const btn = evt.currentTarget;
    btn.disabled = true;
    btn.textContent = "Working…";
    try {
      await onConfirm();
      hideConfirm();
    } catch (err) {
      hideConfirm();
      throw err;
    }
  });
}

/* -------- keys-issued modal navigation guard --------
 * state.issued holds up to 25 plaintext API keys that exist nowhere else —
 * the server only ever stores a hash. While that batch is on screen and
 * hasn't been downloaded yet, losing it means revoking and re-issuing every
 * key. These two module-level flags let the boot-time hashchange listener
 * and beforeunload know a guard is active without wiring a general modal
 * framework.
 */
let keysModalGuard = null; // { hash, onBeforeUnload } while armed
let suppressNextHashchange = false;

function armKeysModalGuard() {
  if (keysModalGuard) return;
  const onBeforeUnload = (evt) => {
    evt.preventDefault();
    evt.returnValue = "";
  };
  window.addEventListener("beforeunload", onBeforeUnload);
  keysModalGuard = { hash: location.hash, onBeforeUnload };
}

function disarmKeysModalGuard() {
  if (!keysModalGuard) return;
  window.removeEventListener("beforeunload", keysModalGuard.onBeforeUnload);
  keysModalGuard = null;
}

/* =========================================================================
 * SECTION: shared shell markup
 * ========================================================================= */

function shell(activeNav, user, bodyHtml, extraHtml) {
  const isAdmin = user.role === "admin";
  return (
    '<div class="app">' +
    '<div class="topbar">' +
    '<div class="brand"><div class="brand-mark">F</div>Fauxnance<span class="brand-sub">Console</span></div>' +
    '<nav class="topnav">' +
    '<a href="#/classes" class="' +
    (activeNav === "classes" ? "active" : "") +
    '">Classes</a>' +
    (isAdmin
      ? '<a href="#/instructors" class="' + (activeNav === "instructors" ? "active" : "") + '">Instructors</a>'
      : "") +
    "</nav>" +
    '<div class="topbar-end">' +
    '<div class="whoami">' +
    '<div class="avatar">' +
    escapeHtml(initials(user.name)) +
    "</div><div>" +
    '<div class="whoami-name">' +
    escapeHtml(user.name) +
    "</div>" +
    '<div class="whoami-role">' +
    (isAdmin ? "Admin" : "Instructor") +
    "</div></div></div>" +
    '<button class="btn btn-ghost btn-sm" type="button" data-role="sign-out">Sign out</button>' +
    "</div></div>" +
    '<div class="page">' +
    bodyHtml +
    "</div>" +
    "</div>" +
    (extraHtml || "")
  );
}

function alertDanger(message) {
  if (!message) return "";
  return (
    '<div class="alert alert-danger"><div class="alert-icon">⚠</div><div><p class="alert-body">' +
    escapeHtml(message) +
    "</p></div></div>"
  );
}

function wireSignOut(root) {
  on(qs('[data-role="sign-out"]', root), "click", async () => {
    try {
      await api.logout();
    } catch (err) {
      // ignore — we clear the local session regardless
    }
    clearSession();
    location.hash = "#/login";
  });
}

/* =========================================================================
 * SECTION: views — Login
 * ========================================================================= */

function renderLogin(app, prefillEmail, errorMessage) {
  app.innerHTML =
    '<div class="auth-shell"><div class="auth-card">' +
    '<div class="auth-brand"><div class="brand-mark">F</div>' +
    '<div class="auth-name">Fauxnance</div>' +
    '<div class="auth-tag">Instructor console</div></div>' +
    '<div class="card"><div class="card-body">' +
    alertDanger(errorMessage) +
    '<form data-role="login-form">' +
    '<div class="field"><label class="label">Email</label>' +
    '<input class="input" type="email" name="email" placeholder="you@institution.edu" value="' +
    escapeHtml(prefillEmail || "") +
    '" required></div>' +
    '<div class="field"><label class="label">Password</label>' +
    '<input class="input" type="password" name="password" placeholder="••••••••••••" required></div>' +
    '<button class="btn btn-primary btn-block" type="submit">Sign in</button>' +
    "</form>" +
    "</div></div>" +
    '<p class="auth-foot">Accounts are issued by your operator. Contact them if you\'ve lost access.</p>' +
    "</div></div>";

  const form = qs('[data-role="login-form"]', app);
  on(form, "submit", async (evt) => {
    evt.preventDefault();
    const email = form.email.value.trim();
    const password = form.password.value;
    const button = qs('button[type="submit"]', form);
    button.disabled = true;
    button.textContent = "Signing in…";
    try {
      const data = await api.login(email, password);
      setSession({ token: data.token, expiresAt: data.expiresAt, user: data.user });
      location.hash = "#/classes";
    } catch (err) {
      if (err instanceof ApiError) {
        renderLogin(app, email, err.message);
      } else {
        renderLogin(app, email, "Something went wrong. Please try again.");
      }
    }
  });
}

/* =========================================================================
 * SECTION: views — Classes list
 * ========================================================================= */

async function renderClasses(app) {
  const user = currentUser();
  app.innerHTML = shell("classes", user, '<p class="muted">Loading classes…</p>');

  let classes;
  try {
    const data = await api.listClasses();
    classes = data.classes || [];
  } catch (err) {
    if (!(err instanceof ApiError)) throw err;
    app.innerHTML = shell("classes", user, alertDanger(err.message));
    wireSignOut(app);
    return;
  }

  paintClasses(app, user, classes, false, null);
}

function paintClasses(app, user, classes, showModal, errorMessage) {
  const isAdmin = user.role === "admin";
  const totalStudents = classes.reduce((sum, k) => sum + (k.studentCount || 0), 0);

  const rows = classes
    .map((klass) => {
      const badge = classBadge(klass);
      const chips = (klass.instructors || [])
        .map(
          (ins) =>
            '<span class="chip"><span class="avatar">' +
            escapeHtml(initials(ins.name)) +
            "</span>" +
            escapeHtml(ins.name) +
            "</span>"
        )
        .join("");
      return (
        '<tr class="row-clickable" data-role="class-row" data-cohort="' +
        escapeHtml(klass.cohortId) +
        '">' +
        "<td><div class=\"cell-primary\">" +
        escapeHtml(klass.name) +
        '</div><div class="cell-sub mono">' +
        escapeHtml(klass.cohortId) +
        "</div></td>" +
        '<td><div class="chips">' +
        (chips || '<span class="faint">—</span>') +
        "</div></td>" +
        '<td class="num">' +
        fmtNumber(klass.studentCount) +
        "</td>" +
        '<td class="num">' +
        fmtNumber(klass.dailyQuota) +
        "</td>" +
        "<td>" +
        escapeHtml(fmtDate(klass.expiresAt)) +
        "</td>" +
        '<td class="shrink"><span class="badge ' +
        badge.cls +
        '">' +
        escapeHtml(badge.text) +
        "</span></td>" +
        "</tr>"
      );
    })
    .join("");

  const body =
    '<div class="page-head"><div><h1 class="page-title">Classes</h1>' +
    '<p class="page-sub">' +
    (isAdmin
      ? "A class issues API keys to its students and expires them automatically."
      : "Classes you've been assigned to.") +
    "</p></div>" +
    (isAdmin
      ? '<div class="page-actions"><button class="btn btn-primary" type="button" data-role="new-class">New class</button></div>'
      : "") +
    "</div>" +
    alertDanger(errorMessage) +
    '<div class="card"><div class="table-wrap"><table class="table"><thead><tr>' +
    "<th>Class</th><th>Instructors</th><th class=\"num\">Students</th>" +
    '<th class="num">Daily quota</th><th>Expires</th><th class="shrink">Status</th>' +
    "</tr></thead><tbody>" +
    (rows || '<tr><td colspan="6"><div class="empty"><p class="empty-title">No classes yet</p></div></td></tr>') +
    "</tbody></table></div>" +
    '<div class="card-foot">' +
    fmtNumber(classes.length) +
    " classes · " +
    fmtNumber(totalStudents) +
    " students.</div></div>" +
    (isAdmin
      ? '<p class="section-note"></p>'
      : "<p class=\"section-note\">Only your operator can create classes, change quotas, or move expiry dates. You can manage the students and keys inside the classes listed above.</p>");

  const modalHtml = showModal ? renderNewClassModalHtml() : "";
  app.innerHTML = shell("classes", user, body, modalHtml);
  wireSignOut(app);

  qsa('[data-role="class-row"]', app).forEach((row) => {
    on(row, "click", () => {
      location.hash = "#/classes/" + encodeURIComponent(row.dataset.cohort);
    });
  });

  on(qs('[data-role="new-class"]', app), "click", () => {
    paintClasses(app, user, classes, true, null);
  });

  if (showModal) {
    wireNewClassModal(app, {
      onCancel: () => paintClasses(app, user, classes, false, null),
      onCreated: () => renderClasses(app),
    });
  }
}

function renderNewClassModalHtml() {
  const defaultQuota = 2000;
  const defaultExpiry = "";
  return (
    '<div class="modal-backdrop" data-role="new-class-backdrop"><div class="modal">' +
    '<div class="modal-head"><h2 class="modal-title">New class</h2></div>' +
    '<div class="modal-body">' +
    alertDanger("") +
    '<form data-role="new-class-form" id="new-class-form">' +
    '<div class="field"><label class="label">Class name</label>' +
    '<input class="input" type="text" name="name" placeholder="HDFC-GradBatch-2026Q4" required></div>' +
    '<div class="field-row">' +
    '<div class="field"><label class="label">Daily quota per student</label>' +
    '<input class="input" type="number" name="dailyQuota" min="1" value="' +
    defaultQuota +
    '" required>' +
    '<p class="hint">Each student key may make this many requests per day. Resets at 00:00 UTC.</p></div>' +
    '<div class="field"><label class="label">Expires</label>' +
    '<input class="input" type="date" name="expiresAt" value="' +
    defaultExpiry +
    '" required>' +
    '<p class="hint">All keys in this class stop working after this date.</p></div>' +
    "</div>" +
    '<div class="alert alert-info"><div class="alert-icon">→</div><div>' +
    '<p class="alert-body">Creating a class also creates its cohort in the Fauxnance API. You can add students once it exists.</p>' +
    "</div></div>" +
    '<div data-role="new-class-error"></div>' +
    "</form>" +
    "</div>" +
    '<div class="modal-foot">' +
    '<button class="btn" type="button" data-role="new-class-cancel">Cancel</button>' +
    '<button class="btn btn-primary" type="submit" form="new-class-form" data-role="new-class-submit">Create class</button>' +
    "</div></div></div>"
  );
}

function wireNewClassModal(app, { onCancel, onCreated }) {
  const backdrop = qs('[data-role="new-class-backdrop"]', app);
  const form = qs('[data-role="new-class-form"]', app);
  const submitBtn = qs('[data-role="new-class-submit"]', app);
  const errorSlot = qs('[data-role="new-class-error"]', app);

  on(qs('[data-role="new-class-cancel"]', app), "click", onCancel);
  on(backdrop, "click", (evt) => {
    if (evt.target === evt.currentTarget) onCancel();
  });
  on(form, "submit", async (evt) => {
    evt.preventDefault();
    const name = form.name.value.trim();
    const dailyQuota = Number(form.dailyQuota.value);
    const expiresAt = form.expiresAt.value;
    submitBtn.disabled = true;
    submitBtn.textContent = "Creating…";
    try {
      await api.createClass({ name, dailyQuota, expiresAt });
      onCreated();
    } catch (err) {
      submitBtn.disabled = false;
      submitBtn.textContent = "Create class";
      if (err instanceof ApiError && errorSlot) {
        errorSlot.innerHTML = alertDanger(err.message);
      } else {
        throw err;
      }
    }
  });
}

/* =========================================================================
 * SECTION: views — Class detail (roster + instructors)
 * ========================================================================= */

async function renderClassDetail(app, cohortId) {
  const user = currentUser();
  app.innerHTML = shell("classes", user, '<p class="muted">Loading class…</p>');

  let detail;
  try {
    detail = await api.getClass(cohortId);
  } catch (err) {
    if (!(err instanceof ApiError)) throw err;
    app.innerHTML = shell("classes", user, alertDanger(err.message));
    wireSignOut(app);
    return;
  }

  let allUsers = null;
  let usersLoadError = null;
  if (user.role === "admin") {
    try {
      const data = await api.listUsers();
      allUsers = data.users || [];
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      usersLoadError = err.message || "Could not load instructors.";
    }
  }

  paintClassDetail(app, user, detail, {
    modal: null,
    errorMessage: null,
    allUsers,
    usersLoadError,
    issued: null,
  });
}

function paintClassDetail(app, user, detail, state) {
  const klass = detail.class;
  const students = detail.students || [];
  const instructors = detail.instructors || [];
  const isAdmin = user.role === "admin";
  const badge = classBadge(klass);

  // The roster includes revoked keys (shown with a "Revoked" badge, kept for
  // history) alongside active ones, so any on-screen count must be built
  // from what's actually in `students` rather than mixed with the
  // active-only studentCount the class list uses — otherwise the header and
  // footer contradict each other (e.g. "5 of 3").
  const activeCount = students.filter((s) => s.status !== "revoked").length;
  const revokedCount = students.length - activeCount;

  const studentRows = students
    .map((s) => {
      const revoked = s.status === "revoked";
      let statusCell, actionCell, usedCell;
      if (revoked) {
        statusCell = '<span class="badge badge-danger">Revoked</span>';
        actionCell = '<span class="faint">' + escapeHtml(fmtDate(s.revokedAt)) + "</span>";
        usedCell = '<span class="meter-label">—</span>';
      } else {
        const m = meterInfo(s.usedToday, s.dailyQuota);
        if (!s.usedToday) {
          statusCell = '<span class="badge">None today</span>';
        } else {
          statusCell = '<span class="badge badge-ok">Active</span>';
        }
        usedCell =
          '<div class="meter"><div class="meter-track"><div class="meter-fill ' +
          m.cls +
          '" data-pct="' +
          m.pct +
          '"></div></div><span class="meter-label">' +
          fmtNumber(m.used) +
          " / " +
          fmtNumber(m.cap) +
          "</span></div>";
        actionCell =
          '<button class="btn btn-sm btn-danger" type="button" data-role="revoke-key" data-key-id="' +
          escapeHtml(s.keyId) +
          '" data-key-label="' +
          escapeHtml(s.label) +
          '">Revoke</button>';
      }
      return (
        (revoked ? '<tr class="row-muted">' : "<tr>") +
        '<td><div class="cell-primary">' +
        escapeHtml(s.studentName) +
        '</div><div class="cell-sub">' +
        escapeHtml(s.studentEmail) +
        "</div></td>" +
        '<td class="mono">' +
        escapeHtml(s.label) +
        "</td>" +
        '<td class="mono faint">' +
        escapeHtml(s.keyId) +
        "</td>" +
        "<td>" +
        usedCell +
        "</td>" +
        "<td>" +
        statusCell +
        "</td>" +
        '<td class="shrink">' +
        actionCell +
        "</td>" +
        "</tr>"
      );
    })
    .join("");

  const instructorRows = instructors
    .map((ins) => {
      const isSelf = ins.userId === user.userId;
      const end = isAdmin
        ? '<button class="btn btn-sm btn-danger" type="button" data-role="remove-instructor" data-user-id="' +
          escapeHtml(ins.userId) +
          '" data-user-name="' +
          escapeHtml(ins.name) +
          '">Remove</button>'
        : '<span class="faint">Managed by admin</span>';
      return (
        '<div class="list-row"><div class="avatar">' +
        escapeHtml(initials(ins.name)) +
        "</div><div>" +
        '<div class="cell-primary">' +
        escapeHtml(ins.name) +
        '</div><div class="cell-sub">' +
        escapeHtml(ins.email) +
        "</div></div>" +
        (isSelf ? '<span class="badge">You</span>' : "") +
        '<div class="list-row-end">' +
        end +
        "</div></div>"
      );
    })
    .join("");

  const addInstructorControl = isAdmin
    ? '<div class="card-actions"><button class="btn btn-sm btn-primary" type="button" data-role="add-instructor">Add instructor</button></div>'
    : "";

  const body =
    '<div class="breadcrumb"><a href="#/classes">Classes</a><span class="faint">→</span><span>' +
    escapeHtml(klass.name) +
    "</span></div>" +
    '<div class="page-head"><div><h1 class="page-title">' +
    escapeHtml(klass.name) +
    '</h1><div class="chips">' +
    '<span class="chip mono">' +
    escapeHtml(klass.cohortId) +
    "</span>" +
    '<span class="chip">Quota <strong>' +
    fmtNumber(klass.dailyQuota) +
    "</strong>/day</span>" +
    '<span class="chip">Expires <strong>' +
    escapeHtml(fmtDate(klass.expiresAt)) +
    "</strong></span>" +
    '<span class="badge ' +
    badge.cls +
    '">' +
    escapeHtml(badge.text) +
    "</span>" +
    "</div></div>" +
    '<div class="page-actions">' +
    '<button class="btn" type="button" data-role="export-csv">Export CSV</button>' +
    '<button class="btn btn-primary" type="button" data-role="add-students">Add students</button>' +
    "</div></div>" +
    alertDanger(state.errorMessage) +
    '<div class="card"><div class="card-head"><h2 class="card-title">Students</h2>' +
    '<div class="card-actions"><span class="muted">' +
    fmtNumber(students.length) +
    " student" +
    (students.length === 1 ? "" : "s") +
    "</span></div></div>" +
    '<div class="table-wrap"><table class="table"><thead><tr>' +
    "<th>Student</th><th>Label</th><th>Key ID</th><th>Used today</th><th>Status</th><th class=\"shrink\"></th>" +
    "</tr></thead><tbody>" +
    (studentRows ||
      '<tr><td colspan="6"><div class="empty"><p class="empty-title">No students yet</p><p class="empty-body">Add students to issue their keys.</p></div></td></tr>') +
    "</tbody></table></div>" +
    '<div class="card-foot">' +
    fmtNumber(activeCount) +
    " active · " +
    fmtNumber(revokedCount) +
    " revoked · usage resets at 00:00 UTC.</div></div>" +
    '<div class="card"><div class="card-head"><h2 class="card-title">Instructors</h2>' +
    addInstructorControl +
    '</div><div class="card-body">' +
    (instructorRows || '<p class="muted">No instructors assigned.</p>') +
    "</div></div>" +
    (isAdmin ? "" : '<p class="section-note">Only an admin can add or remove instructors.</p>');

  let modalHtml = "";
  if (state.modal === "add-students") {
    modalHtml = renderAddStudentsModalHtml(klass);
  } else if (state.modal === "keys-issued") {
    modalHtml = renderKeysIssuedModalHtml(klass, state.issued || []);
  } else if (state.modal === "add-instructor") {
    modalHtml = renderAddInstructorModalHtml(klass, instructors, state.allUsers || [], state.usersLoadError);
  }

  app.innerHTML = shell("classes", user, body, modalHtml);
  wireSignOut(app);

  // CSP has no style-src, so widths are set via the CSSOM instead of an
  // inline style attribute — see the meter-fill markup above.
  qsa("[data-pct]", app).forEach((el) => {
    el.style.width = el.dataset.pct + "%";
  });

  on(qs('[data-role="export-csv"]', app), "click", () => {
    const rows = [["name", "email", "label", "status", "usedToday"]];
    students.forEach((s) => {
      rows.push([s.studentName, s.studentEmail, s.label, s.status, s.usedToday || 0]);
    });
    downloadCsv(klass.cohortId + "-roster.csv", rows);
  });

  on(qs('[data-role="add-students"]', app), "click", () => {
    paintClassDetail(app, user, detail, Object.assign({}, state, { modal: "add-students", errorMessage: null }));
  });

  if (isAdmin) {
    on(qs('[data-role="add-instructor"]', app), "click", () => {
      paintClassDetail(app, user, detail, Object.assign({}, state, { modal: "add-instructor", errorMessage: null }));
    });
    qsa('[data-role="remove-instructor"]', app).forEach((btn) => {
      on(btn, "click", () => {
        const userId = btn.dataset.userId;
        const name = btn.dataset.userName;
        showConfirm({
          title: "Remove instructor",
          body: 'Remove "' + name + '" from ' + klass.name + "? They will lose access to this class.",
          confirmLabel: "Remove",
          onConfirm: async () => {
            try {
              await api.removeInstructor(klass.cohortId, userId);
              await renderClassDetail(app, klass.cohortId);
            } catch (err) {
              if (err instanceof ApiError) {
                paintClassDetail(app, user, detail, Object.assign({}, state, { errorMessage: err.message }));
              } else {
                throw err;
              }
            }
          },
        });
      });
    });
  }

  qsa('[data-role="revoke-key"]', app).forEach((btn) => {
    on(btn, "click", () => {
      const keyId = btn.dataset.keyId;
      const label = btn.dataset.keyLabel;
      showConfirm({
        title: "Revoke key",
        body: 'Revoke the key for "' + label + '"? This cannot be undone; the student will need a new key.',
        confirmLabel: "Revoke",
        onConfirm: async () => {
          try {
            await api.revokeStudent(klass.cohortId, keyId);
            await renderClassDetail(app, klass.cohortId);
          } catch (err) {
            if (err instanceof ApiError) {
              paintClassDetail(app, user, detail, Object.assign({}, state, { errorMessage: err.message }));
            } else {
              throw err;
            }
          }
        },
      });
    });
  });

  if (state.modal === "add-students") {
    wireAddStudentsModal(app, klass, {
      onCancel: () => paintClassDetail(app, user, detail, Object.assign({}, state, { modal: null })),
      onIssued: (issued) =>
        paintClassDetail(app, user, detail, Object.assign({}, state, { modal: "keys-issued", issued })),
    });
  } else if (state.modal === "keys-issued") {
    wireKeysIssuedModal(app, klass, state.issued || [], {
      onDone: () => renderClassDetail(app, klass.cohortId),
    });
  } else if (state.modal === "add-instructor") {
    wireAddInstructorModal(app, klass, {
      onCancel: () => paintClassDetail(app, user, detail, Object.assign({}, state, { modal: null })),
      onAdded: () => renderClassDetail(app, klass.cohortId),
    });
  }
}

function renderAddStudentsModalHtml(klass) {
  return (
    '<div class="modal-backdrop" data-role="add-students-backdrop"><div class="modal modal-wide">' +
    '<div class="modal-head"><h2 class="modal-title">Add students to ' +
    escapeHtml(klass.name) +
    "</h2></div>" +
    '<div class="modal-body">' +
    '<div class="field"><label class="label">Paste one student per line</label>' +
    '<textarea class="textarea" data-role="students-textarea" placeholder="Jane Doe, jane.doe@example.com"></textarea>' +
    '<p class="hint">Format: name, email — one per line. A URL-safe label is generated from each name. Up to ' +
    MAX_STUDENTS_PER_REQUEST +
    " at a time.</p>" +
    "</div>" +
    '<div class="divider"></div>' +
    '<div class="alert alert-warn"><span class="alert-icon">⚠</span><div>' +
    '<p class="alert-title">Keys are shown once</p>' +
    "<p class=\"alert-body\">You'll see each key on the next screen and can download them as CSV. They're stored hashed and cannot be shown again.</p>" +
    "</div></div>" +
    '<div data-role="add-students-error"></div>' +
    "</div>" +
    '<div class="modal-foot">' +
    '<button class="btn" type="button" data-role="add-students-cancel">Cancel</button>' +
    '<button class="btn btn-primary" type="button" data-role="add-students-submit">Issue keys</button>' +
    "</div></div></div>"
  );
}

function parseStudentLines(raw) {
  return raw
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const idx = line.indexOf(",");
      if (idx === -1) return { name: line, email: "" };
      return { name: line.slice(0, idx).trim(), email: line.slice(idx + 1).trim() };
    });
}

function wireAddStudentsModal(app, klass, { onCancel, onIssued }) {
  const backdrop = qs('[data-role="add-students-backdrop"]', app);
  const textarea = qs('[data-role="students-textarea"]', app);
  const submitBtn = qs('[data-role="add-students-submit"]', app);
  const errorSlot = qs('[data-role="add-students-error"]', app);

  on(qs('[data-role="add-students-cancel"]', app), "click", onCancel);
  on(backdrop, "click", (evt) => {
    if (evt.target === evt.currentTarget) onCancel();
  });
  on(submitBtn, "click", async () => {
    const students = parseStudentLines(textarea.value);
    if (students.length === 0) {
      errorSlot.innerHTML = alertDanger("Enter at least one student.");
      return;
    }
    if (students.length > MAX_STUDENTS_PER_REQUEST) {
      errorSlot.innerHTML = alertDanger(
        "You can add at most " +
          MAX_STUDENTS_PER_REQUEST +
          " students at a time. Split the list and paste the rest after this batch."
      );
      return;
    }
    submitBtn.disabled = true;
    submitBtn.textContent = "Issuing…";
    try {
      const data = await api.addStudents(klass.cohortId, students);
      onIssued(data.issued || []);
    } catch (err) {
      submitBtn.disabled = false;
      submitBtn.textContent = "Issue keys";
      if (err instanceof ApiError) {
        errorSlot.innerHTML = alertDanger(err.message);
      } else {
        throw err;
      }
    }
  });
}

function renderKeysIssuedModalHtml(klass, issued) {
  const rows = issued
    .map(
      (row) =>
        "<tr><td><div class=\"cell-primary\">" +
        escapeHtml(row.studentName) +
        '</div><div class="cell-sub">' +
        escapeHtml(row.studentEmail) +
        "</div></td>" +
        '<td class="mono">' +
        escapeHtml(row.label) +
        "</td>" +
        '<td><span class="keycode">' +
        escapeHtml(row.key) +
        "</span></td></tr>"
    )
    .join("");

  return (
    '<div class="modal-backdrop" data-role="keys-issued-backdrop"><div class="modal modal-wide">' +
    '<div class="modal-head"><h2 class="modal-title">' +
    issued.length +
    " key" +
    (issued.length === 1 ? "" : "s") +
    " issued</h2></div>" +
    '<div class="modal-body">' +
    '<div class="alert alert-warn"><span class="alert-icon">⚠</span><div>' +
    '<p class="alert-title">Download these now — they cannot be shown again</p>' +
    '<p class="alert-body">Only a hash is stored. To replace a lost key you must revoke it and issue a new one.</p>' +
    "</div></div>" +
    '<div class="table-wrap"><table class="table"><thead><tr>' +
    "<th>Student</th><th>Label</th><th>API key</th></tr></thead><tbody>" +
    rows +
    "</tbody></table></div>" +
    '<div data-role="keys-issued-error"></div>' +
    "</div>" +
    '<div class="modal-foot">' +
    '<button class="btn" type="button" data-role="keys-issued-done">Done</button>' +
    '<button class="btn btn-primary" type="button" data-role="keys-issued-download">Download CSV</button>' +
    "</div></div></div>"
  );
}

function wireKeysIssuedModal(app, klass, issued, { onDone }) {
  // Only a hash is stored server-side, so this batch must not be discarded
  // without either a download or an explicit warning. That covers a stray
  // backdrop click (already blocked — no listener wires it) and the "Done"
  // button below; armKeysModalGuard()/inert cover Back/Alt+Left, keyboard
  // focus reaching the topnav/Sign out/Add students behind the backdrop,
  // and F5/tab close.
  let downloaded = false;

  armKeysModalGuard();
  const appShell = qs(".app", app);
  if (appShell) appShell.inert = true;

  const errorSlot = qs('[data-role="keys-issued-error"]', app);

  function closeModal() {
    disarmKeysModalGuard();
    if (appShell) appShell.inert = false;
    onDone();
  }

  on(qs('[data-role="keys-issued-done"]', app), "click", () => {
    if (downloaded) {
      closeModal();
      return;
    }
    showConfirm({
      title: "Keys not downloaded",
      body:
        "You haven't downloaded these keys yet. Only a hash is stored, so once you leave this screen they cannot be shown again — you'd have to revoke and re-issue them. Continue anyway?",
      confirmLabel: "Continue without downloading",
      onConfirm: async () => {
        closeModal();
      },
    });
  });
  on(qs('[data-role="keys-issued-download"]', app), "click", () => {
    const rows = [["name", "email", "label", "key"]];
    issued.forEach((row) => rows.push([row.studentName, row.studentEmail, row.label, row.key]));
    try {
      downloadCsv(klass.cohortId + "-keys.csv", rows);
    } catch (err) {
      if (errorSlot) {
        errorSlot.innerHTML = alertDanger(
          "The download didn't start. Try again, or copy the keys from this table before leaving."
        );
      }
      return;
    }
    downloaded = true;
    disarmKeysModalGuard();
    if (errorSlot) errorSlot.innerHTML = "";
  });
}

function renderAddInstructorModalHtml(klass, currentInstructors, allUsers, usersLoadError) {
  const assignedIds = new Set(currentInstructors.map((i) => i.userId));
  const eligible = allUsers.filter((u) => u.role !== "admin" && u.status === "active" && !assignedIds.has(u.userId));
  const options = eligible
    .map((u) => '<option value="' + escapeHtml(u.userId) + '">' + escapeHtml(u.name) + " (" + escapeHtml(u.email) + ")</option>")
    .join("");

  let bodyContent;
  if (usersLoadError) {
    bodyContent = alertDanger("Could not load instructors: " + usersLoadError);
  } else if (eligible.length === 0) {
    bodyContent = '<p class="muted">No eligible instructors to add. Create an instructor account first.</p>';
  } else {
    bodyContent =
      '<div class="field"><label class="label">Instructor</label>' +
      '<select class="select" data-role="add-instructor-select">' +
      options +
      "</select></div>";
  }
  const disableSubmit = Boolean(usersLoadError) || eligible.length === 0;

  return (
    '<div class="modal-backdrop" data-role="add-instructor-backdrop"><div class="modal">' +
    '<div class="modal-head"><h2 class="modal-title">Add instructor to ' +
    escapeHtml(klass.name) +
    "</h2></div>" +
    '<div class="modal-body">' +
    bodyContent +
    '<div data-role="add-instructor-error"></div>' +
    "</div>" +
    '<div class="modal-foot">' +
    '<button class="btn" type="button" data-role="add-instructor-cancel">Cancel</button>' +
    '<button class="btn btn-primary" type="button" data-role="add-instructor-submit"' +
    (disableSubmit ? " disabled" : "") +
    ">Add</button>" +
    "</div></div></div>"
  );
}

function wireAddInstructorModal(app, klass, { onCancel, onAdded }) {
  const backdrop = qs('[data-role="add-instructor-backdrop"]', app);
  const submitBtn = qs('[data-role="add-instructor-submit"]', app);
  const errorSlot = qs('[data-role="add-instructor-error"]', app);
  const select = qs('[data-role="add-instructor-select"]', app);

  on(qs('[data-role="add-instructor-cancel"]', app), "click", onCancel);
  on(backdrop, "click", (evt) => {
    if (evt.target === evt.currentTarget) onCancel();
  });
  on(submitBtn, "click", async () => {
    if (!select || !select.value) return;
    submitBtn.disabled = true;
    submitBtn.textContent = "Adding…";
    try {
      await api.addInstructor(klass.cohortId, select.value);
      onAdded();
    } catch (err) {
      submitBtn.disabled = false;
      submitBtn.textContent = "Add";
      if (err instanceof ApiError) {
        errorSlot.innerHTML = alertDanger(err.message);
      } else {
        throw err;
      }
    }
  });
}

/* =========================================================================
 * SECTION: views — Instructors admin
 * ========================================================================= */

async function renderInstructors(app) {
  const user = currentUser();
  if (user.role !== "admin") {
    location.hash = "#/classes";
    return;
  }
  app.innerHTML = shell("instructors", user, '<p class="muted">Loading accounts…</p>');

  let users;
  try {
    const data = await api.listUsers();
    users = data.users || [];
  } catch (err) {
    if (!(err instanceof ApiError)) throw err;
    app.innerHTML = shell("instructors", user, alertDanger(err.message));
    wireSignOut(app);
    return;
  }

  paintInstructors(app, user, users, { modal: false, errorMessage: null });
}

function paintInstructors(app, currentAdmin, users, state) {
  const rows = users
    .map((u) => {
      let statusBadge;
      if (u.role === "admin") {
        statusBadge = '<span class="badge badge-accent">Admin</span>';
      } else if (u.status === "active") {
        statusBadge = '<span class="badge badge-ok">Active</span>';
      } else {
        statusBadge = '<span class="badge badge-danger">Disabled</span>';
      }

      let action;
      if (u.role === "admin") {
        action = '<span class="faint">—</span>';
      } else if (u.status === "active") {
        action =
          '<button class="btn btn-sm btn-danger" type="button" data-role="deactivate-user" data-user-id="' +
          escapeHtml(u.userId) +
          '" data-user-name="' +
          escapeHtml(u.name) +
          '">Deactivate</button>';
      } else {
        action =
          '<button class="btn btn-sm" type="button" data-role="activate-user" data-user-id="' +
          escapeHtml(u.userId) +
          '">Activate</button>';
      }

      return (
        "<tr><td><div class=\"whoami\"><div class=\"avatar\">" +
        escapeHtml(initials(u.name)) +
        '</div><div><div class="cell-primary">' +
        escapeHtml(u.name) +
        '</div><div class="cell-sub">' +
        escapeHtml(u.email) +
        "</div></div></div></td>" +
        '<td class="num">' +
        fmtNumber(u.classCount || 0) +
        "</td>" +
        "<td>" +
        statusBadge +
        "</td>" +
        "<td>" +
        escapeHtml(fmtDate(u.createdAt)) +
        "</td>" +
        '<td class="shrink">' +
        action +
        "</td></tr>"
      );
    })
    .join("");

  const body =
    '<div class="page-head"><div><h1 class="page-title">Instructors</h1>' +
    '<p class="page-sub">Instructors can manage students in the classes they\'re assigned to.</p></div>' +
    '<div class="page-actions"><button class="btn btn-primary" type="button" data-role="add-instructor-user">Add instructor</button></div>' +
    "</div>" +
    alertDanger(state.errorMessage) +
    '<div class="card"><div class="table-wrap"><table class="table"><thead><tr>' +
    '<th>Instructor</th><th class="num">Classes</th><th>Status</th><th>Added</th><th class="shrink"></th>' +
    "</tr></thead><tbody>" +
    (rows || '<tr><td colspan="5"><div class="empty"><p class="empty-title">No accounts yet</p></div></td></tr>') +
    "</tbody></table></div>" +
    '<div class="card-foot">' +
    fmtNumber(users.length) +
    " accounts.</div></div>";

  const modalHtml = state.modal ? renderAddUserModalHtml() : "";
  app.innerHTML = shell("instructors", currentAdmin, body, modalHtml);
  wireSignOut(app);

  on(qs('[data-role="add-instructor-user"]', app), "click", () => {
    paintInstructors(app, currentAdmin, users, Object.assign({}, state, { modal: true }));
  });

  qsa('[data-role="deactivate-user"]', app).forEach((btn) => {
    on(btn, "click", () => {
      const userId = btn.dataset.userId;
      const name = btn.dataset.userName;
      if (userId === currentAdmin.userId) {
        paintInstructors(
          app,
          currentAdmin,
          users,
          Object.assign({}, state, { errorMessage: "You cannot disable your own account." })
        );
        return;
      }
      showConfirm({
        title: "Deactivate account",
        body: 'Deactivate "' + name + '"? They will immediately lose access to the console.',
        confirmLabel: "Deactivate",
        onConfirm: async () => {
          try {
            await api.patchUser(userId, { status: "disabled" });
            await renderInstructors(app);
          } catch (err) {
            if (err instanceof ApiError) {
              paintInstructors(app, currentAdmin, users, Object.assign({}, state, { errorMessage: err.message }));
            } else {
              throw err;
            }
          }
        },
      });
    });
  });

  qsa('[data-role="activate-user"]', app).forEach((btn) => {
    on(btn, "click", async () => {
      btn.disabled = true;
      btn.textContent = "Activating…";
      try {
        await api.patchUser(btn.dataset.userId, { status: "active" });
        await renderInstructors(app);
      } catch (err) {
        btn.disabled = false;
        btn.textContent = "Activate";
        if (err instanceof ApiError) {
          paintInstructors(app, currentAdmin, users, Object.assign({}, state, { errorMessage: err.message }));
        } else {
          throw err;
        }
      }
    });
  });

  if (state.modal) {
    wireAddUserModal(app, {
      onCancel: () => paintInstructors(app, currentAdmin, users, Object.assign({}, state, { modal: false })),
      onCreated: () => renderInstructors(app),
    });
  }
}

function renderAddUserModalHtml() {
  return (
    '<div class="modal-backdrop" data-role="add-user-backdrop"><div class="modal">' +
    '<div class="modal-head"><h2 class="modal-title">Add instructor</h2></div>' +
    '<div class="modal-body">' +
    '<form data-role="add-user-form" id="add-user-form">' +
    '<div class="field"><label class="label">Full name</label>' +
    '<input class="input" type="text" name="name" placeholder="Priya Raman" required></div>' +
    '<div class="field"><label class="label">Email</label>' +
    '<input class="input" type="email" name="email" placeholder="priya.raman@institution.edu" required></div>' +
    '<div class="field"><label class="label">Role</label>' +
    '<select class="select" name="role"><option value="instructor" selected>Instructor</option><option value="admin">Admin</option></select></div>' +
    '<div class="field"><label class="label">Temporary password</label>' +
    '<input class="input" type="text" name="password" placeholder="At least 12 characters" required minlength="12">' +
    '<p class="hint">Share this with the instructor through a secure channel; they can be issued a new one later.</p></div>' +
    '<div data-role="add-user-error"></div>' +
    "</form>" +
    "</div>" +
    '<div class="modal-foot">' +
    '<button class="btn" type="button" data-role="add-user-cancel">Cancel</button>' +
    '<button class="btn btn-primary" type="submit" form="add-user-form" data-role="add-user-submit">Create account</button>' +
    "</div></div></div>"
  );
}

function wireAddUserModal(app, { onCancel, onCreated }) {
  const backdrop = qs('[data-role="add-user-backdrop"]', app);
  const form = qs('[data-role="add-user-form"]', app);
  const submitBtn = qs('[data-role="add-user-submit"]', app);
  const errorSlot = qs('[data-role="add-user-error"]', app);

  on(qs('[data-role="add-user-cancel"]', app), "click", onCancel);
  on(backdrop, "click", (evt) => {
    if (evt.target === evt.currentTarget) onCancel();
  });
  on(form, "submit", async (evt) => {
    evt.preventDefault();
    const name = form.name.value.trim();
    const email = form.email.value.trim();
    const role = form.role.value;
    const password = form.password.value;
    submitBtn.disabled = true;
    submitBtn.textContent = "Creating…";
    try {
      await api.createUser({ name, email, role, password });
      onCreated();
    } catch (err) {
      submitBtn.disabled = false;
      submitBtn.textContent = "Create account";
      if (err instanceof ApiError) {
        errorSlot.innerHTML = alertDanger(err.message);
      } else {
        throw err;
      }
    }
  });
}

/* =========================================================================
 * SECTION: router
 * ========================================================================= */

async function render() {
  const app = qs("#app");
  hideConfirm();
  const hash = location.hash || "#/login";
  const session = getSession();

  if (!session && hash !== "#/login") {
    location.hash = "#/login";
    return;
  }
  if (session && hash === "#/login") {
    location.hash = "#/classes";
    return;
  }

  if (hash === "#/login") {
    renderLogin(app, "", null);
    return;
  }

  if (hash === "#/classes") {
    await renderClasses(app);
    return;
  }

  if (hash === "#/instructors") {
    await renderInstructors(app);
    return;
  }

  const classMatch = hash.match(/^#\/classes\/([^/]+)$/);
  if (classMatch) {
    await renderClassDetail(app, decodeURIComponent(classMatch[1]));
    return;
  }

  location.hash = "#/classes";
}

/* =========================================================================
 * SECTION: boot
 * ========================================================================= */

window.addEventListener("hashchange", () => {
  // Undownloaded plaintext keys are on screen: don't let Back/Forward (or any
  // other in-app hash navigation) tear down the modal that holds them. Snap
  // the hash back to where the modal was opened instead of routing away.
  if (keysModalGuard && location.hash !== keysModalGuard.hash) {
    suppressNextHashchange = true;
    location.hash = keysModalGuard.hash;
    return;
  }
  if (suppressNextHashchange) {
    suppressNextHashchange = false;
    return;
  }
  render().catch((err) => {
    console.error(err);
  });
});

window.addEventListener("DOMContentLoaded", () => {
  render().catch((err) => {
    console.error(err);
  });
});
