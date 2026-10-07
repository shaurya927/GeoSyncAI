const CACHE_PREFIX = 'geosyncai-shell-'
const CACHE_NAME = `${CACHE_PREFIX}v3`
const SHELL = ['/', '/manifest.webmanifest', '/icon.svg']

function isPublicShellRequest(request) {
  if (request.method !== 'GET') return false
  const url = new URL(request.url)
  if (url.origin !== self.location.origin) return false
  if (url.pathname.startsWith('/api/')) return false
  return url.pathname === '/' || url.pathname === '/manifest.webmanifest' || url.pathname === '/icon.svg' || url.pathname.startsWith('/assets/')
}

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME)
    await cache.addAll(SHELL)
    // The first page may load JS/CSS before this worker controls it. Precache
    // the production entry assets now so its first offline reload also works.
    const response = await cache.match('/')
    const html = await response.text()
    const assets = [...html.matchAll(/(?:src|href)=["'](\/assets\/[^"']+)["']/g)].map(match => match[1])
    await cache.addAll([...new Set(assets)])
    await self.skipWaiting()
  })())
})

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME).map(key => caches.delete(key))))
    .then(() => self.clients.claim()))
})

self.addEventListener('fetch', event => {
  if (!isPublicShellRequest(event.request)) return
  event.respondWith((async () => {
    try {
      const response = await fetch(event.request)
      if (response.ok) { const cache = await caches.open(CACHE_NAME); await cache.put(event.request, response.clone()) }
      return response
    } catch (error) {
      const cached = await caches.match(event.request)
      if (cached) return cached
      throw error
    }
  })())
})

self.addEventListener('message', event => {
  if (event.data?.type === 'CLEAR_SHELL_CACHE') {
    event.waitUntil(caches.delete(CACHE_NAME))
  }
})
