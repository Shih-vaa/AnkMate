{% load static %}// AnkMate service worker.
// PRIVACY RULE: only public static files and the generic offline page are cached.
// Pages that show student, fee or staff data are never stored on the device by this worker.
const CACHE = "ankmate-static-v1";
const OWN_CACHE_PREFIXES = ["ankmate-", "schoolhub-"]; // "schoolhub-" = the old working name; tidy up its leftovers
const OFFLINE_URL = "{% url 'offline' %}";
const STATIC_PREFIX = "{% static '' %}";
const PRECACHE = [
  OFFLINE_URL,
  "{% static 'web/app.css' %}",
  "{% static 'web/icons/icon-192.png' %}",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((k) => k !== CACHE && OWN_CACHE_PREFIXES.some((p) => k.startsWith(p))).map((k) => caches.delete(k))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Static assets: show the cached copy immediately, refresh it in the background.
  if (url.pathname.startsWith(STATIC_PREFIX)) {
    event.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(request).then((cached) => {
          const network = fetch(request)
            .then((response) => {
              if (response.ok) cache.put(request, response.clone());
              return response;
            })
            .catch(() => cached);
          return cached || network;
        })
      )
    );
    return;
  }

  // Pages: always go to the network (fresh, permission-checked data). Offline -> friendly page.
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE_URL)));
  }
});
