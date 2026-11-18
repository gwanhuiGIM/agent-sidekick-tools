from subdisplay import Conflict, apply, parse

SRC = ['# 오늘', '', '- [ ] A', '- [x] B', '메모 줄', '  - [ ] C']


def raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    return False


items = parse(SRC)
assert [(i['line'], i['done'], i['text']) for i in items] == [(2, False, 'A'), (3, True, 'B'), (5, False, 'C')]

out = apply(SRC, {'action': 'toggle', 'line': 2, 'expect': '- [ ] A'})
assert out[2] == '- [x] A' and out[:2] == SRC[:2] and out[3:] == SRC[3:]  # 다른 줄 보존
assert apply(SRC, {'action': 'toggle', 'line': 3, 'expect': '- [x] B'})[3] == '- [ ] B'
assert apply(SRC, {'action': 'toggle', 'line': 5, 'expect': '  - [ ] C'})[5] == '  - [x] C'  # 들여쓰기 보존
assert apply(SRC, {'action': 'edit', 'line': 3, 'expect': '- [x] B', 'text': 'B2'})[3] == '- [x] B2'
assert apply(SRC, {'action': 'delete', 'line': 2, 'expect': '- [ ] A'}) == ['# 오늘', '', '- [x] B', '메모 줄', '  - [ ] C']
assert apply(SRC, {'action': 'add', 'text': 'D\n- [x] 주입'})[-1] == '- [ ] D - [x] 주입'  # 한 줄로 합침

# 파일이 다른 곳(Obsidian)에서 바뀐 뒤 들어온 요청은 거부
assert raises(Conflict, lambda: apply(SRC, {'action': 'toggle', 'line': 2, 'expect': '- [ ] 옛날 A'}))
assert raises(Conflict, lambda: apply(SRC, {'action': 'toggle', 'line': 4, 'expect': '메모 줄'}))  # 항목 아닌 줄
assert raises(Conflict, lambda: apply(SRC, {'action': 'delete', 'line': 99, 'expect': ''}))
assert raises(ValueError, lambda: apply(SRC, {'action': 'add', 'text': '   '}))
# move: 아래로 옮기면 대상 뒤, 위로 옮기면 대상 앞 — 항목 아닌 줄(제목·메모)은 제자리
assert apply(SRC, {'action': 'move', 'line': 2, 'expect': '- [ ] A', 'to': 5, 'to_expect': '  - [ ] C'}) == ['# 오늘', '', '- [x] B', '메모 줄', '  - [ ] C', '- [ ] A']
assert apply(SRC, {'action': 'move', 'line': 5, 'expect': '  - [ ] C', 'to': 2, 'to_expect': '- [ ] A'}) == ['# 오늘', '', '  - [ ] C', '- [ ] A', '- [x] B', '메모 줄']
assert raises(Conflict, lambda: apply(SRC, {'action': 'move', 'line': 2, 'expect': '- [ ] A', 'to': 3, 'to_expect': '- [x] 옛날 B'}))
assert raises(Conflict, lambda: apply(SRC, {'action': 'move', 'line': 2, 'expect': '- [ ] A', 'to': 4, 'to_expect': '메모 줄'}))  # 대상이 항목 아님
assert raises(Conflict, lambda: apply(SRC, {'action': 'move', 'line': 2, 'expect': '- [ ] A', 'to': 99, 'to_expect': ''}))
assert raises(KeyError, lambda: apply(SRC, {'action': 'move', 'line': 2, 'expect': '- [ ] A'}))  # to 누락 → 서버 400
assert SRC == ['# 오늘', '', '- [ ] A', '- [x] B', '메모 줄', '  - [ ] C']  # 원본 불변
print('OK')

# --- PC 사용량: /proc 텍스트를 직접 넣어 계산만 확인 ---
from subdisplay import PAGE_BYTES, ci_fill, cpu_percent, cpu_times, frontmatter, group_top, jd_fill, mem_percent, proc_stat, sections

