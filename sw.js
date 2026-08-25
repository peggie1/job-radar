const CACHE='job-radar-v2';
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['./','index.html','manifest.webmanifest']))));
self.addEventListener('fetch',e=>{if(new URL(e.request.url).pathname.endsWith('jobs.json'))return;e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)))})

