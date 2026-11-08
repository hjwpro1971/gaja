"""지도 DB 서버 — gaja-tools(8444)의 /mapdb/ 로 중계된다.

GET  /mapdb/                 관리 화면(조회·통계·갱신)
GET  /mapdb/api/bbox         s,w,n,e 범위의 점·선을 Overpass와 같은 JSON 형식으로
                             (only=hiking 이면 등산로(highway=path)만)
GET  /mapdb/api/stats        종류별 개수, 생성 시각, 원본 날짜
GET  /mapdb/api/search?q=    이름 검색 (kind= 로 종류 제한)
GET  /mapdb/api/refresh      갱신 진행 상태·로그
POST /mapdb/api/refresh      OSM 최신 데이터 받아 DB 다시 만들기(백그라운드)
GET  /mapdb/api/pois         tourist_map용: {osm:[KoreaPOIs 형식], kakao:[...], custom:[...]} (kakao=1이면 카카오 칸 갱신)
GET  /mapdb/api/custom       직접 입력한 지점 전체 (tourist_map의 MountainPOIs)

bbox·pois 에 kakao=1 을 붙이면 그 범위의 카카오 장소(30일 보관, extra.py)를 함께 준다.
"""
import json, os, sqlite3, subprocess, sys, threading, time
import extra
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DATA = os.environ.get('DATA_ROOT', '/data')
DB = os.path.join(DATA, 'mapdb.sqlite')
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_AREA = 0.6  # 한 번에 조회할 수 있는 범위(위도×경도 제곱도). 약 60km×50km

job = {'running': False, 'started': None, 'ended': None, 'ok': None, 'log': []}
local = threading.local()


def conn():
    """스레드마다 읽기 전용 연결. DB 파일이 새로 바뀌면(갱신 후) 다시 연다."""
    try:
        st = os.stat(DB).st_ino, os.stat(DB).st_mtime
    except FileNotFoundError:
        return None
    c = getattr(local, 'c', None)
    if c is None or local.st != st:
        if c is not None:
            c.close()
        local.c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, check_same_thread=False)
        local.st = st
    return local.c


def bbox_query(s, w, n, e, only=None):
    c = conn()
    if c is None:
        raise RuntimeError('DB가 아직 없습니다. 관리 화면에서 「데이터 갱신」을 먼저 실행하세요.')
    els = []
    if only != 'hiking':
        for osm, tags, lat, lon in c.execute(
                'SELECT p.osm,p.tags,p.lat,p.lon FROM poi_idx i JOIN poi p ON p.id=i.id '
                'WHERE i.minlat<=? AND i.maxlat>=? AND i.minlon<=? AND i.maxlon>=?', (n, s, e, w)):
            els.append({'type': 'node', 'id': int(osm[1:]), 'lat': lat, 'lon': lon, 'tags': json.loads(tags)})
        # 면(행정구역·관계로 그려진 큰 호수): rings = [[[lat,lon],...], ...] 바깥 테두리들. 예전 DB엔 표가 없을 수 있음
        try:
            for osm, tags, geom in c.execute(
                    'SELECT a.osm,a.tags,a.geom FROM area_idx i JOIN area a ON a.id=i.id '
                    'WHERE i.minlat<=? AND i.maxlat>=? AND i.minlon<=? AND i.maxlon>=?', (n, s, e, w)):
                els.append({'type': 'area', 'id': int(osm[1:]), 'tags': json.loads(tags), 'rings': json.loads(geom)})
        except sqlite3.OperationalError:
            pass
    sql = ('SELECT l.osm,l.tags,l.geom FROM line_idx i JOIN line l ON l.id=i.id '
           'WHERE i.minlat<=? AND i.maxlat>=? AND i.minlon<=? AND i.maxlon>=?')
    args = [n, s, e, w]
    if only == 'hiking':
        sql += " AND l.kind='highway=path'"
    for osm, tags, geom in c.execute(sql, args):
        els.append({'type': 'way', 'id': int(osm[1:]), 'tags': json.loads(tags),
                    'geometry': [{'lat': a, 'lon': b} for a, b in json.loads(geom)]})
    return {'version': 0.6, 'generator': 'gaja-mapdb', 'elements': els}


