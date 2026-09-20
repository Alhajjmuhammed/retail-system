/*
 * Keeps the till openable with no connection.
 *
 * till.js already queues sales in IndexedDB while the page stays open. What
 * it could not survive was the page itself going away -- a reload, a closed
 * tab, a tablet restarting -- because nothing kept a copy of the screen. This
 * keeps one: the till page network-first (always fresh when online), and the
 * static files it needs stale-while-revalidate.
 *
 * Only GETs, only this site, and never the sync API: sales and the catalogue
 * go through till.js, which knows how to queue and retry them.
 */
const PAGES = "till-pages-v1";
const ASSETS = "till-assets-v7";  // v7: messages stay long enough to read
const TILL = new URL("./", self.location).pathname; // "/pos/"

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  const keep = new Set([PAGES, ASSETS]);
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names.filter((n) => n.startsWith("till-") && !keep.has(n)).map((n) => caches.delete(n))
      ))
      .then(() => self.clients.claim())
  );
});

// The till page asks to be kept as soon as the worker is ready. Waiting for
// the next visit meant a till opened once had nothing cached when the line
// dropped. It sends the files it actually uses, so older copies -- a new
// file name on every deploy -- are cleared out instead of piling up.
self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type !== "keep-till") return;
  event.waitUntil(keepTill(data.assets || []));
});

async function keepTill(assets) {
  const pages = await caches.open(PAGES);
  try {
    const response = await fetch(TILL, { credentials: "same-origin" });
    if (response.ok && !response.redirected) await pages.put(TILL, response);
  } catch (err) {
    /* offline right now: the next online visit keeps it */
  }
  const store = await caches.open(ASSETS);
  const wanted = new Set(assets.map((url) => new URL(url, self.location).href));
  for (const request of await store.keys()) {
    if (!wanted.has(request.url)) await store.delete(request);
  }
  await Promise.all(
    [...wanted].map((url) =>
      fetch(url).then((r) => (r.ok ? store.put(url, r) : null)).catch(() => null)
    )
  );
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate" && url.pathname === TILL) {
    event.respondWith(tillPage(request));
  } else if (url.pathname.startsWith("/static/")) {
    event.respondWith(asset(request));
  }
});

async function tillPage(request) {
  const cache = await caches.open(PAGES);
  try {
    const response = await fetch(request);
    // A redirect means "sign in" or "open a shift" -- not a till to keep.
    if (response.ok && !response.redirected) {
      await cache.put(TILL, response.clone());
    }
    return response;
  } catch (err) {
    const cached = await cache.match(TILL);
    if (cached) return cached;
    return new Response(
      "<!doctype html><meta name=viewport content='width=device-width'>" +
      "<body style='font:16px system-ui;padding:2rem'>" +
      "<h1>No connection</h1><p>Open the till once while online, and it will keep working offline after that.</p>",
      { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } }
    );
  }
}

async function asset(request) {
  const cache = await caches.open(ASSETS);
  const cached = await cache.match(request);
  const fresh = fetch(request)
    .then((response) => {
      if (response.ok) cache.put(request, response.clone());
      return response;
    })
    .catch(() => cached);
  return cached || fresh;
}
