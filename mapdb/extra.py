"""OSM 밖의 지점: 직접 입력한 지점(영구) + 카카오 장소(30일 보관).

data/extra.sqlite 에 따로 둔다 — OSM DB(mapdb.sqlite)는 「데이터 갱신」 때 통째로 새로 만들어지므로
여기 있는 것까지 지워지면 안 된다.

카카오 장소는 0.05°(약 5km) 칸 단위로 받아 두고, 받은 지 30일이 지나면 지운다.
지운 칸은 다음에 그 지역 지도를 그릴 때 다시 받는다 → "30일마다 업데이트".
카카오 API로 받은 데이터를 영구 보관하지 않기 위한 장치이므로 기간을 늘리지 말 것.
"""
import json, math, os, re, sqlite3, threading, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

DATA = os.environ.get('DATA_ROOT', '/data')
XDB = os.path.join(DATA, 'extra.sqlite')
KAKAO_KEY = os.environ.get('KAKAO_REST_KEY', '').strip()
KEEP_DAYS = 30
CELL = 0.05
MAX_CELLS = 80  # 한 번 요청에 새로 받을 칸 상한(가운데부터). 나머지는 다음 요청 때

# 카카오 키워드 검색어 — 산행 지도에 쓸 만한 것만. 결과는 아래 분류·잡음 필터를 통과해야 저장된다
KEYWORDS = ['약수터', '샘터', '쉼터', '정자', '암자', '사찰', '폭포', '전망대', '대피소', '산장', '계곡', '바위', '등산로입구']
NOISE_NAME = re.compile(r'식당|가든|모텔|호텔|리조트|펜션|민박|주유소|편의점|약국|병원|은행|파출소|아파트|빌라|카페|치킨|마트|슈퍼|정육|교회|성당|성결|원룸|부동산|노래|휴게소$')
NOISE_CAT = re.compile(r'음식점|숙박|부동산|의료|금융|교육|가정,생활|자동차|교통,수송|서비스,산업|사무,기업|쇼핑|테마파크|도예|농원|드라이브|체험|캠핑|골프|레저|카페|공방|미술관|박물관|전시')
# 분류 안 되는 일반 관광지는 산행 지도에 어울리는 것만 남긴다
KEEP_POI_CAT = re.compile(r'국립공원|도립공원|군립공원|자연휴양림|휴양림|유적|문화재|사적|성곽|산성|명승|관문|기념물|천연|수목원|숲')
GENERIC = {'정자', '쉼터', '약수터', '샘터', '전망대', '폭포', '계곡', '바위', '산장', '대피소', '등산로입구', '등산로', '사찰', '암자'}
ENG = re.compile(r"^[A-Za-z0-9 .\-'()]+$")

_lock = threading.Lock()
_fetch_lock = threading.Lock()


