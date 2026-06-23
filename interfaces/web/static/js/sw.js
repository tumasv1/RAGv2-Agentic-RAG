// Аварийный сброс SW: очищаем все кеши, снимаем регистрацию, перезагружаем страницы.
//
// Этот файл заменяет предыдущий SW, который кешировал HTML-страницы и ломал
// загрузку истории чата при открытии нового окна/вкладки.
// После выполнения SW удаляет сам себя — дальнейшая регистрация не происходит.

self.addEventListener('install', () => {
  // Активируемся немедленно, не ждём закрытия вкладок
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.map(k => caches.delete(k))))
      .then(() => self.clients.matchAll({ type: 'window', includeUncontrolled: true }))
      .then(clients => {
        // Сначала снимаем регистрацию, потом перезагружаем все вкладки
        return self.registration.unregister().then(() => {
          clients.forEach(c => c.navigate(c.url));
        });
      })
  );
});