a = cpu_times('cpu  100 0 100 700 100 0 0 0 50 0\ncpu0 1 2 3')
b = cpu_times('cpu  150 0 150 750 150 0 0 0 99 0\n')
assert a == (1000, 800) and b == (1200, 900)  # guest(9번째)는 더하지 않는다
assert cpu_percent(a, b) == 50.0 and cpu_percent(a, a) == 0.0
assert mem_percent('MemTotal:  1000 kB\nMemFree: 1 kB\nMemAvailable:  250 kB\n') == 75.0
st = '42 (Web Content (x)) S ' + ' '.join(['0'] * 10) + ' 30 12 ' + ' '.join(['0'] * 8) + ' 5 0'
assert proc_stat(st) == ('Web Content (x)', 42, 5 * PAGE_BYTES)  # 이름 속 공백·괄호
prev = {'1': ('claude', 100, 0), '2': ('claude', 50, 0), '3': ('bash', 10, 0), '9': ('old', 5, 0)}
cur = {'1': ('claude', 130, 2**20), '2': ('claude', 60, 2**20), '3': ('bash', 10, 0), '4': ('new', 20, 0)}
top = group_top(prev, cur, 200)
assert [(g['name'], g['cpu'], g['count'], g['rss']) for g in top] == [('claude', 20.0, 2, 2), ('new', 10.0, 1, 0), ('bash', 0.0, 1, 0)]
assert group_top({'1': ('a', 100, 0)}, {'1': ('b', 4, 0)}, 100)[0]['cpu'] == 4.0  # pid 재사용 → 새 프로세스로
assert [g['name'] for g in group_top({}, {'1': ('busy', 90, 0), '2': ('fat', 1, 3 * 2**20)}, 100)] == ['fat', 'busy']  # 메모리 순

# --- 공고 정리 상태 ---
POST = ['---', 'company: 회사A', 'deadline: 2026-10-29', '---', '# 회사A - 공고 요약 [기입 필요]', '> 공고 링크:',
        '## 1. 직무 정보', '- 직무명: 로봇 엔지니어', '- 우대 사항: [기입 필요]',
        '## 2. 기업 소개', '- [기입 필요] 사업 개요', '### 메모', '',
        '## 3. 회사 정보', '> 각 항목: 내용', '- **A.**: [기입 필요 또는 해당없음]', '- **B.**: 제품 X', '## 4. 일정', '- 마감 10/29']
assert frontmatter(POST) == {'company': '회사A', 'deadline': '2026-10-29'} and frontmatter(['# x']) == {}
sec = sections(POST)
assert sec == {'1': ['- 직무명: 로봇 엔지니어'], '3': ['- **B.**: 제품 X'], '4': ['- 마감 10/29']}  # 인용·빈칸 표시·하위 제목 뺌
assert jd_fill(sec)['state'] == 'partial' and ci_fill(sec) == {'state': 'partial', 'chars': 14, 'why': '14자 (O 기준 500자)'}
assert jd_fill({})['state'] == 'none' and ci_fill({})['state'] == 'none'
# 공고마다 표기가 달라도(담당업무·필수조건·어떤 일·이런 분) O
assert jd_fill({'1': ['담당업무', '. 장비 테스트', '필수조건', '* 학사 이상']})['state'] == 'full'
assert jd_fill({'1': ['어떤 일을 하나요?', '- 개발합니다', '이런 분을 찾습니다!', 'C++ 가능자']})['state'] == 'full'
assert jd_fill({'1': ['주요 업무', '- 운영', '우대사항', '- ROS']})['state'] == 'full'  # 자격 요건 없이 우대만 있는 공고
# 기업 문화 문구만 붙여넣은 JD는 줄이 많아도 △, 업무만 있고 자격·우대가 없어도 △
assert jd_fill({'1': ['- 직무명: 제어 엔지니어'] + ['함께 성장하는 즐거운 팀 문화를 만들어 갑니다'] * 30})['state'] == 'partial'
assert jd_fill({'1': ['- 담당 업무(JD 필수): 데이터 관리']})['why'] == '담당 업무 O · 자격·우대 X · 22자'
# '[기입 필요]' 표시가 남아도 아래에 적은 내용은 센다, 표시 줄 자체는 안 셈. '# '는 절을 끊지 않는다
s = sections(['## 2. 기업 소개', '- [기입 필요] 사업 개요', '# Vision', '## 3. 회사 정보', '- **H.**: [기입 필요]', '- 원격 조작'])
assert s == {'3': ['- 원격 조작']}
assert sections(['## 2. 소개', '# 붙여넣은 제목', '본문'])['2'] == ['본문']
assert ci_fill({'2': ['가' * 250], '3': ['나' * 250]})['state'] == 'full' and ci_fill({'3': ['가' * 499]})['state'] == 'partial'
print('OK2')

