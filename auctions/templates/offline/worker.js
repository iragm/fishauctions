{% load static %}{% comment %}
The site's service worker, served at /offline-worker.js (OfflineWorkerView) so its scope is the whole
site. It does one thing: keep /offline/ and its files cached, and show that page in place of any page
the site can't serve because it can't be reached. Everything else goes to the network untouched.

A plain 500 is not an outage, it's a bug on one page: that gets the 500 page, which links to /offline/.
The version is a hash of every cached file, so a deploy that changes one of them installs a new worker.
{% endcomment %}'use strict';
const VERSION = '{{ version }}';
const CACHE = 'offline-' + VERSION;
const OFFLINE_URL = '{% url "offline" %}';
const PRECACHE = [OFFLINE_URL{% for name in offline_static_files %}, '{% static name %}'{% endfor %}];
// Unreachable, as opposed to broken: our nginx can't reach Django (502-504), or Cloudflare can't
// reach us (520-527, 530).
const OUTAGE = new Set([502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530]);

self.addEventListener('install', (event) => {
  if (self.navigator.userAgent.includes('FishAuctionsApp')) {
    // The app has its own offline mode, which answering for its failed pages would hide.
    event.waitUntil(self.registration.unregister());
    return;
  }
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(PRECACHE.map((url) => new Request(url, { cache: 'reload' }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((name) => name.startsWith('offline-') && name !== CACHE).map((name) => caches.delete(name)));
    if (self.registration.navigationPreload) {
      // Page loads start while the worker wakes up, so having one costs nothing.
      await self.registration.navigationPreload.enable();
    }
    await self.clients.claim();
  })());
});

async function offlinePage() {
  const cached = await caches.match(OFFLINE_URL);
  return cached || new Response('auction.fish is not reachable right now.', {
    status: 503,
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
}

async function navigate(event) {
  try {
    const response = (await event.preloadResponse) || (await fetch(event.request));
    return OUTAGE.has(response.status) ? offlinePage() : response;
  } catch (error) {
    return offlinePage();
  }
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') {
    return;
  }
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }
  if (url.pathname === OFFLINE_URL || PRECACHE.includes(url.pathname)) {
    // The emergency page and its files: the copy this worker installed, so it opens with no network.
    event.respondWith(caches.match(url.pathname).then((cached) => cached || fetch(request)));
    return;
  }
  if (request.mode === 'navigate') {
    event.respondWith(navigate(event));
  }
});
