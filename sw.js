// gaja 등산로 뷰어 오프라인 캐싱 서비스 워커 (PWA Service Worker)
const CACHE_NAME = 'gaja-trail-cache-v1';
const CORE_ASSETS = [
  './viewer.html',
  './leaflet.js',
  './leaflet.css'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(CORE_ASSETS).catch(err => console.warn('PWA Asset 캐시 등록 알림:', err));
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', event => {
  const url = event.request.url;

  // 네이버 지도 타일, 이미지, 스타일, 스크립트, GPX 등 가로채서 캐시 우선 응답
  const isCachable = url.includes('pstatic.net') || 
                     url.includes('naver.com') || 
                     url.includes('naver.net') ||
                     url.includes('viewer.html') ||
                     url.includes('leaflet') ||
                     url.includes('.gpx') ||
                     url.includes('openstreetmap.org') ||
                     url.includes('opentopomap.org');

  if (isCachable) {
    event.respondWith(
      caches.match(event.request).then(cachedResponse => {
        if (cachedResponse) {
          // 캐시에 이미 있으면 인터넷 연결 없이 즉시 반환!
          return cachedResponse;
        }
        // 캐시에 없으면 네트워크로 가져와서 캐시에 자동 보관
        return fetch(event.request).then(networkResponse => {
          if (networkResponse && (networkResponse.status === 200 || networkResponse.type === 'opaque')) {
            const clone = networkResponse.clone();
            caches.open(CACHE_NAME).then(cache => {
              cache.put(event.request, clone);
            });
          }
          return networkResponse;
        }).catch(() => {
          // 오프라인 상태에서 네트워크 실패 시 캐시 재확인
          return caches.match(event.request);
        });
      })
    );
  } else {
    event.respondWith(
      caches.match(event.request).then(resp => resp || fetch(event.request))
    );
  }
});
