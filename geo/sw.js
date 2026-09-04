const PREFIX='geo-classroom-',CACHE=PREFIX+'v1';
const CORE=['./','./index.html','./style.css','./app.js','./manifest.webmanifest','./icon-192.png','./icon-512.png','./data/catalog.json','./data/questions.json','./data/resources.json','./data/audit.json'];
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(CORE)).then(()=>self.skipWaiting())));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith(PREFIX)&&k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{if(e.request.method!=='GET'||!e.request.url.startsWith(self.registration.scope))return;e.respondWith(fetch(e.request).then(r=>{if(r.ok){const copy=r.clone();e.waitUntil(caches.open(CACHE).then(c=>c.put(e.request,copy)));}return r;}).catch(async()=>await caches.match(e.request)||new Response('该资料尚未缓存，请联网后再打开。',{status:503,headers:{'Content-Type':'text/plain;charset=utf-8'}})));});
