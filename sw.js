// gaja 등산지도뷰어 오프라인 캐싱 서비스 워커 (PWA Service Worker - v9)
const CACHE_NAME = 'gaja-trail-cache-v9';
const CORE_ASSETS = [
  './leaflet.js',
  './leaflet.css',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  './favicon.png',
  './track_viewer.html',
  './gpx-map.html'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(CORE_ASSETS).catch(err => console.warn('PWA Asset 캐시 등록 알림:', err));
    })
  );
  self.skipWaiting();
});

// 활성화 시 구버전 캐시(v1 등) 자동 청소
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => {
      return Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => {
          console.log('구버전 캐시 삭제:', k);
          return caches.delete(k);
        })
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const req = event.request;
  const url = req.url;

  // 1. viewer.html / viewer_pc.html 등 웹페이지 문서는 "Network-First"
  // 온라인이면 항상 최신 코드를 즉시 받아오고, 인터넷이 안 되는 산속(오프라인)일 때만 캐시 사용!
  if (req.mode === 'navigate' || url.includes('viewer.html') || url.includes('viewer_pc.html') || url.endsWith('/gaja/')) {
    event.respondWith(
      fetch(req, { cache: 'no-cache' }).then(networkResponse => {
        if (networkResponse && networkResponse.status === 200) {
          const clone = networkResponse.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(req, clone));
        }
        return networkResponse;
      }).catch(() => {
        const fallbackTarget = url.includes('viewer_pc.html') ? './viewer_pc.html' : (url.includes('track_viewer.html') ? './track_viewer.html' : './viewer.html');
        return caches.match(req).then(cached => cached || caches.match(fallbackTarget));
      })
    );
    return;
  }

  // 2. 네이버 등고선 지도 타일(이미지), 라이브러리는 "Cache-First" (빠른 속도 & 오프라인 보장)
  const isCachableAsset = url.includes('pstatic.net') ||
                          url.includes('naver.com') ||
                          url.includes('leaflet') ||
                          url.includes('opentopomap.org');

  if (isCachableAsset) {
    event.respondWith(
      caches.match(req).then(cachedResponse => {
        if (cachedResponse) {
          return cachedResponse;
        }
        return fetch(req, { cache: 'no-cache' }).then(networkResponse => {
          if (networkResponse && (networkResponse.status === 200 || networkResponse.type === 'opaque')) {
            const clone = networkResponse.clone();
            caches.open(CACHE_NAME).then(cache => cache.put(req, clone));
          }
          return networkResponse;
        }).catch(() => caches.match(req));
      })
    );
    return;
  }

  // 3. 그 외 요청은 네트워크 기본 처리
  event.respondWith(fetch(req).catch(() => caches.match(req)));
});
