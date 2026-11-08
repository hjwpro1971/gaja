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
    ('railway', {'station'}),
    ('amenity', {'school', 'place_of_worship', 'parking', 'shelter'}),
    ('tourism', {'viewpoint', 'information', 'alpine_hut', 'camp_site'}),
]
HIGHWAYS = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential',
            'track', 'path', 'footway', 'steps'}
KEEP_TAGS = {'name', 'name:ko', 'ele', 'natural', 'place', 'railway', 'amenity', 'tourism', 'highway',
             'waterway', 'tunnel', 'bridge', 'ref', 'religion', 'information', 'sac_scale', 'surface'}


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


def slim(tags):
    return {t.k: t.v for t in tags if t.k in KEEP_TAGS}


SCHEMA = """
CREATE TABLE poi(id INTEGER PRIMARY KEY, osm TEXT, kind TEXT, name TEXT, ele REAL, lat REAL, lon REAL, tags TEXT);
CREATE TABLE line(id INTEGER PRIMARY KEY, osm TEXT, kind TEXT, name TEXT, tags TEXT, geom TEXT);
CREATE VIRTUAL TABLE poi_idx USING rtree(id, minlat, maxlat, minlon, maxlon);
CREATE VIRTUAL TABLE line_idx USING rtree(id, minlat, maxlat, minlon, maxlon);
CREATE TABLE junction(id INTEGER PRIMARY KEY, mountain TEXT, lat REAL, lon REAL);
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
"""


class Handler(osmium.SimpleHandler):
    def __init__(self, db):
        super().__init__()
        self.db = db
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
        if 'name' not in n.tags:
            return
        k = poi_kind(n.tags)
        if k:
            self.add_poi(f'n{n.id}', n.tags, k, n.location.lat, n.location.lon)

    def way(self, w):
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
    h = Handler(db)
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
    nj = import_junctions(db, os.environ.get('JUNCTIONS_JS', '/srv/mountain_junctions.js'))
    log(f'갈림길 {nj:,}개 추가')
    db.executescript('CREATE INDEX poi_name ON poi(name); CREATE INDEX poi_kind ON poi(kind); CREATE INDEX line_kind ON line(kind);')
    meta = {'built_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'source_url': PBF_URL, 'source_date': src_date,
            'poi_count': str(h.np), 'line_count': str(h.nl), 'junction_count': str(nj)}
    db.executemany('INSERT INTO meta VALUES(?,?)', meta.items())
    db.commit()
    db.close()
    os.replace(out, DB)
    log('DB 교체 완료 ✔')


if __name__ == '__main__':
    main()
