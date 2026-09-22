// Keep the till on the device so it opens with no connection.
//
// The settings come from data attributes on the script tag rather than from
// values written into the source, because inline script is not allowed by
// the Content-Security-Policy this site sends -- and blocked, this never
// ran: the offline till registered no worker at all in production.
(() => {
  const here = document.currentScript;
  if (!here) return;
  const offline = here.dataset.offline === "true";
  const workerUrl = here.dataset.worker;
  const scope = here.dataset.scope;
  if (!offline || !("serviceWorker" in navigator) || !workerUrl) return;

  navigator.serviceWorker.register(workerUrl, { scope })
    .then(() => navigator.serviceWorker.ready)
    .then((registration) => {
      const assets = [...document.querySelectorAll("script[src], link[rel=stylesheet][href], link[rel=icon][href]")]
        .map((el) => el.src || el.href)
        .filter((url) => url.startsWith(location.origin + "/static/"));
      registration.active.postMessage({ type: "keep-till", assets: assets });
    })
    .catch(() => {});

  // A new version of the styles or the script is served from the cache
  // first, so the load right after an update shows the new page with the old
  // stylesheet. The worker says when it has taken over; the till reloads
  // itself, but never while there is a basket on the screen.
  navigator.serviceWorker.addEventListener("message", (event) => {
    if ((event.data || {}).type !== "worker-updated") return;
    const till = Alpine.$data(document.querySelector("[x-data]"));
    if (!till) return;
    till.updateWaiting = true;
    till.takeUpdateIfSafe();
  });
})();
