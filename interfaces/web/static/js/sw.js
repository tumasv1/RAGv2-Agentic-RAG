// Service Worker для PWA — кэшируем только статику, HTML-страницы всегда с сервера

// Увеличь версию при изменении списка статики чтобы сбросить старый кэш
const CACHE = 'ragv2-v2';
const PRECACHE = [
  '/static/css/app.css',
  '/static/css/theme-geo.css',
  '/static/css/theme-minimal.css',
  '/static/js/app.js',
  '/static/logo.png',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

// При установке — закачиваем статику в кэш (без HTML-страниц)
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(PRECACHE))
  );
  self.skipWaiting();
});

// При активации — чистим старые кэши
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // API-запросы: только сеть, при офлайне — JSON-ошибка
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(
      fetch(e.request).catch(() =>
        new Response(JSON.stringify({ error: 'Нет соединения с сервером' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        })
      )
    );
    return;
  }

  // HTML-навигация (Accept: text/html): сеть первая, при офлайне — кэш
  // Никогда не кэшируем HTML — иначе SSR с историей чата будет устаревшим
  if (e.request.mode === 'navigate' || e.request.headers.get('accept')?.includes('text/html')) {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }

  // Статика (CSS, JS, картинки): кэш → сеть
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(response => {
        if (e.request.method === 'GET' && response.status === 200) {
          caches.open(CACHE).then(c => c.put(e.request, response.clone()));
        }
        return response;
      });
    })
  );
});