# --- herdr 세션 prompt cache 만료 ---
import json
from pathlib import Path
from subdisplay import cache_info, cache_state, transcript


def ans(ts, read, write=0, ttl='1h', side=False):
    cc = {'ephemeral_1h_input_tokens': write if ttl == '1h' else 0, 'ephemeral_5m_input_tokens': write if ttl == '5m' else 0}
    return json.dumps({'type': 'assistant', 'isSidechain': side, 'timestamp': ts,
                       'message': {'usage': {'input_tokens': 2, 'cache_read_input_tokens': read, 'cache_creation_input_tokens': write, 'cache_creation': cc}}})


T0 = 1793106000.0  # 2026-10-27T13:00:00Z
L = ['{"잘린 첫 줄', ans('2026-10-27T12:00:00Z', 1000, 50), ans('2026-10-27T13:00:00Z', 200_000, 10),
     ans('2026-10-27T13:30:00Z', 5, side=True),  # subagent 답변은 무시
     json.dumps({'type': 'user', 'timestamp': '2026-10-27T13:40:00Z'})]
i = cache_info(L)
assert i == {'at': T0, 'ctx': 200_012, 'ttl': 3600, 'compacted': False, 'req': None}, i  # user 줄(13:40)은 요청으로 안 본다
assert cache_info(L + [ans('2026-10-27T13:01:00Z', 200_000)])['ttl'] == 3600  # cache 쓰기 없는 답변은 앞 TTL 유지
FIVE = ans('2026-10-27T13:00:00Z', 1, 9, ttl='5m')
assert cache_info([FIVE])['ttl'] == 300
assert cache_info([FIVE, ans('2026-10-27T13:01:00Z', 200_000)])['ttl'] == 300  # 기본값(1h)과 다른 TTL도 유지
assert cache_info([ans('2026-10-27T13:00:00Z', 5)])['ttl'] is None  # TTL 근거 없음
assert cache_info(['x', '{}', '[]']) is None
BAD = [json.dumps({'type': 'assistant', 'timestamp': '2026-10-27T14:00:00Z', 'message': 'x'}),  # 형식이 다른 줄은 건너뛴다
       json.dumps({'type': 'assistant', 'timestamp': '2026-10-27T14:00:00Z', 'message': {'usage': [1]}}),
       json.dumps({'type': 'assistant', 'timestamp': '2026-10-27T14:00:00Z', 'message': {'usage': {'input_tokens': 'x'}}}),
       json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': 1}}})]
assert cache_info(L + BAD) == i
assert cache_info([json.dumps({'type': 'assistant', 'timestamp': '2026-10-27T13:00:00Z', 'message': {'usage': {'input_tokens': None, 'cache_read_input_tokens': 4}}})])['ctx'] == 4
assert cache_state(i, T0 + 39 * 60)['level'] == 'ok'
assert cache_state(i, T0 + 41 * 60) == {'age': 2460, 'left': 1140, 'ctx': 200_012, 'compacted': False, 'level': 'warn', 'gap': None, 'miss': False, 'big': True, 'ttl': 3600}
assert cache_state(i, T0 + 61 * 60)['level'] == 'expired'
assert [cache_state(i, T0 + s)['level'] for s in (2399, 2400, 3599, 3600)] == ['ok', 'warn', 'warn', 'expired']  # 경계
assert cache_state(dict(i, ttl=None), T0 + 40 * 60)['level'] == 'warn' and cache_state(dict(i, ttl=300), T0 + 224)['level'] == 'ok' and cache_state(dict(i, ttl=300), T0 + 225)['level'] == 'warn'
assert cache_state(dict(i, ctx=99_999), T0 + 40 * 60)['level'] == 'ok'  # 작은 세션은 경고 안 함
c = cache_info(L + [json.dumps({'type': 'system', 'subtype': 'compact_boundary', 'timestamp': '2026-10-27T13:41:00Z'})])
assert c['compacted'] and cache_state(c, T0 + 50 * 60)['level'] == 'ok'  # compact 뒤엔 경고 안 함
assert cache_state(None, T0) is None

# 답변 뒤 요청(api-request 줄): 첫 응답 전에도 이번 요청의 cache 적중·miss를 판정하고, 상태는 요청 시각부터 잰다
req = lambda ts, side=None: json.dumps({'type': 'api-request', 'timestamp': ts, 'querySource': 'repl_main_thread', **({'isSidechain': side} if side else {})})
r = cache_info(L + [req('2026-10-27T13:50:00Z'), req('2026-10-27T13:55:00Z')])
assert r['req'] == T0 + 50 * 60  # 답변 뒤 첫 요청
assert cache_info(L + [req('2026-10-27T13:50:00Z', side=True)])['req'] is None  # subagent 요청은 무시
assert cache_info([req('2026-10-27T12:59:00Z'), L[2]])['req'] is None  # 답변 전 요청은 그 답변이 끝냈다
assert cache_info(L + [req('2026-10-27T13:50:00Z'), ans('2026-10-27T13:51:00Z', 200_000)])['req'] is None  # 응답이 오면 해소
st = cache_state(r, T0 + 52 * 60)
assert (st['gap'], st['miss'], st['level'], st['left'], st['age']) == (3000, False, 'ok', 3480, 3120), st  # 적중 — 요청이 cache를 갱신
assert [cache_state(dict(i, req=T0 + s), T0 + 3700)['miss'] for s in (3599, 3600)] == [False, True]  # 경계
assert cache_state(dict(i, req=T0 + 2 * 3600), T0 + 2 * 3600 + 10)['level'] == 'ok'  # miss여도 다시 쓴 cache는 새것
assert cache_state(dict(i, req=T0 + 3600, compacted=True), T0 + 3700)['miss'] is False  # compact 뒤엔 판정 안 함
assert [cache_state(dict(i, ctx=n), T0)['big'] for n in (149_999, 150_000)] == [False, True]
assert cache_state(dict(i, compacted=True), T0)['big'] is False

# transcript(): 끝만 읽다가 큰 줄에 가려지면 범위를 넓혀 파일 전체까지 본다
import tempfile
import subdisplay
with tempfile.TemporaryDirectory() as tmp:
    subdisplay.PROJECTS, subdisplay.TAIL = Path(tmp), 1024
    (Path(tmp) / 'p').mkdir()
    big = json.dumps({'type': 'user', 'content': 'x' * 5000})
    def put(sid, lines):
        (Path(tmp) / 'p' / f'{sid}.jsonl').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return transcript(sid)
    s1, s2, s3 = (f'{n:08d}-0000-0000-0000-000000000000' for n in (1, 2, 3))
    assert put(s1, [FIVE, big, ans('2026-10-27T13:01:00Z', 200_000)])['ttl'] == 300  # TTL 근거가 큰 줄 너머
    assert put(s2, [L[2], big])['at'] == T0  # 마지막 답변이 큰 줄 너머
    assert put(s3, BAD) is None and transcript('../x') is None and transcript(None) is None
    assert put(s1, [L[2], 'x' * 3000])['ttl'] == 3600  # 파일이 바뀌면(크기·mtime) 다시 읽는다

    # account(): 이메일의 @ 앞만, 깨졌거나 없으면 None
    subdisplay.CLAUDE_JSON = cj = Path(tmp) / 'claude.json'
    assert subdisplay.account() is None  # 파일 없음
    cj.write_text(json.dumps({'oauthAccount': {'emailAddress': 'me.x@ex.com'}}))
    assert subdisplay.account() == 'me.x'
    cj.write_text('{"oauthAccount": {"emailAd')  # 쓰는 중
    assert subdisplay.account() is None
    cj.write_text(json.dumps({'oauthAccount': None}))
    assert subdisplay.account() is None

    # claude_quota(): indicator 파일의 Claude 5h·7d 사용%, 없거나 깨졌거나 오래됐거나 claude 조회 실패면 None
    subdisplay.QUOTA_FILE = qf = Path(tmp) / 'quota.json'
    from datetime import datetime as _dt
    now = _dt(2026, 11, 3, 13, 30)
    assert subdisplay.claude_quota(now) is None  # 파일 없음
    good = {'fetched_at': '2026-11-03T13:26:00', 'claude': {'5h': {'used': 51, 'resets_at': 'x'}, '7d': {'used': 80, 'resets_at': 'x'}}}
    qf.write_text(json.dumps(good))
    assert subdisplay.claude_quota(now) == {'5h': 51, '7d': 80}
    qf.write_text(json.dumps({**good, 'fetched_at': '2026-11-03T13:25:00'}))
    assert subdisplay.claude_quota(now) == {'5h': 51, '7d': 80}  # 정확히 5분 = 표시
    qf.write_text(json.dumps({**good, 'fetched_at': '2026-11-03T13:24:59'}))
    assert subdisplay.claude_quota(now) is None  # 5분 넘음
    qf.write_text(json.dumps({'fetched_at': '2026-11-03T13:29:00', 'codex': {}}))
    assert subdisplay.claude_quota(now) is None  # claude 조회 실패로 항목 없음
    qf.write_text('{"fetched_at": "2026-11-03T13:29')  # 쓰는 중
    assert subdisplay.claude_quota(now) is None

print('OK3')

# --- HTTP: 기본 경로가 example/ 이고, JSON이 아닌 요청·큰 본문은 체크리스트를 바꾸지 않는다 ---
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
import importlib
fresh = importlib.reload(subdisplay)  # 위에서 바꾼 전역을 되돌린다
assert fresh.PORTFOLIO == Path(fresh.__file__).resolve().parent / 'example' / 'portfolio'
with tempfile.TemporaryDirectory() as tmp:
    fresh.CHECKLIST = ck = Path(tmp) / 'c.md'
    ck.write_text('- [ ] A\n', encoding='utf-8')
    fresh.agents = lambda: ([], None)  # herdr 없이
    srv = ThreadingHTTPServer(('127.0.0.1', 0), fresh.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{srv.server_address[1]}/api/checklist'

    def post(body, ctype):
        req = urllib.request.Request(url, data=body, headers={'Content-Type': ctype}, method='POST')
        try:
            return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as e:
            return e.code
    add = json.dumps({'action': 'add', 'text': 'B'}).encode()
    assert post(add, 'text/plain') == 415 and ck.read_text(encoding='utf-8') == '- [ ] A\n'  # simple request 차단
    assert post(b'{"action": "add", "text": "' + b'x' * 70_000 + b'"}', 'application/json') == 413
    raw = __import__('http.client').client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=5)
    raw.putrequest('POST', '/api/checklist'); raw.putheader('Content-Type', 'application/json'); raw.putheader('Content-Length', '-1'); raw.endheaders()
    assert raw.getresponse().status == 413  # 음수 길이는 읽지 않고 거절
    raw.close()
    # Host 검사(DNS rebinding) — 다른 도메인·Host 없음은 403, 이 PC 이름·tailnet IP는 통과
    def get_host(host):
        c = __import__('http.client').client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=5)
        c.putrequest('GET', '/api/state', skip_host=True)
        if host is not None:
            c.putheader('Host', host)
        c.endheaders(); s = c.getresponse().status; c.close(); return s
    assert get_host('evil.com') == 403 and get_host(None) == 403 and get_host('100.64.0.1:8765') == 200
    M = fresh.MACHINE
    assert all(fresh.host_ok(h) for h in ['127.0.0.1:8765', 'localhost', '100.127.255.255', M, f'{M}.tailabc.ts.net', f'{M.upper()}.tailabc.ts.net:443'])
    assert not any(fresh.host_ok(h) for h in ['', 'evil.com', '100.63.0.1', '100.128.0.1', f'{M}.evil.com', f'x{M}.tailabc.ts.net', 'evil.ts.net', '100.65.0.1.evil.com'])
    assert post(add, 'application/json; charset=utf-8') == 200 and ck.read_text(encoding='utf-8') == '- [ ] A\n- [ ] B\n'
    assert len(json.loads(urllib.request.urlopen(url.replace('checklist', 'state')).read())['postings']) == 3  # example 공고
    srv.shutdown()
