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

async function deleteJSON(url) {
  const resp = await fetch(url, { method: "DELETE", credentials: "same-origin" });
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
/*  Чат: /                                                                */
/* ────────────────────────────────────────────────────────────────────── */

function showTyping(history) {
  if (!history || document.getElementById("typing-indicator")) return;
  const el = document.createElement("div");
  el.id = "typing-indicator";
  el.className = "chat-msg chat-msg--agent typing-msg";
  el.innerHTML =
    '<div class="chat-bubble typing-bubble" aria-label="Агент печатает">' +
      '<span></span><span></span><span></span>' +
    '</div>';
  history.appendChild(el);
  requestAnimationFrame(() => {
    history.scrollTo({ top: history.scrollHeight, behavior: "smooth" });
  });
}

function hideTyping() {
  const el = document.getElementById("typing-indicator");
  if (el) el.remove();
}

function initChat() {
  const form = document.getElementById("chat-form");
  const qTa = document.getElementById("question");
  const banner = document.getElementById("banner");
  const history = document.getElementById("chat-history");
  const threadIdEl = document.getElementById("thread-id");

  // ── Фикс мобильной клавиатуры ────────────────────────────────────────
  // Visual Viewport API: --vh учитывает реальную высоту видимой области
  // (уменьшается когда открыта клавиатура на iOS и Android)
  const applyVh = () => {
    const h = window.visualViewport ? window.visualViewport.height : window.innerHeight;
    // --vh = реальная высота видимой области (уменьшается когда открыта клавиатура).
    // body.chat-page имеет position:fixed, поэтому layout viewport не скроллится
    // и scrollTo больше не нужен.
    document.documentElement.style.setProperty("--vh", h + "px");
  };
  applyVh();
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", () => {
      applyVh();
      // После пересчёта высоты скроллим историю в самый низ
      requestAnimationFrame(() => {
        if (history) history.scrollTo({ top: history.scrollHeight });
      });
    });
  }

  // Измеряем высоту топбара и передаём в CSS — нужно для padding-top контейнера
  const topbar = document.querySelector(".topbar");
  if (topbar) {
    document.documentElement.style.setProperty(
      "--topbar-h", topbar.getBoundingClientRect().height + "px"
    );
  }

  // Измеряем высоту поля ввода и передаём в CSS — нужно для padding-bottom
  // chat-history, чтобы сообщения не прятались за фиксированным input-ом на мобиле
  const updateInputH = () => {
    document.documentElement.style.setProperty(
      "--input-h", form.getBoundingClientRect().height + "px"
    );
  };
  updateInputH();
  // Пересчитываем при изменении размера формы (например, textarea растягивается)
  if (window.ResizeObserver) {
    new ResizeObserver(updateInputH).observe(form);
  }

  // Кнопка «Отправить» не должна забирать фокус с textarea:
  // без этого клавиатура скрывается в момент нажатия кнопки
  const sendBtn = form.querySelector(".btn-send");
  if (sendBtn) {
    sendBtn.addEventListener("mousedown", e => e.preventDefault());
  }

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
    qTa.value = "";
    qTa.focus();                       // сразу возвращаем фокус — клавиатура остаётся открытой
    appendChatMsg("user", question);   // сначала сообщение пользователя
    showTyping(history);               // затем индикатор «печатает…» на месте ответа

    try {
      const data = await postJSON("/api/ask", { question });
      if (threadIdEl) threadIdEl.textContent = data.thread_id;

      const meta = `итераций: ${data.iterations} · чанков: ${data.chunks_used} · ${data.latency_ms.toFixed(0)} мс`;
      hideTyping();
      appendChatMsg("agent", data.answer || "(пустой ответ)", data.sources || [], meta);

      // Обновляем список сессий сразу (появится новая / обновится updated_at)
      if (typeof loadSessions === "function") {
        loadSessions();
        // И повторно через 10с — чтобы подхватить сгенерированный title
        setTimeout(loadSessions, 10000);
      }
    } catch (e) {
      hideTyping();
      setBanner(banner, "error", `${e.message}${e.status ? ` (HTTP ${e.status})` : ""}`);
    } finally {
      qTa.focus();
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
    // Скроллим сам контейнер истории в самый низ — надёжнее, чем scrollIntoView
    // (который может проскроллить до того, как layout устаканится)
    requestAnimationFrame(() => {
      history.scrollTo({ top: history.scrollHeight, behavior: "smooth" });
    });
  }
}

