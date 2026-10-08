/* AI Build-Out — service worker (v2, 2026-10-06). NETWORK FIRST:
   online, every launch loads the newest build and the newest data; the cache
   is only the fallback — offline, or when the network has not answered in
   time (the app page: 5 s; everything else: 10 s). Was stale-while-revalidate
   (a push went live on the SECOND open), which hid fresh builds during rapid
   iteration. Every good answer still refreshes the cache, so the app keeps
   working offline exactly as before.
   On upgrade from v1 the old cache is dropped and open windows reload once,
   so nobody has to open the app twice to leave the old copy behind.
   v3 (10-07): the kit's OWN files only. The backend's answers (positions,
   notes, trades, /me/*) carry a bearer and no Vary, so a cached copy outlived
   sign-out and answered for the next token on a slow network; they now go
   straight to the network (the app keeps its own caches and honest
   "unreachable" rows). v3 drops the v2 cache with every stored API body. */
const CACHE = 'aibo-v3';
const CORE = ['./', './index.html', './manifest.json', './icon-180.png', './icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(CORE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(ks => {
        const old = ks.filter(k => k !== CACHE);
        return Promise.all(old.map(k => caches.delete(k))).then(() => old.length > 0);
      })
      .then(upgraded => self.clients.claim().then(() => upgraded))
      .then(upgraded => {
        if (!upgraded) return;
        /* a window still showing the copy the old worker served: load the new one.
           NOT awaited: the reload's own request waits for this worker to finish
           activating, so waiting on it here would deadlock the activation. */
        self.clients.matchAll({ type: 'window' })
          .then(cs => cs.forEach(c => { c.navigate(c.url).catch(() => {}); }));
      })
  );
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  if (new URL(e.request.url).origin !== self.location.origin) return;   // 10-07: never cache the backend (see v3)
  const page = e.request.mode === 'navigate';
  e.respondWith(
    /* ignoreSearch for the page only (its ?v=); anything else matches its full URL */
    caches.match(e.request, page ? { ignoreSearch: true } : {}).then(hit => {
      /* a navigation Request cannot be re-issued with options, so the page is fetched by URL — past the HTTP cache */
      const net = (page ? fetch(e.request.url, { cache: 'no-store', credentials: 'same-origin' }) : fetch(e.request)).then(res => {
        if (res && res.ok) {
          const cp = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, cp));
        }
        return res;
      });
      if (!hit) return net;
      /* cached copy exists: use it only if the network fails or is too slow */
      const slow = new Promise(resolve => setTimeout(() => resolve(hit), page ? 5000 : 10000));
      return Promise.race([net.catch(() => hit), slow]);
    })
  );
});