print('OK4')

# battery_info(): charge 계열(µAh·µA×V)·energy 계열(µWh·µW), 충전/방전 남은 시간, 값 없음
from subdisplay import battery_info
U = 'POWER_SUPPLY_'
chg = '\n'.join(U + l for l in ['STATUS=Charging', 'CAPACITY=19', 'CHARGE_NOW=1569000', 'CHARGE_FULL=7868000',
                                 'CHARGE_FULL_DESIGN=9450000', 'CURRENT_NOW=4522000', 'VOLTAGE_NOW=8090000'])
b = battery_info(chg)
assert b == {'percent': 19, 'status': 'Charging', 'watts': 36.6, 'left': round(6299000 / 4522000 * 3600), 'health': 83}, b
dis = '\n'.join(U + l for l in ['STATUS=Discharging', 'CAPACITY=50', 'ENERGY_NOW=30000000', 'ENERGY_FULL=60000000',
                                 'ENERGY_FULL_DESIGN=72000000', 'POWER_NOW=-15000000'])
assert battery_info(dis) == {'percent': 50, 'status': 'Discharging', 'watts': 15.0, 'left': 7200, 'health': 83}
assert battery_info(U + 'STATUS=Full\n' + U + 'CAPACITY=100') == {'percent': 100, 'status': 'Full', 'watts': None, 'left': None, 'health': None}
assert battery_info(U + 'STATUS=Discharging\n' + U + 'CAPACITY=40\n' + U + 'CHARGE_NOW=1000\n' + U + 'CURRENT_NOW=0')['left'] is None  # 전류 0
assert battery_info(U + 'STATUS=Full\n' + U + 'ENERGY_NOW=5\n' + U + 'POWER_NOW=0')['watts'] is None  # energy 계열 전력 0(완충) — 예외 나면 /api/state 전체 실패
assert battery_info(U + 'STATUS=Full\n' + U + 'ENERGY_NOW=5')['watts'] is None  # 전력 값 없음
print('OK5')