/* ────────────────────────────────────────────────────────────────────── */
/*  Sidebar с историей сессий                                             */
/* ────────────────────────────────────────────────────────────────────── */

const SIDEBAR_DESKTOP_MIN = 1024;          // меньше — мобильный режим
const SIDEBAR_POLL_MS = 10000;             // частота фонового refresh

function initSidebar() {
  const sidebar = document.getElementById("sidebar");
  if (!sidebar) return;                    // не на странице чата

  const toggleBtn = document.getElementById("sidebar-toggle");
  const closeBtn = document.getElementById("btn-sidebar-close");
  const overlay = document.getElementById("sidebar-overlay");
  const newChatBtn = document.getElementById("btn-new-chat");
  const body = document.getElementById("sidebar-body");

  // начальное состояние: на десктопе sidebar видим (grid), на мобиле скрыт
  function isMobile() { return window.innerWidth < SIDEBAR_DESKTOP_MIN; }
  function closeMobile() {
    sidebar.classList.remove("open");
    overlay && overlay.classList.remove("open");
  }
  function openMobile() {
    sidebar.classList.add("open");
    overlay && overlay.classList.add("open");
  }
  function toggleMobile() {
    if (sidebar.classList.contains("open")) closeMobile();
    else openMobile();
  }

  if (toggleBtn) toggleBtn.addEventListener("click", () => {
    if (isMobile()) toggleMobile();
  });
  if (closeBtn) closeBtn.addEventListener("click", closeMobile);
  if (overlay) overlay.addEventListener("click", closeMobile);

  // Escape закрывает мобильный sidebar
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isMobile()) closeMobile();
  });

  // Клики внутри sidebar-body: по item → select, по .session-menu → delete
  if (body) {
    body.addEventListener("click", async (ev) => {
      const menu = ev.target.closest(".session-menu");
      if (menu) {
        ev.stopPropagation();
        const item = menu.closest(".session-item");
        const tid = item && item.dataset.threadId;
        if (!tid) return;
        if (!confirm("Удалить этот диалог? Восстановить не получится.")) return;
        try {
          const res = await deleteJSON(`/api/sessions/${encodeURIComponent(tid)}`);
          // если удалили активный — сбрасываем UI чата
          if (item.classList.contains("active")) {
            const history = document.getElementById("chat-history");
            const threadIdEl = document.getElementById("thread-id");
            if (history) history.innerHTML = "";
            if (threadIdEl && res.new_thread_id) threadIdEl.textContent = res.new_thread_id;
          }
          loadSessions();
        } catch (e) {
          alert("Не удалось удалить: " + e.message);
        }
        return;
      }
      const item = ev.target.closest(".session-item");
      if (item) {
        const tid = item.dataset.threadId;
        if (tid) selectSession(tid);
      }
    });
  }

  if (newChatBtn) newChatBtn.addEventListener("click", onNewChat);

  // Периодическая перезагрузка — подхватываем новые сессии / сгенерированные title.
  loadSessions();
  setInterval(loadSessions, SIDEBAR_POLL_MS);
}

async function loadSessions() {
  try {
    const data = await getJSON("/api/sessions");
    const groups = groupSessionsByDate(data.sessions || []);
    renderSessionList(groups);
  } catch (e) {
    // молча, фоновый поллинг не должен спамить баннерами
    console.warn("loadSessions:", e.message);
  }
}

function groupSessionsByDate(items) {
  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  const yesterdayStart = todayStart - 86400;

  const today = [], yesterday = [], earlier = [];
  for (const s of items) {
    const ts = Number(s.updated_at) || 0;
    if (ts >= todayStart) today.push(s);
    else if (ts >= yesterdayStart) yesterday.push(s);
    else earlier.push(s);
  }
  const groups = [];
  if (today.length) groups.push({ label: "Сегодня", items: today });
  if (yesterday.length) groups.push({ label: "Вчера", items: yesterday });
  if (earlier.length) groups.push({ label: "Ранее", items: earlier });
  return groups;
}

