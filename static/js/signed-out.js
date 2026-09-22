// Whoever signs in next must not be handed the previous person's till.
// Queued, unsent sales live in IndexedDB and are left alone.
if ("caches" in window) {
  caches.keys().then((names) => names
    .filter((name) => name.startsWith("till-pages"))
    .forEach((name) => caches.delete(name))).catch(() => {});
}
