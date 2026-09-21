/* Service worker do Acervo.
   É ele que faz o Chrome oferecer "Instalar app" em vez de só criar um atalho.

   Estratégia: REDE PRIMEIRO. Com internet, sempre busca o arquivo novo — assim
   uma atualização no GitHub chega na hora, sem ficar presa numa versão velha
   guardada. Sem internet, usa a última cópia, e o app ainda abre.

   A nuvem (Supabase) e o Fandom nunca passam pelo cache: são dados vivos. */
const CACHE = "acervo-v1";
const CASCA = ["./", "./index.html", "./manifest.json", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(CASCA)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  // apaga caches de versões antigas deste service worker
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // só o próprio app; dados de terceiros passam direto
  if (url.origin !== self.location.origin) return;

  e.respondWith(
    fetch(req)
      .then((resp) => {
        const copia = resp.clone();
        caches.open(CACHE).then((c) => c.put(req, copia)).catch(() => {});
        return resp;
      })
      .catch(() => caches.match(req).then((r) => r || caches.match("./index.html")))
  );
});
