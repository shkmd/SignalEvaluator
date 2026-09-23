// Service worker for the PWA app shell. Deliberately conservative for a live-trading app:
// only ever caches the HTML shell (as an offline fallback) and versioned /static/ assets
// (app.js/style.css already carry a ?v=<deploy-time> cache-busting query param -- see
// _asset_version() in app/main.py -- so a cached entry is naturally invalidated on every
// deploy without this worker needing to track versions itself). /api/* is never touched: an
// order book, position, or price served stale from a cache would be actively misleading, not
// just an inconvenience.
const CACHE_NAME = "signal-evaluator-shell-v1";

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  if (url.pathname === "/") {
    // Network-first: always prefer the freshest HTML when online (matches the server's own
    // Cache-Control: no-store on this route). The cache is purely an offline fallback.
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          return res;
        });
      })
    );
  }
});
