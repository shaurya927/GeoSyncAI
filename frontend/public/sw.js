const CACHE_PREFIX = 'geosyncai-shell-'
const CACHE_NAME = `${CACHE_PREFIX}v2`
const SHELL = ['/', '/manifest.webmanifest', '/icon.svg']

function isPublicShellRequest(request) {
  if (request.method !== 'GET') return false
  const url = new URL(request.url)
  if (url.origin !== self.location.origin) return false
  if (url.pathname.startsWith('/api/')) return false
  return url.pathname === '/' || url.pathname === '/manifest.webmanifest' || url.pathname === '/icon.svg' || url.pathname.startsWith('/assets/')
}

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()))
})

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME).map(key => caches.delete(key))))
    .then(() => self.clients.claim()))
})

self.addEventListener('fetch', event => {
  if (!isPublicShellRequest(event.request)) return
  event.respondWith(caches.match(event.request).then(cached => {
    const network = fetch(event.request).then(response => {
      if (response.ok) caches.open(CACHE_NAME).then(cache => cache.put(event.request, response.clone()))
      return response
    })
    return cached || network
  }))
})

self.addEventListener('message', event => {
  if (event.data?.type === 'CLEAR_SHELL_CACHE') {
    event.waitUntil(caches.delete(CACHE_NAME))
  }
})
