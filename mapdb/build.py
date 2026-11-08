"""한국 OSM 데이터(Geofabrik .pbf) → 지도용 SQLite DB 생성.

지도 도구(illust_map, gpx-map)가 쓰는 것만 골라 담는다:
  - 점(poi):  봉우리·고개·마을/지역·역·학교·사찰·전망대·주차장·쉼터·샘·안내소
  - 선(line): 도로·임도·등산로·하천·철도, 저수지/호수(닫힌 way)
관계(relation)로만 그려진 큰 호수 등은 v1에서는 빠진다.

메모리를 아끼려고 노드 좌표 색인은 디스크 파일(sparse_file_array)을 쓴다.
결과는 임시 파일에 만든 뒤 마지막에 원자적으로 교체하므로, 갱신 도중에도
서버는 기존 DB로 계속 응답한다.
"""
import json, os, sqlite3, sys, time, urllib.request

import osmium

DATA = os.environ.get('DATA_ROOT', '/data')
PBF_URL = os.environ.get('PBF_URL', 'https://download.geofabrik.de/asia/south-korea-latest.osm.pbf')
DB = os.path.join(DATA, 'mapdb.sqlite')

POI_RULES = [  # (태그, 값 집합) → kind
    ('natural', {'peak', 'saddle', 'spring', 'cave_entrance'}),
    ('place', {'city', 'town', 'village', 'hamlet', 'suburb', 'neighbourhood', 'quarter', 'borough', 'county'}),
    ('waterway', {'waterfall'}),
    ('railway', {'station'}),
    ('amenity', {'school', 'place_of_worship', 'parking', 'shelter', 'toilets', 'bus_station'}),
    ('highway', {'bus_stop'}),
    ('tourism', {'viewpoint', 'information', 'alpine_hut', 'camp_site'}),
]
HIGHWAYS = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential',
            'track', 'path', 'footway', 'steps'}
KEEP_TAGS = {'name', 'name:ko', 'ele', 'natural', 'place', 'railway', 'amenity', 'tourism', 'highway',
             'waterway', 'tunnel', 'bridge', 'ref', 'religion', 'information', 'sac_scale', 'surface',
             'admin_level', 'boundary', 'water', 'landuse'}
# 관계(relation)로 그려진 면: 행정구역(4=시·도, 6=시·군·구, 7=일반구, 8=읍·면·동)과 큰 호수·저수지
ADMIN_LEVELS = {'4', '6', '7', '8'}
EPS = {'admin=4': .002, 'admin=6': .001, 'admin=7': .0007, 'admin=8': .0005, 'natural=water': .0002}  # 단순화 허용오차(도)


def log(msg):
    print(time.strftime('%H:%M:%S'), msg, flush=True)


def poi_kind(tags):
    for k, vals in POI_RULES:
        v = tags.get(k)
        if v in vals:
            return f'{k}={v}'
    return None


def line_kind(tags):
    hw = tags.get('highway')
    if hw in HIGHWAYS:
        return f'highway={hw}'
    ww = tags.get('waterway')
    if ww in ('river', 'stream'):
        return f'waterway={ww}'
    if tags.get('natural') == 'water':
        return 'natural=water'
    if tags.get('railway') == 'rail':
        return 'railway=rail'
    return None


def area_kind(tags):
    if tags.get('boundary') == 'administrative' and tags.get('admin_level') in ADMIN_LEVELS and 'name' in tags:
        return 'admin=' + tags.get('admin_level')
    if tags.get('type') == 'multipolygon' and (tags.get('natural') == 'water' or tags.get('landuse') == 'reservoir'):
        return 'natural=water'
    return None