function renderSessionList(groups) {
  const body = document.getElementById("sidebar-body");
  if (!body) return;
  const activeTid = (document.getElementById("thread-id") || {}).textContent || "";

  if (!groups.length) {
    body.innerHTML = '<div class="sidebar-empty">Нет сохранённых диалогов</div>';
    return;
  }

  const trashSvg =
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>' +
    '<path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>';

  const html = groups.map(g => {
    const items = g.items.map(s => {
      const isActive = s.thread_id === activeTid;
      return (
        `<div class="session-item${isActive ? " active" : ""}" ` +
             `data-thread-id="${escapeHtml(s.thread_id)}" title="${escapeHtml(s.title)}">` +
          `<span class="session-title">${escapeHtml(s.title)}</span>` +
          `<button class="session-menu" type="button" aria-label="Удалить диалог" title="Удалить">${trashSvg}</button>` +
        `</div>`
      );
    }).join("");
    return (
      `<div class="session-group">` +
        `<div class="session-group-title">${escapeHtml(g.label)}</div>` +
        items +
      `</div>`
    );
  }).join("");

  body.innerHTML = html;
}

async function selectSession(tid) {
  const history = document.getElementById("chat-history");
  const threadIdEl = document.getElementById("thread-id");
  try {
    await postJSON(`/api/sessions/${encodeURIComponent(tid)}/select`);
    const data = await getJSON(`/api/sessions/${encodeURIComponent(tid)}/messages`);
    if (threadIdEl) threadIdEl.textContent = data.thread_id;
    if (history) {
      history.innerHTML = "";
      (data.messages || []).forEach(m => appendHistoryMsg(history, m));
      requestAnimationFrame(() => {
        history.scrollTo({ top: history.scrollHeight });
      });
    }
    // обновляем подсветку активного в списке
    document.querySelectorAll(".session-item").forEach(el => {
      el.classList.toggle("active", el.dataset.threadId === tid);
    });
    // на мобайле — закрываем sidebar после выбора
    if (window.innerWidth < SIDEBAR_DESKTOP_MIN) {
      document.getElementById("sidebar").classList.remove("open");
      const ov = document.getElementById("sidebar-overlay");
      if (ov) ov.classList.remove("open");
    }
  } catch (e) {
    alert("Не удалось открыть сессию: " + e.message);
  }
}

function appendHistoryMsg(history, m) {
  const wrapper = document.createElement("div");
  wrapper.className = "chat-msg chat-msg--" + (m.role || "agent");
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble";
  const textEl = document.createElement("div");
  textEl.className = "bubble-text";
  textEl.textContent = m.content || "";
  bubble.appendChild(textEl);
  if (m.sources && m.sources.length) {
    const ul = document.createElement("ul");
    ul.className = "bubble-sources";
    m.sources.forEach(s => {
      const li = document.createElement("li");
      li.innerHTML = `<code>${escapeHtml(s)}</code>`;
      ul.appendChild(li);
    });
    bubble.appendChild(ul);
  }
  if (m.meta) {
    const metaEl = document.createElement("div");
    metaEl.className = "bubble-meta";
    metaEl.textContent = m.meta;
    bubble.appendChild(metaEl);
  }
  wrapper.appendChild(bubble);
  history.appendChild(wrapper);
}

async function onNewChat() {
  const history = document.getElementById("chat-history");
  const threadIdEl = document.getElementById("thread-id");
  const banner = document.getElementById("banner");
  try {
    const data = await postJSON("/api/thread/reset");
    if (threadIdEl) threadIdEl.textContent = data.thread_id;
    if (history) history.innerHTML = "";
    if (banner) banner.classList.add("hidden");
    document.querySelectorAll(".session-item.active").forEach(el => el.classList.remove("active"));
    if (window.innerWidth < SIDEBAR_DESKTOP_MIN) {
      document.getElementById("sidebar").classList.remove("open");
      const ov = document.getElementById("sidebar-overlay");
      if (ov) ov.classList.remove("open");
    }
    loadSessions();
  } catch (e) {
    alert("Не удалось создать новый диалог: " + e.message);
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

// Подхватываем цвет фона из активной темы и ставим его в theme-color,
// чтобы статус-бар и нав-бар Android совпадали с фоном чата в PWA-режиме
function updateThemeColor() {
  const color = getComputedStyle(document.documentElement)
    .getPropertyValue('--bg').trim();
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta && color) meta.setAttribute('content', color);
}
updateThemeColor();

// Регистрация service worker для PWA
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js')
      .catch(err => console.warn('SW регистрация не удалась:', err));
  });
}