# 자동 compact 대상 판정·입력줄 검사
from subdisplay import compact_due, input_empty
W = {'level': 'warn', 'compacted': False, 'ctx': 150_000, 'gap': None, 'left': 420, 'ttl': 3600}
assert compact_due('idle', W, None) and compact_due('done', W, {'5h': 89})
assert not compact_due('working', W, None) and not compact_due('blocked', W, None)
assert not compact_due('idle', dict(W, ctx=149_999), None)  # 기준 150k
assert not compact_due('idle', dict(W, ttl=300, left=60), None)  # 1h TTL만
assert not compact_due('idle', dict(W, left=421), None) and compact_due('idle', dict(W, left=1), None)  # 만료 7분 전부터
assert not compact_due('idle', dict(W, level='ok'), None) and not compact_due('idle', dict(W, level='expired'), None)  # 만료 뒤엔 안 함
assert not compact_due('idle', dict(W, compacted=True), None) and not compact_due('idle', dict(W, gap=30), None)
assert not compact_due('idle', W, {'5h': 90}) and not compact_due('idle', None, None)
assert input_empty('출력\n──\n❯ \n──\n  🤖 Opus') and input_empty('❯')
assert not input_empty('──\n❯ 쓰다 만 글\n──') and not input_empty('입력줄 없음')
print('OK6')