def db():
    c = sqlite3.connect(XDB, timeout=30, check_same_thread=False)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS custom_poi(id INTEGER PRIMARY KEY AUTOINCREMENT, mountain TEXT, name TEXT, lat REAL, lon REAL,
        ele REAL, type TEXT, note TEXT, created TEXT);
    CREATE TABLE IF NOT EXISTS kakao_poi(kid TEXT PRIMARY KEY, cell TEXT, name TEXT, lat REAL, lon REAL, category TEXT,
        type TEXT, fetched REAL);
    CREATE INDEX IF NOT EXISTS kakao_ll ON kakao_poi(lat, lon);
    CREATE INDEX IF NOT EXISTS kakao_cellx ON kakao_poi(cell);
    CREATE TABLE IF NOT EXISTS kakao_cell(cell TEXT PRIMARY KEY, fetched REAL, n INTEGER);
    """)
    # 2026-11-08: tourist_map에서 옮긴 90개는 좌표가 대부분 틀려(OSM 대조 63개가 0.3~8.6km 어긋남, 백담사 7.4km 등)
    # 사용 중지로 표시. 지우지는 않음 — active=1 인 것(앞으로 사람이 직접 확인해 넣는 것)만 내보낸다.
    cols = [r[1] for r in c.execute('PRAGMA table_info(custom_poi)')]
    if 'active' not in cols:
        c.execute('ALTER TABLE custom_poi ADD COLUMN active INTEGER DEFAULT 1')
        c.execute("UPDATE custom_poi SET active=0, note='tourist_map 원본 — 좌표 부정확(2026-11-08 OSM 대조: 90개 중 63개 0.3km 이상 어긋남), 사용 중지' WHERE note='tourist_map 원본'")
        c.commit()
    return c


def import_custom_once(path):
    """tourist_map의 data/mountain_pois.json(직접 입력한 지점)을 처음 한 번만 옮겨 담는다."""
    if not os.path.exists(path):
        return 0
    with _lock:
        c = db()
        if c.execute('SELECT count(*) FROM custom_poi').fetchone()[0]:
            return 0
        pois = json.load(open(path, encoding='utf-8')).get('pois', [])
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        c.executemany('INSERT INTO custom_poi(mountain,name,lat,lon,ele,type,note,created) VALUES(?,?,?,?,?,?,?,?)',
                      [(p.get('mountain'), p['name'], p['lat'], p['lon'], p.get('ele'), p.get('type') or 'poi', 'tourist_map 원본', now)
                       for p in pois if p.get('name') and p.get('lat') is not None])
        c.commit()
        return len(pois)


def classify(cat, name):
    if re.search(r'산$|봉$|정상', name):
        return 'peak'
    if '폭포' in name or '폭포' in cat:
        return 'waterfall'
    if re.search(r'대피소|산장', name):
        return 'shelter'
    if re.search(r'샘터|약수|식수', name) or '약수터' in cat:
        return 'spring'
    if re.search(r'전망대|조망', name) or '전망대' in cat:
        return 'viewpoint'
    if re.search(r'계곡', name) or '계곡' in cat:
        return 'valley'
    if re.search(r'사$|암$|사찰|선원', name) or '사찰' in cat:
        return 'temple'
    if re.search(r'쉼터|정자', name) or '정자' in cat:
        return 'rest'
    if '바위' in name:
        return 'rock'
    return 'poi'


def _get(url):
    # 이 키는 웹(JavaScript)용 앱 키라 KA 헤더(출처)가 필요하다 — tourist_map이 보내던 값과 같게
    req = urllib.request.Request(url, headers={'Authorization': f'KakaoAK {KAKAO_KEY}', 'KA': os.environ.get('KAKAO_KA', 'sdk/1.0 os/web origin/http://192.168.219.109')})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _fetch_one(cell, query):
    """한 칸 × 검색어 하나: 최대 3쪽(45건)."""
    la, lo = (float(v) for v in cell.split(','))
    rect = f'{lo},{la},{lo + CELL},{la + CELL}'
    docs = []
    for page in (1, 2, 3):
        if query == '@AT4':
            url = f'https://dapi.kakao.com/v2/local/search/category.json?category_group_code=AT4&rect={rect}&page={page}&size=15'
        else:
            url = f'https://dapi.kakao.com/v2/local/search/keyword.json?query={urllib.parse.quote(query)}&rect={rect}&page={page}&size=15'
        try:
            j = _get(url)
        except Exception:
            break
        docs += j.get('documents', [])
        if j.get('meta', {}).get('is_end', True):
            break
    return cell, docs


def cells_for(s, w, n, e):
    out = []
    for i in range(math.floor(s / CELL), math.floor(n / CELL) + 1):
        for j in range(math.floor(w / CELL), math.floor(e / CELL) + 1):
            out.append(f'{round(i * CELL, 2)},{round(j * CELL, 2)}')
    cy, cx = (s + n) / 2, (w + e) / 2
    out.sort(key=lambda k: (float(k.split(',')[0]) + CELL / 2 - cy) ** 2 + (float(k.split(',')[1]) + CELL / 2 - cx) ** 2)
    return out


def purge_old(c):
    lim = time.time() - KEEP_DAYS * 86400
    c.execute('DELETE FROM kakao_poi WHERE fetched < ?', (lim,))
    c.execute('DELETE FROM kakao_cell WHERE fetched < ?', (lim,))
    c.commit()


def ensure_kakao(s, w, n, e):
    """범위를 덮는 칸 중 아직 없거나 30일 지난 칸을 카카오에서 받는다. 반환: 새로 받은 칸 수."""
    if not KAKAO_KEY:
        return 0
    with _fetch_lock:
        c = db()
        purge_old(c)
        have = {r[0] for r in c.execute('SELECT cell FROM kakao_cell')}
        need = [k for k in cells_for(s, w, n, e) if k not in have][:MAX_CELLS]
        return _fetch_cells(c, need)


def refresh_expiring(limit=200):
    """(스케줄러용) 30일이 다 되어 가는 칸(27일 이상)을 미리 다시 받아 바꿔 넣는다 — 지워졌다가 비는 기간이 없게.
    한 번에 limit칸까지(카카오 일일 호출 한도 보호). 반환: 다시 받은 칸 수."""
    if not KAKAO_KEY:
        return 0
    with _fetch_lock:
        c = db()
        old = [r[0] for r in c.execute('SELECT cell FROM kakao_cell WHERE fetched < ? ORDER BY fetched LIMIT ?',
                                       (time.time() - (KEEP_DAYS - 3) * 86400, limit))]
        n = _fetch_cells(c, old)
        purge_old(c)  # 다시 받으면서 사라진 장소(오래된 행)는 정리
        return n


def _fetch_cells(c, need):
    if not need:
        return 0
    jobs = [(k, q) for k in need for q in ['@AT4'] + KEYWORDS]
    bycell = {k: {} for k in need}
    with ThreadPoolExecutor(8) as ex:
        for cell, docs in ex.map(lambda a: _fetch_one(*a), jobs):
            for d in docs:
                bycell[cell][d['id']] = d
    now = time.time()
    rows = []
    for cell, docs in bycell.items():
        for d in docs.values():
            name, cat = (d.get('place_name') or '').strip(), d.get('category_name') or ''
            if len(name) < 2 or name in GENERIC or NOISE_NAME.search(name) or NOISE_CAT.search(cat) or ENG.match(name):
                continue
            t = classify(cat, name)
            if t == 'poi' and not KEEP_POI_CAT.search(cat + ' ' + name):
                continue  # 분류가 안 되는 일반 장소는 산행 지도에 어울리는 것(공원·유적·휴양림 등)만
            rows.append((d['id'], cell, name, float(d['y']), float(d['x']), cat, t, now))
    c.executemany('INSERT OR REPLACE INTO kakao_poi VALUES(?,?,?,?,?,?,?,?)', rows)
    c.executemany('INSERT OR REPLACE INTO kakao_cell VALUES(?,?,?)',
                  [(k, now, sum(1 for r in rows if r[1] == k)) for k in need])
    c.commit()
    return len(need)


def kakao_in(s, w, n, e):
    c = db()
    return [dict(zip(('kid', 'name', 'lat', 'lon', 'category', 'type', 'fetched'), r)) for r in c.execute(
        'SELECT kid,name,lat,lon,category,type,fetched FROM kakao_poi WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?', (s, n, w, e))]


def custom_in(s=None, w=None, n=None, e=None):
    c = db()
    sql, args = 'SELECT id,mountain,name,lat,lon,ele,type FROM custom_poi WHERE active=1', []
    if s is not None:
        sql += ' AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?'
        args = [s, n, w, e]
    return [dict(zip(('id', 'mountain', 'name', 'lat', 'lon', 'ele', 'type'), r)) for r in c.execute(sql, args)]


def stats():
    c = db()
    purge_old(c)
    k = c.execute('SELECT count(*), min(fetched) FROM kakao_poi').fetchone()
    return {'custom': c.execute('SELECT count(*) FROM custom_poi WHERE active=1').fetchone()[0],
            'custom_off': c.execute('SELECT count(*) FROM custom_poi WHERE active=0').fetchone()[0], 'kakao': k[0],
            'kakao_cells': c.execute('SELECT count(*) FROM kakao_cell').fetchone()[0],
            'kakao_oldest': time.strftime('%Y-%m-%d', time.localtime(k[1])) if k[1] else None,
            'kakao_types': dict(c.execute('SELECT type,count(*) FROM kakao_poi GROUP BY type')),
            'kakao_key': bool(KAKAO_KEY), 'keep_days': KEEP_DAYS}


# OSM 태그 흉내 — illust_map 등이 기존 OSM 점과 같은 방식으로 그리도록
TYPE_TAGS = {'peak': {'natural': 'peak'}, 'spring': {'natural': 'spring'}, 'shelter': {'tourism': 'alpine_hut'},
             'rest': {'amenity': 'shelter'}, 'temple': {'amenity': 'place_of_worship', 'religion': 'buddhist'},
             'waterfall': {'waterway': 'waterfall'}, 'viewpoint': {'tourism': 'viewpoint'}, 'valley': {'natural': 'valley'},
             'rock': {'natural': 'rock'}, 'pass': {'natural': 'saddle'}, 'poi': {'tourism': 'attraction'}}


def kakao_ok(p, osm_names):
    """카카오 장소를 쓸지: 봉우리·능선·바위 등 자연 지형은 카카오 좌표가 주소·입구 기준이라 자주 틀림 → OSM만 믿는다.
    OSM에 같은 이름이 범위 안에 있으면(거리 무관) 그쪽 위치가 맞으므로 카카오 것은 버린다."""
    if p['type'] in ('peak', 'rock', 'valley') or re.search(r'능선|봉$|산$|바위', p['name']):
        return False
    return p['name'].replace(' ', '') not in osm_names


def as_nodes(osm_nodes, s, w, n, e, with_kakao=True):
    """직접 입력 + 카카오 지점을 Overpass 노드 모양으로. OSM에 이미 있는 것(같은 이름 300m 안, 또는 25m 안 아무 점)은 뺀다."""
    def dup(name, lat, lon):
        for o in osm_nodes:
            dy, dx = (o['lat'] - lat) * 111000, (o['lon'] - lon) * 88000
            d2 = dy * dy + dx * dx
            if d2 < 625:
                return True
            if d2 < 90000:  # 300m 안에서 이름이 같거나 한쪽이 다른 쪽을 포함 (조령샘 ↔ 조령산 조령샘터)
                on = o['tags'].get('name:ko') or o['tags'].get('name') or ''
                if len(on) >= 2 and (on == name or on in name or name in on):
                    return True
        return False
    out = []
    for p in custom_in(s, w, n, e):
        if dup(p['name'], p['lat'], p['lon']):
            continue
        out.append({'type': 'node', 'id': -1000000 - p['id'], 'lat': p['lat'], 'lon': p['lon'],
                    'tags': {'name': p['name'], 'source': 'manual', **({'ele': str(p['ele'])} if p['ele'] else {}), **TYPE_TAGS.get(p['type'], {})}})
    if with_kakao:
        osm_names = {(o['tags'].get('name:ko') or o['tags'].get('name') or '').replace(' ', '') for o in osm_nodes}
        for p in kakao_in(s, w, n, e):
            if not kakao_ok(p, osm_names) or dup(p['name'], p['lat'], p['lon']):
                continue
            out.append({'type': 'node', 'id': -int(p['kid']), 'lat': p['lat'], 'lon': p['lon'],
                        'tags': {'name': p['name'], 'source': 'kakao', 'kakao_category': p['category'], **TYPE_TAGS.get(p['type'], {})}})
    return out
