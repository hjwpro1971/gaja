# mapdb — NAS 지도 DB

한국 OSM(Geofabrik `south-korea-latest.osm.pbf`)에서 지도 도구가 쓰는 점·선만 골라
SQLite(R-tree 색인)로 담고, gaja-tools(8444)의 `/mapdb/` 로 중계해 쓴다.

- 관리 화면: `https://192.168.219.109:8444/mapdb/` (현황·검색·지도 조회·데이터 갱신)
- API: `/mapdb/api/bbox?s=&w=&n=&e=` → Overpass와 같은 JSON (`only=hiking`이면 등산로만)
- 사용처: `illust_map.html`(지명·도로·하천), `gpx-map.html`(갈림길 데이터 보완) — NAS DB를 먼저 쓰고 실패하면 외부 Overpass로 넘어감

## 배포 (NAS: /share/CACHEDEV1_DATA/WebApp/mapdb)
```
cp ../mountain_junctions.js .        # 빌드 컨텍스트에 포함
DOCKER_BUILDKIT=0 docker build -t mapdb .
docker run -d --name mapdb --restart unless-stopped --network gaja-net \
  -v /share/CACHEDEV1_DATA/WebApp/mapdb/data:/data mapdb
```
gaja-tools `tls.conf`에 `ProxyPass /mapdb/ http://mapdb:8000/mapdb/` 추가 후 `docker restart gaja-tools`.

DB는 `data/mapdb.sqlite`. 갱신은 관리 화면 버튼(또는 `docker exec mapdb python build.py`) — 새 파일로 만든 뒤 교체하므로 도중에도 서비스 유지.
관계(relation)로만 그려진 큰 호수는 v1에 없음. 지명 수정·추가 기능은 추후.