def osm_pois(s, w, n, e):
    """tourist_map의 KoreaPOIs와 같은 모양: {lat,lon,name,natural,amenity,tourism,information,highway,ele}"""
    c = conn()
    if c is None:
        return []
    out = []
    for tags, lat, lon, ele in c.execute(
            'SELECT p.tags,p.lat,p.lon,p.ele FROM poi_idx i JOIN poi p ON p.id=i.id '
            'WHERE i.minlat<=? AND i.maxlat>=? AND i.minlon<=? AND i.maxlon>=? AND p.kind NOT LIKE ?', (n, s, e, w, 'place=%')):
        t = json.loads(tags)
        out.append({'lat': lat, 'lon': lon, 'name': t.get('name:ko') or t.get('name'), 'natural': t.get('natural', ''),
                    'amenity': t.get('amenity', ''), 'tourism': t.get('tourism', ''), 'information': t.get('information', ''),
                    'highway': '', 'ele': ele})
    return out


def stats():
    c = conn()
    if c is None:
        return {'ready': False, 'extra': extra.stats()}
    meta = dict(c.execute('SELECT k,v FROM meta'))
    kinds = {}
    for t in ('poi', 'line', 'area'):
        try:
            kinds[t] = dict(c.execute(f'SELECT kind,count(*) FROM {t} GROUP BY kind'))
        except sqlite3.OperationalError:
            kinds[t] = {}
    return {'ready': True, 'meta': meta, 'kinds': kinds, 'junctions': c.execute('SELECT count(*) FROM junction').fetchone()[0],
            'size_mb': round(os.path.getsize(DB) / 1048576, 1), 'extra': extra.stats(), 'sched': sched}


def search(q, kind=None, limit=100):
    c = conn()
    if c is None or not q:
        return []
    sql, args = 'SELECT kind,name,ele,lat,lon FROM poi WHERE name LIKE ?', [f'%{q}%']
    if kind:
        sql += ' AND kind=?'
        args.append(kind)
    sql += ' ORDER BY (name=?) DESC, ele DESC LIMIT ?'
    args += [q, limit]
    return [dict(zip(('kind', 'name', 'ele', 'lat', 'lon'), r)) for r in c.execute(sql, args)]


def run_refresh():
    job.update(running=True, started=time.strftime('%Y-%m-%d %H:%M:%S'), ended=None, ok=None, log=[])
    try:
        p = subprocess.Popen([sys.executable, '-u', os.path.join(HERE, 'build.py')], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, env=os.environ)
        for line in p.stdout:
            job['log'].append(line.rstrip())
            del job['log'][:-300]
        job['ok'] = p.wait() == 0
    except Exception as ex:  # noqa
        job['log'].append(f'오류: {ex}')
        job['ok'] = False
    job.update(running=False, ended=time.strftime('%Y-%m-%d %H:%M:%S'))