def rdp(pts, eps):
    """Douglas-Peucker 단순화(반복형). 양 끝점은 항상 남는다 — 선을 이어 붙일 때 끝점이 맞아야 하므로."""
    if len(pts) < 3:
        return list(pts)
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (y1, x1), (y2, x2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        L = (dx * dx + dy * dy) ** .5
        best, bi = -1, -1
        for i in range(a + 1, b):
            y, x = pts[i]
            d = abs(dy * (x - x1) - dx * (y - y1)) / L if L else ((x - x1) ** 2 + (y - y1) ** 2) ** .5
            if d > best:
                best, bi = d, i
        if best > eps:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def stitch(segs):
    """관계의 바깥 테두리 선들을 끝점끼리 이어 닫힌 고리(ring)들로 만든다."""
    segs = [list(x) for x in segs if len(x) >= 2]
    rings = []
    while segs:
        cur = segs.pop()
        grown = True
        while grown and cur[0] != cur[-1]:
            grown = False
            for i, x in enumerate(segs):
                if x[0] == cur[-1]:
                    cur += x[1:]
                elif x[-1] == cur[-1]:
                    cur += x[::-1][1:]
                elif x[-1] == cur[0]:
                    cur = x + cur[1:]
                elif x[0] == cur[0]:
                    cur = x[::-1] + cur[1:]
                else:
                    continue
                segs.pop(i)
                grown = True
                break
        if len(cur) >= 4:
            rings.append(cur)
    return rings


class RelCollector(osmium.SimpleHandler):
    """1차: 관계만 훑어 필요한 행정구역·호수와 그 테두리 선(way) 번호를 모은다."""
    def __init__(self):
        super().__init__()
        self.rels, self.need = [], set()

    def relation(self, r):
        k = area_kind(r.tags)
        if not k:
            return
        outer = [m.ref for m in r.members if m.type == 'w' and m.role in ('outer', '')]
        if outer:
            self.rels.append((r.id, k, slim(r.tags), outer))
            self.need.update(outer)


def slim(tags):
    return {t.k: t.v for t in tags if t.k in KEEP_TAGS}


SCHEMA = """
CREATE TABLE poi(id INTEGER PRIMARY KEY, osm TEXT, kind TEXT, name TEXT, ele REAL, lat REAL, lon REAL, tags TEXT);
CREATE TABLE line(id INTEGER PRIMARY KEY, osm TEXT, kind TEXT, name TEXT, tags TEXT, geom TEXT);
CREATE VIRTUAL TABLE poi_idx USING rtree(id, minlat, maxlat, minlon, maxlon);
CREATE VIRTUAL TABLE line_idx USING rtree(id, minlat, maxlat, minlon, maxlon);
CREATE TABLE area(id INTEGER PRIMARY KEY, osm TEXT, kind TEXT, name TEXT, tags TEXT, geom TEXT);
CREATE VIRTUAL TABLE area_idx USING rtree(id, minlat, maxlat, minlon, maxlon);
CREATE TABLE junction(id INTEGER PRIMARY KEY, mountain TEXT, lat REAL, lon REAL);
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
"""


class Handler(osmium.SimpleHandler):
    def __init__(self, db, need=()):
        super().__init__()
        self.db = db
        self.need, self.wgeom = need, {}  # 관계 테두리로 쓰일 선 → 단순화한 좌표
        self.np = self.nl = 0
        self.pois, self.lines = [], []

    def flush(self):
        c = self.db
        if self.pois:
            c.executemany('INSERT INTO poi VALUES(?,?,?,?,?,?,?,?)', [p[:8] for p in self.pois])
            c.executemany('INSERT INTO poi_idx VALUES(?,?,?,?,?)', [(p[0], p[5], p[5], p[6], p[6]) for p in self.pois])
            self.pois = []
        if self.lines:
            c.executemany('INSERT INTO line VALUES(?,?,?,?,?,?)', [l[:6] for l in self.lines])
            c.executemany('INSERT INTO line_idx VALUES(?,?,?,?,?)', [(l[0],) + l[6] for l in self.lines])
            self.lines = []

    def add_poi(self, osm, tags, kind, lat, lon):
        t = slim(tags)
        if kind == 'amenity=toilets' and not t.get('name'):
            t['name'] = '화장실'
        try:
            ele = float(str(t.get('ele', '')).replace('m', '').strip())
        except ValueError:
            ele = None
        self.np += 1
        self.pois.append((self.np, osm, kind, t.get('name:ko') or t.get('name'), ele, round(lat, 6), round(lon, 6),
                          json.dumps(t, ensure_ascii=False)))
        if len(self.pois) >= 5000:
            self.flush()

    def node(self, n):
        if 'name' not in n.tags and n.tags.get('amenity') != 'toilets':  # 화장실은 이름 없는 것도 (산행 들머리에 중요)
            return
        k = poi_kind(n.tags)
        if k:
            self.add_poi(f'n{n.id}', n.tags, k, n.location.lat, n.location.lon)

    def way(self, w):
        if w.id in self.need:
            try:
                g = [(round(nd.lat, 6), round(nd.lon, 6)) for nd in w.nodes if nd.location.valid()]
                if len(g) >= 2:
                    self.wgeom[w.id] = rdp(g, .0002)
            except osmium.InvalidLocationError:
                pass
        tags = w.tags
        lk = line_kind(tags)
        pk = poi_kind(tags) if 'name' in tags else None
        if not lk and not pk:
            return
        try:
            pts = [(round(nd.lat, 6), round(nd.lon, 6)) for nd in w.nodes if nd.location.valid()]
        except osmium.InvalidLocationError:
            return
        if len(pts) < 2:
            return
        if pk:  # 학교·절 등 면으로 그려진 시설 → 중심점을 점으로 저장
            self.add_poi(f'w{w.id}', tags, pk, sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        if lk:
            t = slim(tags)
            lats = [p[0] for p in pts]
            lons = [p[1] for p in pts]
            self.nl += 1
            self.lines.append((self.nl, f'w{w.id}', lk, t.get('name:ko') or t.get('name'), json.dumps(t, ensure_ascii=False),
                               json.dumps(pts, separators=(',', ':')), (min(lats), max(lats), min(lons), max(lons))))
            if len(self.lines) >= 5000:
                self.flush()


def import_junctions(db, path):
    """기존 mountain_junctions.js(산별 등산로 갈림길)를 그대로 옮겨 담는다."""
    if not os.path.exists(path):
        return 0
    txt = open(path, encoding='utf-8').read()
    arr = json.loads(txt[txt.index('['):txt.rindex(']') + 1])
    db.executemany('INSERT INTO junction(mountain,lat,lon) VALUES(?,?,?)', [(j['mountain'], j['lat'], j['lon']) for j in arr])
    return len(arr)


def main():
    os.makedirs(DATA, exist_ok=True)
    pbf = os.path.join(DATA, 'korea.osm.pbf')
    if '--no-download' not in sys.argv or not os.path.exists(pbf):
        log(f'다운로드 시작: {PBF_URL}')
        tmp = pbf + '.part'
        with urllib.request.urlopen(PBF_URL, timeout=60) as r, open(tmp, 'wb') as f:
            total = int(r.headers.get('Content-Length') or 0)
            src_date = r.headers.get('Last-Modified', '')
            got, last = 0, 0
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
                got += len(b)
                if got - last > 30 << 20:
                    last = got
                    log(f'  {got >> 20}MB / {total >> 20}MB')
        os.replace(tmp, pbf)
        open(pbf + '.date', 'w').write(src_date)
        log('다운로드 완료')
    src_date = open(pbf + '.date').read() if os.path.exists(pbf + '.date') else ''

    out = DB + '.new'
    for p in (out, out + '-journal'):
        if os.path.exists(p):
            os.remove(p)
    db = sqlite3.connect(out)
    db.executescript('PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;' + SCHEMA)
    log('행정구역·큰 호수(관계) 목록 읽는 중…')
    rc = RelCollector()
    rc.apply_file(pbf)
    log(f'  관계 {len(rc.rels):,}개, 테두리 선 {len(rc.need):,}개')
    h = Handler(db, rc.need)
    idx = os.path.join(DATA, 'nodes.idx')
    log('OSM 데이터 읽는 중 (NAS 기준 10분 안팎)…')
    t0 = time.time()
    h.apply_file(pbf, locations=True, idx=f'sparse_file_array,{idx}')
    h.flush()
    try:
        os.remove(idx)  # 노드 좌표 임시 색인(수백 MB) 정리 — Windows에선 아직 열려 있으면 실패하므로 무시
    except OSError:
        pass
    log(f'읽기 완료: 점 {h.np:,}개, 선 {h.nl:,}개 ({time.time() - t0:.0f}초)')
    na, rows, idxs = 0, [], []
    for rid, k, t, outer in rc.rels:
        rings = [rdp(r, EPS[k]) for r in stitch([h.wgeom[i] for i in outer if i in h.wgeom])]
        rings = [r for r in rings if len(r) >= 4]
        if not rings:
            continue
        lats = [p[0] for r in rings for p in r]
        lons = [p[1] for r in rings for p in r]
        na += 1
        rows.append((na, f'r{rid}', k, t.get('name:ko') or t.get('name'), json.dumps(t, ensure_ascii=False), json.dumps(rings, separators=(',', ':'))))
        idxs.append((na, min(lats), max(lats), min(lons), max(lons)))
    db.executemany('INSERT INTO area VALUES(?,?,?,?,?,?)', rows)
    db.executemany('INSERT INTO area_idx VALUES(?,?,?,?,?)', idxs)
    h.wgeom = None
    log(f'면 {na:,}개 (행정구역·호수) 조립 완료')
    nj = import_junctions(db, os.environ.get('JUNCTIONS_JS', '/srv/mountain_junctions.js'))
    log(f'갈림길 {nj:,}개 추가')
    db.executescript('CREATE INDEX poi_name ON poi(name); CREATE INDEX poi_kind ON poi(kind); CREATE INDEX line_kind ON line(kind); CREATE INDEX area_kind ON area(kind);')
    meta = {'built_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'source_url': PBF_URL, 'source_date': src_date,
            'poi_count': str(h.np), 'line_count': str(h.nl), 'area_count': str(na), 'junction_count': str(nj)}
    db.executemany('INSERT INTO meta VALUES(?,?)', meta.items())
    db.commit()
    db.close()
    os.replace(out, DB)
    log('DB 교체 완료 ✔')


if __name__ == '__main__':
    main()
