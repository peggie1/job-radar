const CACHE='job-radar-v4';
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['./','index.html','manifest.webmanifest']))));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k))))));
self.addEventListener('fetch',e=>{if(new URL(e.request.url).pathname.endsWith('jobs.json'))return;e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)))})

