// IO's service worker, for the phone app: keeps the app shell and the "your PC is asleep" screen on the phone, so opening
// IO while the PC sleeps shows a Wake button instead of a browser error. It never caches your data: /api always goes to IO.
const SHELL = "io-shell-v2";
const SHELL_FILES = ["/offline.html", "/static/io-192.png", "/static/alpine.min.js", "/static/lucide.min.js", "/static/marked.min.js", "/static/purify.min.js"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k.startsWith("io-shell-") && k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;
  if (e.request.mode === "navigate") {
    // the page itself: always fresh from IO; when IO doesn't answer (asleep, offline), the wake screen. A sleeping PC
    // doesn't refuse the connection, it just never answers, so waiting longer than a few seconds means it's asleep.
    if (url.pathname === "/offline.html") { e.respondWith(caches.match("/offline.html").then((hit) => hit || fetch(e.request))); return; }
    const asleep = () => caches.match("/offline.html");
    e.respondWith(Promise.race([
      fetch(e.request),
      new Promise((resolve) => setTimeout(resolve, 8000)).then(() => { throw new Error("no answer"); }),
    ]).catch(asleep));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
  }
  // /api and everything else: straight to IO, never from a cache
});

// notifications IO sends this phone (a run finished, an approval or a question waits)
self.addEventListener("push", (e) => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch { data = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(data.title || "IO", {
    body: data.body || "", icon: "/static/io-192.png", badge: "/static/io-192.png", tag: data.tag || "io", data: { url: data.url || "/" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(self.clients.matchAll({ type: "window" }).then((wins) => {
    const w = wins.find((c) => c.url.includes(location.origin));
    return w ? w.focus().then(() => w.navigate(target)) : self.clients.openWindow(target);
  }));
});
