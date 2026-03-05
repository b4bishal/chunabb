const CACHE = "election2082-v1";
const PRECACHE = ["/", "/manifest.json"];

// ── Install ───────────────────────────────────────────────────────────────
self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

// ── Activate ──────────────────────────────────────────────────────────────
self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// ── Fetch: network-first for API, cache-first for assets ─────────────────
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/results/") || url.pathname.startsWith("/cache")) {
    // API calls: always network, no caching
    e.respondWith(fetch(e.request).catch(() =>
      new Response(JSON.stringify({ error: "Offline" }), {
        headers: { "Content-Type": "application/json" }
      })
    ));
    return;
  }
  // Static assets: cache-first
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request).then(resp => {
      if (resp.ok && e.request.method === "GET") {
        caches.open(CACHE).then(c => c.put(e.request, resp.clone()));
      }
      return resp;
    }))
  );
});

// ── Background Sync: poll results every 30s when app is in background ────
let pollInterval = null;

self.addEventListener("message", e => {
  if (e.data?.type === "START_POLL") {
    const { slug, interval } = e.data;
    if (pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(() => pollAndNotify(slug), interval || 30000);
    pollAndNotify(slug); // immediate first poll
  }
  if (e.data?.type === "STOP_POLL") {
    if (pollInterval) clearInterval(pollInterval);
    pollInterval = null;
  }
});

async function pollAndNotify(slug) {
  if (!slug) return;
  try {
    const resp = await fetch(`/results/${slug}`);
    if (!resp.ok) return;
    const data = await resp.json();

    // Broadcast to all open windows
    const clients = await self.clients.matchAll({ type: "window" });
    clients.forEach(c => c.postMessage({ type: "RESULT_UPDATE", data }));

    // Check if winner changed — send push notification
    const key = `winner_${slug}`;
    const prev = await getStored(key);
    const winner = data.candidates?.find(c => c.winner);
    if (winner && prev !== winner.candidate_name) {
      await setStored(key, winner.candidate_name);
      if (prev !== null) { // Don't notify on first load
        self.registration.showNotification("🗳️ निर्वाचन अपडेट!", {
          body: `${winner.candidate_name} विजयी — ${data.constituency_slug?.toUpperCase()}`,
          icon: "/manifest.json",
          badge: "/manifest.json",
          tag: "election-winner",
          renotify: true,
          data: { slug }
        });
      }
    }
  } catch (_) {}
}

// Simple key-value store using Cache API as storage
async function getStored(key) {
  try {
    const c = await caches.open("election-kv");
    const r = await c.match(`/kv/${key}`);
    return r ? await r.text() : null;
  } catch { return null; }
}
async function setStored(key, val) {
  try {
    const c = await caches.open("election-kv");
    await c.put(`/kv/${key}`, new Response(val));
  } catch {}
}

self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(
    self.clients.matchAll({ type: "window" }).then(clients => {
      if (clients.length) return clients[0].focus();
      return self.clients.openWindow("/");
    })
  );
});
