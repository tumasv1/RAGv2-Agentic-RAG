/* ────────────────────────────────────────────────────────────────────── */
/*  Общие утилиты                                                         */
/* ────────────────────────────────────────────────────────────────────── */

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify(body || {}),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const msg = (data && data.message) || resp.statusText || "Ошибка запроса";
    const err = new Error(msg);
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

async function getJSON(url) {
  const resp = await fetch(url, { credentials: "same-origin" });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error((data && data.message) || resp.statusText);
    err.status = resp.status;
    throw err;
  }
  return data;
}

function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }
function setBanner(el, kind, msg) {
  el.className = "banner " + kind;
  el.textContent = msg;
  show(el);
}

/* ────────────────────────────────────────────────────────────────────── */
/*  Переключатель темы (работает на всех страницах)                        */
/* ────────────────────────────────────────────────────────────────────── */

const THEMES = ["default", "geo", "minimal"];

function initTheme() {
  const btn = document.getElementById("theme-toggle-btn");
  if (!btn) return;

  function apply(theme) {
    document.body.classList.remove("theme-geo", "theme-minimal");
    if (theme === "geo") document.body.classList.add("theme-geo");
    if (theme === "minimal") document.body.classList.add("theme-minimal");
    btn.textContent = theme;
    localStorage.setItem("chat-theme", theme);
  }

  const saved = localStorage.getItem("chat-theme");
  apply(THEMES.includes(saved) ? saved : "default");

  btn.addEventListener("click", () => {
    const current = localStorage.getItem("chat-theme") || "default";
    const next = THEMES[(THEMES.indexOf(current) + 1) % THEMES.length];
    apply(next);
  });
}

/* ────────────────────────────────────────────────────────────────────── */
/*  Чат: /                                                                */
/* ────────────────────────────────────────────────────────────────────── */

function initChat() {
  const form = document.getElementById("chat-form");
  const qTa = document.getElementById("question");
  const loader = document.getElementById("loader");
  const banner = document.getElementById("banner");
  const history = document.getElementById("chat-history");
  const threadIdEl = document.getElementById("thread-id");
  const resetBtn = document.getElementById("reset-thread-btn");

  // Enter → отправить, Shift+Enter → перенос строки
  qTa.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      form.requestSubmit();
    }
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const question = qTa.value.trim();
    if (!question) return;

    hide(banner);
    appendChatMsg("user", question);
    qTa.value = "";
    show(loader);

    try {
      const data = await postJSON("/api/ask", { question });
      threadIdEl.textContent = data.thread_id;

      const meta = `итераций: ${data.iterations} · чанков: ${data.chunks_used} · ${data.latency_ms.toFixed(0)} мс`;
      appendChatMsg("agent", data.answer || "(пустой ответ)", data.sources || [], meta);
    } catch (e) {
      setBanner(banner, "error", `${e.message}${e.status ? ` (HTTP ${e.status})` : ""}`);
    } finally {
      hide(loader);
      qTa.focus();
    }
  });

  resetBtn.addEventListener("click", async () => {
    try {
      const data = await postJSON("/api/thread/reset");
      threadIdEl.textContent = data.thread_id;
      history.innerHTML = "";
      hide(banner);
      setBanner(banner, "info", "Сессия сброшена. Новый thread_id: " + data.thread_id);
    } catch (e) {
      setBanner(banner, "error", e.message);
    }
  });

  // Добавляет пузырёк сообщения в историю чата
  function appendChatMsg(role, text, sources, meta) {
    const wrapper = document.createElement("div");
    wrapper.className = "chat-msg chat-msg--" + role;

    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";

    const textEl = document.createElement("div");
    textEl.className = "bubble-text";
    textEl.textContent = text;
    bubble.appendChild(textEl);

    if (sources && sources.length) {
      const srcEl = document.createElement("ul");
      srcEl.className = "bubble-sources";
      sources.forEach(s => {
        const li = document.createElement("li");
        li.innerHTML = `<code>${escapeHtml(s)}</code>`;
        srcEl.appendChild(li);
      });
      bubble.appendChild(srcEl);
    }

    if (meta) {
      const metaEl = document.createElement("div");
      metaEl.className = "bubble-meta";
      metaEl.textContent = meta;
      bubble.appendChild(metaEl);
    }

    wrapper.appendChild(bubble);
    history.appendChild(wrapper);
    wrapper.scrollIntoView({ behavior: "smooth", block: "end" });
  }
}

/* ────────────────────────────────────────────────────────────────────── */
/*  Admin: /admin                                                         */
/* ────────────────────────────────────────────────────────────────────── */

function initAdmin() {
  const btnInc = document.getElementById("reindex-incremental");
  const btnForce = document.getElementById("reindex-force");
  const banner = document.getElementById("reindex-banner");
  const statusLabel = document.getElementById("status-label");
  const jobIdEl = document.getElementById("status-jobid");
  const startedEl = document.getElementById("status-started");
  const finishedEl = document.getElementById("status-finished");
  const statsBlock = document.getElementById("status-stats");
  const errorBlock = document.getElementById("status-error");

  let pollTimer = null;

  async function refreshStatus() {
    try {
      const s = await getJSON("/api/reindex/status");
      statusLabel.textContent = s.status;
      jobIdEl.textContent = s.job_id || "—";
      startedEl.textContent = s.started_at ? new Date(s.started_at * 1000).toLocaleString() : "—";
      finishedEl.textContent = s.finished_at ? new Date(s.finished_at * 1000).toLocaleString() : "—";

      if (s.stats) {
        document.getElementById("stats-added").textContent = s.stats.added;
        document.getElementById("stats-updated").textContent = s.stats.updated;
        document.getElementById("stats-deleted").textContent = s.stats.deleted;
        document.getElementById("stats-unchanged").textContent = s.stats.unchanged;
        document.getElementById("stats-chunks").textContent = s.stats.total_chunks;
        show(statsBlock);
      } else {
        hide(statsBlock);
      }

      if (s.error) {
        errorBlock.textContent = "Ошибка: " + s.error;
        show(errorBlock);
      } else {
        hide(errorBlock);
      }

      // если идёт — продолжаем поллинг; иначе — останавливаем
      if (s.status === "running") {
        if (!pollTimer) pollTimer = setInterval(refreshStatus, 5000);
      } else if (pollTimer) {
        clearInterval(pollTimer); pollTimer = null;
      }
    } catch (e) {
      setBanner(banner, "error", "Не удалось получить статус: " + e.message);
    }
  }

  async function triggerReindex(force) {
    hide(banner);
    try {
      const data = await postJSON("/api/reindex?force=" + (force ? "true" : "false"));
      setBanner(banner, "info", `Запущена индексация. job_id: ${data.job_id}`);
      refreshStatus();
    } catch (e) {
      if (e.status === 409) {
        setBanner(banner, "warning", "Индексация уже запущена. Дождись завершения.");
      } else {
        setBanner(banner, "error", e.message);
      }
    }
  }

  btnInc.addEventListener("click", () => triggerReindex(false));
  btnForce.addEventListener("click", () => {
    if (confirm("Полная переиндексация пересоберёт весь индекс. Продолжить?")) {
      triggerReindex(true);
    }
  });

  // первый вызов + автоподхват running-задачи
  refreshStatus();
}

/* ────────────────────────────────────────────────────────────────────── */
/*  helpers                                                               */
/* ────────────────────────────────────────────────────────────────────── */

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
