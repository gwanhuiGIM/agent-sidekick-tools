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
assert i == {'at': T0, 'ctx': 200_012, 'ttl': 3600, 'compacted': False}, i
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
assert cache_state(i, T0 + 49 * 60)['level'] == 'ok'
assert cache_state(i, T0 + 51 * 60) == {'age': 3060, 'left': 540, 'ctx': 200_012, 'compacted': False, 'level': 'warn'}
assert cache_state(i, T0 + 61 * 60)['level'] == 'expired'
assert [cache_state(i, T0 + s)['level'] for s in (2999, 3000, 3599, 3600)] == ['ok', 'warn', 'warn', 'expired']  # 경계
assert cache_state(dict(i, ttl=None), T0 + 50 * 60)['level'] == 'warn' and cache_state(dict(i, ttl=300), T0 + 224)['level'] == 'ok' and cache_state(dict(i, ttl=300), T0 + 225)['level'] == 'warn'
assert cache_state(dict(i, ctx=99_999), T0 + 50 * 60)['level'] == 'ok'  # 작은 세션은 경고 안 함
c = cache_info(L + [json.dumps({'type': 'system', 'subtype': 'compact_boundary', 'timestamp': '2026-10-27T13:41:00Z'})])
assert c['compacted'] and cache_state(c, T0 + 50 * 60)['level'] == 'ok'  # compact 뒤엔 경고 안 함
assert cache_state(None, T0) is None

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
    assert post(add, 'application/json; charset=utf-8') == 200 and ck.read_text(encoding='utf-8') == '- [ ] A\n- [ ] B\n'
    assert len(json.loads(urllib.request.urlopen(url.replace('checklist', 'state')).read())['postings']) == 3  # example 공고
    srv.shutdown()
print('OK4')
