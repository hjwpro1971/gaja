"""한국등산트레킹지원센터·산림청 '100대명산' GPX(웨이포인트 분류 CATEGORY 포함)에서
산행에 도움 되는 지점만 뽑아 forest_pois.json 으로 만든다. (2022-01-12 자료 — OSM이 우선, 이 자료는 보충용)

사용: python make_forest_json.py "<100대명산 폴더>" <출력 json>
  → 출력 파일을 NAS의 mapdb/data/forest_pois.json 에 두면 컨테이너가 시작할 때 extra.sqlite 의 forest_poi 표로 가져간다.
원본 GPX·결과 json은 이용조건 확인 전이라 저장소에 올리지 않는다(.gitignore 대상 아님 — 올리지 말 것).
"""
import glob, json, os, re, sys

src, out = sys.argv[1], sys.argv[2]
SKIP_NAME = re.compile(r'^(이정표|이정목|웨이포인트\s*\d*|갈림길\s*\d*|정상|조망|조망점|전망|쉼터|정자|화장실|주차장|샘|약수터|샘터)$')
CAT = re.compile(r'<category>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</category>')
NAME = re.compile(r'<name><!\[CDATA\[(.*?)\]\]>')
ELE = re.compile(r'<ele>([\d.\-]+)</ele>')


BAD = re.compile(r'폐쇄|폐지|철거|공사|임시|없음|고갈|막힘|식당|매점|회공영|다리|입구|방향|옆|앞|내부|안내판|표지')


def kind(cat, name):
    n = name.strip()
    if BAD.search(n):
        return None
    if re.search(r'[가-힣]{2,}폭포$', n):
        return 'waterfall'
    if re.search(r'[가-힣]{2,}(대피소|산장)$', n):
        return 'shelter'
    if cat in ('ENTRY', 'SHELTER', 'INFO') and re.search(r'탐방지원센터|탐방안내소|매표소', n):
        return 'info'
    if cat == 'TOILET':
        return 'toilet'
    if cat == 'PARK' and '주차장' in n:
        return 'parking'
    if cat == 'SPRING':
        return 'spring'
    if cat == 'REST' and re.search(r'쉼터|정자|휴게', n) and len(n) >= 3 and not SKIP_NAME.match(n):
        return 'rest'
    if cat == 'VIEW' and re.search(r'전망대|조망대|전망바위', n):
        return 'viewpoint'
    if cat in ('PEAK', 'NONE', 'SCENERY') and re.fullmatch(r'[가-힣]{2,}(봉|산)', n) and n not in ('무명봉',):
        return 'peak'
    return None


rows, seen, cnt = [], set(), {}
for f in sorted(glob.glob(os.path.join(src, '*', '*.gpx'))):
    mt = os.path.basename(os.path.dirname(f))
    s = open(f, encoding='utf-8', errors='replace').read()
    for m in re.finditer(r'<wpt lat="([\d.\-]+)" lon="([\d.\-]+)">(.*?)</wpt>', s, re.S):
        la, lo, b = float(m.group(1)), float(m.group(2)), m.group(3)
        if not (33 <= la <= 39 and 124 <= lo <= 132):
            continue
        nm = NAME.search(b)
        name = re.sub(r'\s+', ' ', nm.group(1)).strip() if nm else ''
        name = re.sub(r'\s*정상$', '', name) if re.search(r'[가-힣]{2,}(봉|산)\s*정상$', name) else name
        c = CAT.search(b)
        cat = c.group(1) if c else ''
        if not name or SKIP_NAME.match(name) or re.match(r'^(웨이포인트|갈림길)', name) or len(name) < 2:
            continue
        k = kind(cat, name)
        if not k:
            continue
        key = (k, name, round(la, 4), round(lo, 4))
        if key in seen:
            continue
        seen.add(key)
        e = ELE.search(b)
        rows.append({'mountain': mt, 'name': name, 'lat': round(la, 6), 'lon': round(lo, 6),
                     'ele': round(float(e.group(1))) if e else None, 'type': k})
        cnt[k] = cnt.get(k, 0) + 1

json.dump({'source': '한국등산트레킹지원센터·산림청 100대명산 GPX 2022-01-12', 'pois': rows}, open(out, 'w', encoding='utf-8'), ensure_ascii=False)
print(len(rows), '개', cnt)