# 자동 compact 예외
import subdisplay, tempfile
subdisplay.NOCOMPACT_FILE = Path(tempfile.mkdtemp()) / 'nc.json'
subdisplay.NOCOMPACT.clear()  # import 때 실제 ~/.cache 예외 목록을 읽어 온다 — 비워야 실사용 중에도 시험이 맞는다
SID = '0123abcd-0000-0000-0000-000000000000'
subdisplay.set_nocompact(SID, True)
assert SID in subdisplay.load_nocompact()
subdisplay.set_nocompact(SID, False)
assert subdisplay.load_nocompact() == set()
try:
    subdisplay.set_nocompact('../x', True)
    raise AssertionError('sid 검사 없음')
except ValueError:
    pass
print('OK7')

# autocompactor — herdr 호출·transcript는 가짜. 예외·작업 중 건너뜀, 주기마다 하나, 같은 답변 시점엔 한 번(새 답변이면 다시),
# 입력줄 조회 사이 만료 직전(AUTO_MIN_LEFT 미만)이 되거나 예외 버튼을 누르면 안 보낸다
import time
import types
NOW = time.time()
INFO = {k: {'at': NOW - 3600 + 400, 'ctx': 200_000, 'ttl': 3600, 'compacted': False, 'req': None} for k in 'abc'}
PANE = {}  # pane → (session id, 상태) 바꿔치기 — 입력줄 조회 사이 다른 세션이 뜨거나 상태가 바뀐 경우
def fake_agents():
    return ([dict(pane='p' + k, sid=PANE.get(k, (k,))[0], name=k, status=PANE.get(k, (k, 'working' if k == 'a' else 'idle'))[1],
                  cache=cache_state(INFO[k], time.time())) for k in 'abc'], None)
