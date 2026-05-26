// sw.js — Tee Time Monitor service worker
// Strategy: network-first with cache fallback.
// Cache name is versioned; bump CACHE_NAME when sw.js itself changes to evict stale caches.
//
// DESIGN NOTE: Alternative strategy is stale-while-revalidate — serves cached page instantly
// and updates in background. Better perceived perf but user sees stale data for one full visit.
// Network-first chosen here since data freshness is the whole point of this tool.

const CACHE_NAME = 'tee-times-v1';
const PRECACHE = [
  'index.html',
  'manifest.json',
  'data.json',
  'version.json',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE_NAME)
      .then(c => c.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  // Only handle same-origin GETs; skip cross-origin (Google Fonts etc.)
  if (e.request.method !== 'GET') return;
  if (!e.request.url.startsWith(self.location.origin)) return;

  e.respondWith(
    fetch(e.request)
      .then(response => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
        }
        return response;
      })
      .catch(() => caches.match(e.request))
  );
});
