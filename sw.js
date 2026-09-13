// gaja 등산지도뷰어 오프라인 캐싱 서비스 워커 (PWA Service Worker - v36)
const CACHE_NAME = 'gaja-trail-cache-v36';
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

// 페이지 쪽(track_viewer.html)이 새 서비스워커의 'installed' 상태를
// 감지하면 이 메시지를 보내 즉시 활성화를 요청한다. self.skipWaiting()을
// install 시점에 호출해도, PWA가 백그라운드에 오래 떠 있는 등 표준
// 라이프사이클상 활성화가 지연되는 경우가 있어 명시적으로 한 번 더
// 트리거한다 — 이게 없으면 사이트 데이터를 완전히 지워야만 최신 버전이
// 반영되는 문제가 있었다.
self.addEventListener('message', event => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
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

  // 1. track_viewer.html 등 웹페이지 문서는 "Network-Only, 오프라인일 때만 캐시 폴백".
  // ⚠️ 예전엔 매 요청마다 성공 응답을 캐시에 덮어썼는데(Network-First),
  // 이러면 "버전을 확인해 캐시를 지우는 코드" 자체가 옛날 캐시된 HTML
  // 안에 갇혀 있다가, 코드를 아무리 고쳐도 그 옛날 버전 확인 로직이
  // 계속 실행되며 스스로는 절대 새 버전을 인식 못 하는 모순이 있었다
  // (실제로 site data를 완전히 지워야만 갱신되는 문제로 나타났다).
  // 이제 HTML은 절대 캐시에 새로 저장하지 않는다 — 온라인이면 무조건
  // 네트워크에서 받아오고, fetch 자체가 실패(진짜 오프라인)할 때만
  // install 시점에 미리 저장해둔 스냅샷(CORE_ASSETS)을 최후 수단으로 쓴다.
  if (req.mode === 'navigate' || url.includes('track_viewer.html') || url.endsWith('/gaja/')) {
    event.respondWith(
      fetch(req, { cache: 'no-cache' }).catch(() => {
        return caches.match('./track_viewer.html');
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