calls, hook, RC = [], {}, {}
def fake_run(cmd, **k):
    calls.append(cmd)
    if cmd[1] == 'pane' and cmd[3] in hook:
        hook.pop(cmd[3])()
    return types.SimpleNamespace(returncode=RC.get(cmd[3], 0) if cmd[1] == 'pane' else 0, stdout='❯ \n────' if cmd[1] == 'pane' else '', stderr='')
def run_ticks(n):
    calls.clear()
    left = [n]
    def tick(_):
        left[0] -= 1
        if left[0] < 0:
            raise StopIteration
    real = subdisplay.subprocess.run, subdisplay.time.sleep, subdisplay.agents, subdisplay.transcript, subdisplay.claude_quota
    subdisplay.subprocess.run, subdisplay.time.sleep, subdisplay.agents = fake_run, tick, fake_agents
    subdisplay.transcript, subdisplay.claude_quota = (lambda sid: dict(INFO[sid])), (lambda: None)
    try:
        subdisplay.autocompactor()
    except StopIteration:
        pass
    subdisplay.subprocess.run, subdisplay.time.sleep, subdisplay.agents, subdisplay.transcript, subdisplay.claude_quota = real
    return [c[3] for c in calls if c[1] == 'agent']
subdisplay.NOCOMPACT.clear(); subdisplay.NOCOMPACT.add('b'); subdisplay.AUTO_SENT.clear()
assert run_ticks(3) == ['pc'], calls  # a 작업 중, b 예외, c 한 번만
INFO['c']['at'] += 1  # 새 답변 → 다시 대상
assert run_ticks(2) == ['pc']
subdisplay.NOCOMPACT.clear(); subdisplay.AUTO_SENT.clear()
assert run_ticks(1) == ['pb'], calls  # b·c 둘 다 대상이어도 한 주기에 하나
for k, v in [('hook', lambda: INFO['b'].update(req=time.time())),  # 입력줄 조회 사이 새 요청
             ('hook', lambda: PANE.update(b=('other-session', 'idle'))),  # 다른 세션이 뜸
             ('hook', lambda: PANE.update(b=('b', 'working'))),  # 작업 시작
             ('rc', 1)]:  # 입력줄 조회 실패
    subdisplay.AUTO_SENT.clear()
    if k == 'hook':
        hook['pb'] = v
    else:
        RC['pb'] = v
    assert run_ticks(1) == ['pc'], calls  # b는 건너뛰고 c로
    INFO['b']['req'] = None; PANE.clear(); RC.clear()
subdisplay.NOCOMPACT.clear(); subdisplay.AUTO_SENT.clear()
hook['pb'] = lambda: INFO['b'].update(at=time.time() - 3600 + 20)  # 입력줄 조회 사이 만료 20초 전 → 안 보냄
hook['pc'] = lambda: subdisplay.NOCOMPACT.add('c')  # 입력줄 조회 사이 예외 버튼 → 안 보냄
assert run_ticks(1) == [], calls
assert input_empty('❯ \n  \n────') and not input_empty('❯ \n  둘째 줄 글\n────')  # 여러 줄 입력
try:
    subdisplay.set_nocompact(SID, 'false'); raise AssertionError('off 문자열 허용')
except ValueError:
    pass
try:
    subdisplay.set_nocompact('a' * 8 + '-' * 28, True); raise AssertionError('UUID 구조 아님 허용')
except ValueError:
    pass
print('OK8')