class H(BaseHTTPRequestHandler):
    def send(self, code, body, ctype='application/json; charset=utf-8'):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST')
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path.rstrip('/'), {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if p in ('', '/mapdb'):
                return self.send(200, open(os.path.join(HERE, 'index.html'), 'rb').read(), 'text/html; charset=utf-8')
            if p == '/mapdb/api/bbox':
                s, w, n, e = (float(q[k]) for k in ('s', 'w', 'n', 'e'))
                if (n - s) * (e - w) > MAX_AREA or n <= s or e <= w:
                    return self.send(400, {'error': '범위가 너무 크거나 잘못됐습니다.'})
                kk = q.get('kakao') == '1' and q.get('only') != 'hiking'
                if kk:
                    try:
                        extra.ensure_kakao(s, w, n, e)
                    except Exception as ex:  # 카카오 실패해도 OSM 결과는 준다
                        sys.stderr.write(f'kakao 실패: {ex}' + chr(10))
                r = bbox_query(s, w, n, e, q.get('only'))
                if q.get('only') != 'hiking':
                    r['elements'] += extra.as_nodes([x for x in r['elements'] if x['type'] == 'node'], s, w, n, e, kk)
                return self.send(200, r)
            if p == '/mapdb/api/pois':
                s, w, n, e = (float(q[k]) for k in ('s', 'w', 'n', 'e'))
                if (n - s) * (e - w) > MAX_AREA or n <= s or e <= w:
                    return self.send(400, {'error': '범위가 너무 크거나 잘못됐습니다.'})
                kk = q.get('kakao') == '1'
                if kk:
                    try:
                        extra.ensure_kakao(s, w, n, e)
                    except Exception as ex:
                        sys.stderr.write(f'kakao 실패: {ex}' + chr(10))
                return self.send(200, {'osm': osm_pois(s, w, n, e), 'custom': extra.custom_in(s, w, n, e),
                                       'kakao': extra.kakao_in(s, w, n, e) if kk else []})
            if p == '/mapdb/api/custom':
                return self.send(200, {'pois': extra.custom_in()})
            if p == '/mapdb/api/stats':
                return self.send(200, stats())
            if p == '/mapdb/api/search':
                return self.send(200, search(q.get('q', '').strip(), q.get('kind') or None))
            if p == '/mapdb/api/refresh':
                return self.send(200, job)
            return self.send(404, {'error': 'not found'})
        except RuntimeError as ex:
            return self.send(503, {'error': str(ex)})
        except (KeyError, ValueError):
            return self.send(400, {'error': '잘못된 요청입니다.'})

    def do_POST(self):
        if urlparse(self.path).path.rstrip('/') == '/mapdb/api/refresh':
            if job['running']:
                return self.send(409, {'error': '이미 갱신 중입니다.', **job})
            threading.Thread(target=run_refresh, daemon=True).start()
            time.sleep(0.2)
            return self.send(202, job)
        return self.send(404, {'error': 'not found'})

    def log_message(self, fmt, *a):
        if '/api/refresh' not in self.path:
            sys.stderr.write('%s %s\n' % (self.log_date_time_string(), fmt % a))


def scheduler():
    """자동 갱신 — 6시간마다 확인. OSM DB가 30일 넘었으면 새로 만들고, 카카오 칸은 30일 되기 전에 미리 다시 받는다.
    사람이 아무것도 안 해도 항상 최신이 되게 하는 장치(2026-11-08 사용자 요청)."""
    time.sleep(60)
    while True:
        try:
            c = conn()
            built = dict(c.execute('SELECT k,v FROM meta')).get('built_at') if c else None
            age = (time.time() - time.mktime(time.strptime(built, '%Y-%m-%d %H:%M:%S'))) / 86400 if built else 999
            if age >= OSM_REFRESH_DAYS and not job['running']:
                print(f'[자동 갱신] OSM DB {age:.0f}일 경과 → 새로 만들기', flush=True)
                run_refresh()
            n = extra.refresh_expiring()
            if n:
                print(f'[자동 갱신] 카카오 {n}칸 다시 받음', flush=True)
            sched['last_check'] = time.strftime('%Y-%m-%d %H:%M:%S')
        except Exception as ex:  # noqa — 스케줄러는 절대 죽지 않게
            print(f'[자동 갱신] 오류: {ex}', flush=True)
        time.sleep(6 * 3600)


OSM_REFRESH_DAYS = 30
sched = {'last_check': None, 'every_hours': 6, 'osm_days': OSM_REFRESH_DAYS}


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8000'))
    n = extra.import_custom_once(os.path.join(DATA, 'mountain_pois.json'))
    if n:
        print(f'직접 입력 지점 {n}개 가져옴 (mountain_pois.json)', flush=True)
    threading.Thread(target=scheduler, daemon=True).start()
    print(f'mapdb listening on {port}, db={DB}, kakao={"on" if extra.KAKAO_KEY else "off"}', flush=True)
    ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()
