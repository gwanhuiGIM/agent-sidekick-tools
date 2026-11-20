#!/usr/bin/env python3
"""서브 디스플레이 서버: 체크리스트(markdown) 편집 + herdr 에이전트 상태 + PC 사용량 + 공고 정리 상태. stdlib만 사용.

python3 subdisplay.py   # 기본 example/checklist.md(없으면 checklist.sample.md를 복사), 포트 8765 (SUBDISPLAY_CHECKLIST·SUBDISPLAY_PORT·SUBDISPLAY_PORTFOLIO로 변경)
127.0.0.1에만 연다. 외부(태블릿) 공개는 tailscale serve로 따로.
"""
import json
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ITEM = re.compile(r'^(\s*[-*] \[)([ xX])(\] )(.*)$')
EXAMPLE = Path(__file__).resolve().parent / 'example'  # 데모 데이터 — 기본값이 cwd에 따라 바뀌지 않게 스크립트 기준
SAMPLE = EXAMPLE / 'checklist.sample.md'  # 추적되는 원본. 실제로 쓰는 checklist.md는 첫 실행 때 복사(.gitignore)
CHECKLIST = Path(os.environ.get('SUBDISPLAY_CHECKLIST', EXAMPLE / 'checklist.md')).expanduser()
PORT = int(os.environ.get('SUBDISPLAY_PORT', '8765'))
PAGE = Path(__file__).with_name('index.html')
STATIC = {'/manifest.webmanifest': 'application/manifest+json; charset=utf-8',  # 홈 화면 앱 설치용, 이 목록만 서빙
          '/icon-192.png': 'image/png', '/icon-512.png': 'image/png'}
PORTFOLIO = Path(os.environ.get('SUBDISPLAY_PORTFOLIO', EXAMPLE / 'portfolio')).expanduser()
SYS_INTERVAL = 3  # 초 — 사용량 샘플 주기
SYS_HIST = deque(maxlen=100)  # (시각, cpu%, mem%) 최근 5분. 서버가 가진 유일한 상태 — 재시작하면 비워진다
SYS_TOP = []  # 마지막 샘플의 이름별 상위 프로세스
PAGE_BYTES = os.sysconf('SC_PAGE_SIZE')
LOCK = threading.Lock()  # ponytail: 프로세스 내 락만 — Obsidian 동시 편집은 expect 대조(409)로 막는다
PROJECTS = Path('~/.claude/projects').expanduser()  # Claude Code transcript(<cwd 인코딩>/<session>.jsonl)
CTX_WARN = 100_000  # 이 이상 context인 세션만 만료 경고 — 작으면 다시 읽어도 싸다
COMPACT_CTX = 120_000  # 이 이상 context면 시간과 상관없이 'compact 권장' — 이어서 쓸수록 매 요청이 비싸다
WARN_LEFT = 20 * 60  # 만료까지 이만큼 남으면 경고 (1h TTL → 답변 40분 뒤). TTL이 짧으면 TTL의 1/4
# 자동 compact — 만료 임박 구간(자리를 비운 지 40분+)의 큰 세션에 herdr로 /compact를 넣는다
AUTO = os.environ.get('SUBDISPLAY_AUTOCOMPACT') == '1'  # 기본 꺼짐 — 실행 중인 agent 세션에 입력을 넣으므로 1로 켤 때만
AUTO_CTX = 120_000  # 이 이상 context만 — compact 비용(요약 출력)과 재개 뒤 절약(만료 뒤 재쓰기·요청마다 읽기가 compact 뒤 크기로 준다)의 기대 손익분기 근처. 작성자 transcript 추정(Opus 5.5 단가, 50분+ 쉰 100k+ 세션 재개율 약 38%)으로 약 124k — 재개율·사용 패턴에 따라 달라진다
AUTO_QUOTA = 90  # Claude 5h 사용량 %가 이 이상이면 안 한다 — compact도 요청 하나(context 전체 cache read + 요약 출력)
AUTO_LEFT = 7 * 60  # 초 — 만료까지 이만큼 남았을 때 보낸다(1h TTL이면 답변 53분 뒤). 판정 주기 60초 + compact 소요(69k 세션 12초, 111~168k 세션 34~56초 실측)를 빼도 만료 전으로 본다
AUTO_MIN_LEFT = 30  # 초 — 보내기 직전 다시 쟀을 때 이보다 적게 남았으면 안 보낸다(입력줄 조회 사이 만료 방지)
AUTO_EVERY = 60  # 초 — 판정 주기
NOCOMPACT_FILE = Path('~/.cache/subdisplay-nocompact.json').expanduser()  # 자동 compact 예외 session id 목록 — 화면 버튼으로 켜고 끈다
AUTO_SENT = {}  # session id → (보낼 때의 마지막 답변 시각, 보낸 시각 'HH:MM'). 같은 답변 시점엔 실패해도 1회만(반복 전송 방지), 새 답변이 오면 다시 대상. 서버를 재시작하면 비워진다
TAIL = 512 * 1024  # transcript 끝에서 읽는 바이트 — 마지막 답변만 찾으면 된다
TRANSCRIPTS = {}  # 경로 → ((크기, mtime), cache_info 결과). 안 바뀐 파일은 다시 읽지 않는다
CLAUDE_JSON = Path('~/.claude.json').expanduser()  # 지금 로그인된 계정(.oauthAccount) — /login이 바꾼다
QUOTA_FILE = Path('~/.cache/agent-quota.json').expanduser()  # Claude 5h·주간 사용량 — usage-indicator/claude-usage-indicator.py가 1분마다 쓴다
QUOTA_STALE = 5 * 60  # 초 — 이보다 오래된 값은 표시하지 않는다(indicator가 꺼졌거나 조회가 계속 실패)
MAX_BODY = 64 * 1024  # 바이트 — 체크리스트 요청 본문 상한


class Conflict(Exception):
    pass


def parse(lines):
    return [{'line': i, 'raw': l, 'done': m[2] != ' ', 'text': m[4]}
            for i, l in enumerate(lines) if (m := ITEM.match(l))]


def apply(lines, op):
    """op = {action: add|toggle|edit|delete|move, line, expect(클라이언트가 본 원래 줄), text, to, to_expect}.
    expect가 현재 줄과 다르면 파일이 다른 곳에서 바뀐 것 → Conflict.
    move: line 줄을 to 줄 자리로 옮긴다 — 아래로 옮기면 to 뒤, 위로 옮기면 to 앞(드래그 놓은 자리). 한 줄만 옮긴다(하위 들여쓴 줄은 따라가지 않음)."""
    lines = list(lines)
    action = op['action']
    text = ' '.join(str(op.get('text', '')).split())  # 줄바꿈 주입 차단
    if action in ('add', 'edit') and not text:
        raise ValueError('empty text')
    if action == 'add':
        return lines + [f'- [ ] {text}']
    i = op['line']
    if not (isinstance(i, int) and 0 <= i < len(lines)) or lines[i] != op['expect']:
        raise Conflict
    m = ITEM.match(lines[i])
    if not m:
        raise Conflict
    if action == 'toggle':
        lines[i] = f"{m[1]}{'x' if m[2] == ' ' else ' '}{m[3]}{m[4]}"
    elif action == 'edit':
        lines[i] = f'{m[1]}{m[2]}{m[3]}{text}'
    elif action == 'delete':
        del lines[i]
    elif action == 'move':
        j = op['to']
        if not (isinstance(j, int) and 0 <= j < len(lines)) or lines[j] != op['to_expect'] or not ITEM.match(lines[j]):
            raise Conflict
        lines.insert(j, lines.pop(i))
    else:
        raise ValueError(f'unknown action {action}')
    return lines


def read_lines():
    return CHECKLIST.read_text(encoding='utf-8').splitlines() if CHECKLIST.exists() else []


def write_lines(lines):
    tmp = CHECKLIST.with_name(CHECKLIST.name + '.tmp')
    tmp.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    os.replace(tmp, CHECKLIST)  # 쓰다 죽어도 원본은 온전


def cache_info(lines):
    """transcript jsonl 줄 → 마지막 답변(subagent 제외)의 시각(epoch)·context 토큰·cache TTL(초)·그 뒤 compact 여부·그 뒤 첫 요청 시각. 답변이 없으면 None.
    TTL은 마지막으로 cache를 쓴 답변의 종류(ephemeral_5m/1h)를 따르고, 읽은 범위에 cache 쓰기가 없으면 None.
    req: 답변 뒤 응답을 기다리는 요청('api-request' 줄 — Claude Code가 요청 시작 때 쓴다). 입력 직후 첫 응답 전에도 이번 요청의 cache 적중을 판정하려고.
    user 줄은 쓰지 않는다 — /model 같은 로컬 명령도 user 줄을 남겨 요청 없이 시각만 갱신된다."""
    info, ttl = None, None
    for l in lines:
        try:
            d = json.loads(l)
            if d.get('isSidechain'):
                continue
            if d.get('type') == 'assistant' and (u := d['message'].get('usage')):
                cc = u.get('cache_creation') or {}
                t = 300 if cc.get('ephemeral_5m_input_tokens') else 3600 if cc.get('ephemeral_1h_input_tokens') else ttl
                info = {'at': datetime.fromisoformat(d['timestamp'].replace('Z', '+00:00')).timestamp(),
                        'ctx': sum(u.get(k) or 0 for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')),
                        'ttl': t, 'compacted': False, 'req': None}
                ttl = t
            elif d.get('subtype') == 'compact_boundary' and info:
                info['compacted'] = True
            elif d.get('type') == 'api-request' and info and info['req'] is None:
                info['req'] = datetime.fromisoformat(d['timestamp'].replace('Z', '+00:00')).timestamp()
        except (ValueError, TypeError, AttributeError, KeyError):  # 잘린 첫 줄·형식이 다른 줄은 건너뛴다
            continue
    return info


def cache_state(info, now):
    """마지막 답변 기준 prompt cache 상태. warn = 만료 전 compact하거나 이어갈 때, expired = 다음 입력이 context 전체를 다시 처리.
    응답 대기 중인 요청이 있으면 cache는 그 요청 때 갱신된 것으로 보고 거기서부터 잰다. gap = 답변~요청 간격(초), miss = 그 간격이 TTL 이상(이번 요청이 context 전체를 다시 처리).
    big = compact 안 했고 context가 COMPACT_CTX 이상 — 시간과 무관해 작업 중에도 보인다."""
    if not info:
        return None
    ttl, req = info['ttl'] or 3600, info.get('req')  # TTL 근거가 파일 전체에 없으면 1h로 본다
    age, since = now - info['at'], now - (req or info['at'])
    level = ('ok' if info['compacted'] or info['ctx'] < CTX_WARN or since < ttl - (WARN_LEFT if ttl >= 3600 else ttl // 4)
             else 'warn' if since < ttl else 'expired')
    gap = None if req is None else int(req - info['at'])
    return {'age': int(age), 'left': int(ttl - since), 'ctx': info['ctx'], 'compacted': info['compacted'], 'level': level,
            'gap': gap, 'miss': gap is not None and not info['compacted'] and gap >= ttl,
            'big': not info['compacted'] and info['ctx'] >= COMPACT_CTX, 'ttl': ttl}


def transcript(sid):
    """session id → cache_info. 파일이 없거나 읽기 실패면 None (herdr가 아직 session id를 모를 때 등)."""
    if not re.fullmatch(r'[0-9a-f-]{36}', sid or ''):
        return None
    try:
        path = next(PROJECTS.glob(f'*/{sid}.jsonl'))
        st = path.stat()
        key = (st.st_size, st.st_mtime)
        if TRANSCRIPTS.get(path, (None,))[0] != key:
            size = TAIL
            with path.open('rb') as f:
                while True:  # 끝 TAIL부터, 답변이나 TTL 근거가 없으면(줄 하나가 수 MB일 수 있다) 4배씩 늘려 파일 전체까지
                    f.seek(max(0, st.st_size - size))
                    info = cache_info(f.read().decode('utf-8', 'replace').splitlines())
                    if (info and info['ttl']) or size >= st.st_size:
                        break
                    size *= 4
            TRANSCRIPTS[path] = (key, info)
        return TRANSCRIPTS[path][1]
    except Exception:  # 세션 하나 때문에 /api/state 전체가 실패하지 않게
        return None


def agents():
    """터미널 출력은 가져오지 않는다 — 이름·상태·위치, Claude 세션은 transcript에서 마지막 답변 시각·context 크기만."""
    try:
        r = subprocess.run(['herdr', 'agent', 'list'], capture_output=True, text=True, timeout=3)
        data = json.loads(r.stdout)['result']['agents']
    except Exception as e:  # herdr 서버 꺼짐·타임아웃·형식 변경
        return [], f'{type(e).__name__}: {e}'
    try:  # 탭 이름(사이드바 label)은 agent list에 없어 따로 조회 — 실패하면 tab_id 그대로
        r = subprocess.run(['herdr', 'tab', 'list'], capture_output=True, text=True, timeout=3)
        tabs = {t['tab_id']: t.get('label') for t in json.loads(r.stdout)['result']['tabs']}
    except Exception:
        tabs = {}
    now = time.time()
    return [{'name': a.get('name') or a.get('terminal_title_stripped') or a.get('agent', '?'),
             'agent': a.get('agent'),
             'status': a.get('agent_status', 'unknown'),
             'tab': tabs.get(a.get('tab_id')) or a.get('tab_id'),
             'cwd': Path(a.get('cwd', '')).name,
             'seq': a.get('state_change_seq', 0),
             'pane': a.get('pane_id'),
             'sid': (sid := (a.get('agent_session') or {}).get('value')),
             'auto': (AUTO_SENT.get(sid) or (None, None))[1],
             'nocompact': sid in NOCOMPACT,
             'cache': cache_state(transcript(sid), now) if a.get('agent') == 'claude' else None}
            for a in data], None


def compact_due(status, c, quota):
    """자동 compact 대상인가. 대기·완료(작업 중이면 입력이 끼어들고, blocked는 herdr가 거부) + 만료 임박(warn) 중 남은 시간 AUTO_LEFT 이하(cache가 살아 있어 compact가 싸다,
    만료 뒤면 compact 요청 자체가 context 전체를 다시 처리) + context AUTO_CTX 이상 + 응답 대기 요청 없음 + 사용량 여유(모르면 진행)
    + 1h TTL만 — 보통 세션은 1h인데 드물게 섞이는 5분 cache 쓰기가 마지막이면 5분으로 잡혀 약 4분 만에 compact된다."""
    return (status in ('idle', 'done') and c is not None and c.get('ttl') == 3600 and c['level'] == 'warn' and c['left'] <= AUTO_LEFT and not c['compacted']
            and c['ctx'] >= AUTO_CTX and c['gap'] is None and (quota is None or quota['5h'] < AUTO_QUOTA))


def input_empty(screen):
    """pane 화면 끝 → Claude Code 입력줄(`❯`)이 비었나. 쳐 둔 글이 있으면 /compact가 그 뒤에 붙는다. 입력줄을 못 찾으면 False(안 보냄).
    여러 줄 입력이면 `❯` 줄 아래 줄에도 글이 있다 — 입력 칸 아래 구분선(`─`)까지 모두 비어야 빈 것으로 본다."""
    lines = screen.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith('❯'):
            box = [lines[i].lstrip()[1:]]
            for l in lines[i + 1:]:
                if l.lstrip().startswith('─'):
                    break
                box.append(l)
            return not ''.join(box).strip()
    return False


def autocompactor():
    """AUTO_EVERY마다 compact_due + 입력줄이 빈 세션 하나에 /compact. 한 번에 하나, 같은 답변 시점엔 한 번.
    입력줄 조회(최대 5초) 사이 만료·예외 버튼·새 요청이 생길 수 있어 보내기 직전 transcript로 다시 판정한다."""
    while True:
        time.sleep(AUTO_EVERY)
        try:
            items, _ = agents()
            quota = claude_quota()
            for a in items:
                sid = a['sid']
                if not sid or sid in NOCOMPACT or not compact_due(a['status'], a['cache'], quota):
                    continue
                info = transcript(sid)
                if not info or (AUTO_SENT.get(sid) or (None,))[0] == info['at']:
                    continue
                r = subprocess.run(['herdr', 'pane', 'read', a['pane'], '--lines', '15'], capture_output=True, text=True, timeout=5)
                if r.returncode or not input_empty(r.stdout):
                    continue
                # 입력줄 조회 사이 pane에 다른 세션이 떴거나 상태가 바뀌었을 수 있다 — herdr 목록을 다시 읽어 같은 세션·상태인지 확인
                cur = next((x for x in agents()[0] if x['pane'] == a['pane']), None)
                if not cur or cur['sid'] != sid:
                    continue
                info = transcript(sid)
                c = cache_state(info, time.time())
                if sid in NOCOMPACT or not compact_due(cur['status'], c, quota) or c['left'] < AUTO_MIN_LEFT:
                    continue
                AUTO_SENT[sid] = (info['at'], time.strftime('%H:%M'))
                r = subprocess.run(['herdr', 'agent', 'prompt', a['pane'], '/compact'], capture_output=True, text=True, timeout=10)
                print(f"autocompact {time.strftime('%F %T')} {a['pane']} {a['name']!r} ctx={a['cache']['ctx']} rc={r.returncode} "
                      f"{(r.stderr or r.stdout).strip()[-200:] if r.returncode else ''}", flush=True)
                break
        except Exception as e:  # 한 번 실패로 스레드가 죽지 않게
            print(f'autocompactor: {type(e).__name__}: {e}', flush=True)


def load_nocompact():
    try:
        return set(json.loads(NOCOMPACT_FILE.read_text(encoding='utf-8')))
    except Exception:  # 없거나 깨졌으면 예외 없음
        return set()


NOCOMPACT = load_nocompact()


def set_nocompact(sid, off):
    """화면 버튼: off=True면 이 세션을 자동 compact에서 뺀다. 파일에 남겨 서버를 다시 띄워도 유지."""
    if not re.fullmatch(r'[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}', str(sid)) or not isinstance(off, bool):
        raise ValueError('sid/off')
    (NOCOMPACT.add if off else NOCOMPACT.discard)(sid)
    NOCOMPACT_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOCOMPACT_FILE.write_text(json.dumps(sorted(NOCOMPACT)), encoding='utf-8')


def account():
    """지금 로그인된 Claude 계정 이메일의 @ 앞. 실행 중인 세션도 함께 바뀌므로 세션별이 아니라 하나.
    파일이 없거나 쓰는 중이라 깨졌으면 None."""
    try:
        return json.loads(CLAUDE_JSON.read_text(encoding='utf-8'))['oauthAccount']['emailAddress'].split('@')[0] or None
    except Exception:
        return None


def claude_quota(now=None):
    """indicator가 쓴 파일에서 Claude {'5h': 사용%, '7d': 사용%}. 추가 조회 없이 읽기만 한다.
    파일이 없거나 깨졌거나 QUOTA_STALE보다 오래됐거나 claude 항목이 없으면(조회 실패) None."""
    try:
        q = json.loads(QUOTA_FILE.read_text(encoding='utf-8'))
        fetched = datetime.fromisoformat(q['fetched_at'])
        if (now or datetime.now()) - fetched > timedelta(seconds=QUOTA_STALE):
            return None
        return {k: q['claude'][k]['used'] for k in ('5h', '7d')}
    except Exception:
        return None


def cpu_times(text):
    """/proc/stat 첫 줄 → (전체 tick, idle+iowait tick). guest는 user에 이미 포함돼 앞 8개만 더한다."""
    v = [int(x) for x in text.split('\n', 1)[0].split()[1:9]]
    return sum(v), v[3] + v[4]


def cpu_percent(prev, cur):
    total = cur[0] - prev[0]
    return round(100 * (1 - (cur[1] - prev[1]) / total), 1) if total > 0 else 0.0


def mem_percent(text):
    kv = {l.split(':')[0]: int(l.split()[1]) for l in text.splitlines() if len(l.split()) > 1}
    return round(100 * (1 - kv['MemAvailable'] / kv['MemTotal']), 1)


def proc_stat(text):
    """/proc/<pid>/stat → (이름, utime+stime tick, RSS 바이트). 이름에 공백·괄호가 있을 수 있어 마지막 ')'로 자른다."""
    name = text[text.index('(') + 1:text.rindex(')')]
    f = text[text.rindex(')') + 2:].split()  # f[0] = 3번째 필드(state)
    return name, int(f[11]) + int(f[12]), int(f[21]) * PAGE_BYTES


def read_procs():
    out = {}
    for p in Path('/proc').iterdir():
        if p.name.isdigit():
            try:
                out[p.name] = proc_stat((p / 'stat').read_text())
            except (OSError, ValueError, IndexError):  # 그 사이 종료된 프로세스
                pass
    return out


def group_top(prev, cur, total_ticks, n=6):
    """프로세스를 이름별로 묶어 메모리(RSS) 상위 n개, CPU 사용률(PC 전체 대비 %)도 함께. 구간 중 새로 뜬 프로세스는 tick 전부를 구간 사용량으로 본다."""
    groups = {}
    for pid, (name, ticks, rss) in cur.items():
        used = ticks - prev[pid][1] if pid in prev and prev[pid][0] == name else ticks
        g = groups.setdefault(name, {'name': name, 'cpu': 0, 'rss': 0, 'count': 0})
        g['cpu'] += max(used, 0)
        g['rss'] += rss
        g['count'] += 1
    for g in groups.values():
        g['cpu'] = round(100 * g['cpu'] / total_ticks, 1) if total_ticks > 0 else 0.0
        g['rss'] = round(g['rss'] / 2**20)  # MiB
    return sorted(groups.values(), key=lambda g: (-g['rss'], -g['cpu']))[:n]


def sampler():
    global SYS_TOP
    prev_cpu, prev_procs = cpu_times(Path('/proc/stat').read_text()), read_procs()
    while True:
        time.sleep(SYS_INTERVAL)
        try:
            cur_cpu, cur_procs = cpu_times(Path('/proc/stat').read_text()), read_procs()
            SYS_HIST.append((time.strftime('%H:%M:%S'), cpu_percent(prev_cpu, cur_cpu),
                             mem_percent(Path('/proc/meminfo').read_text())))
            SYS_TOP = group_top(prev_procs, cur_procs, cur_cpu[0] - prev_cpu[0])
            prev_cpu, prev_procs = cur_cpu, cur_procs
        except Exception as e:  # 샘플 하나 실패로 스레드가 죽지 않게
            print(f'sampler: {type(e).__name__}: {e}', flush=True)


def sys_state():
    return {'hist': list(SYS_HIST), 'top': SYS_TOP, 'interval': SYS_INTERVAL,
            'ncpu': os.cpu_count(), 'mem_total': round(os.sysconf('SC_PHYS_PAGES') * PAGE_BYTES / 2**20), 'load': [round(x, 2) for x in os.getloadavg()]}


def battery_info(text):
    """/sys/class/power_supply/BAT*/uevent → 잔량·상태·전력(W)·남은 시간(초)·수명(%). 장치마다 energy(µWh·µW) 또는 charge(µAh·µA) 계열만 준다."""
    kv = dict(l.split('=', 1) for l in text.splitlines() if '=' in l)
    num = lambda k: int(kv['POWER_SUPPLY_' + k]) if kv.get('POWER_SUPPLY_' + k, '').strip().lstrip('-').isdigit() else None
    unit = 'ENERGY' if num('ENERGY_NOW') is not None else 'CHARGE'
    now, full, design = num(unit + '_NOW'), num(unit + '_FULL'), num(unit + '_FULL_DESIGN')
    rate = num('POWER_NOW') if unit == 'ENERGY' else num('CURRENT_NOW')  # µW 또는 µA
    rate = abs(rate) if rate else None  # 일부 장치는 방전 중 음수
    volt = num('VOLTAGE_NOW')
    status = kv.get('POWER_SUPPLY_STATUS', 'Unknown')
    left = None
    if rate and now is not None:
        if status == 'Discharging':
            left = round(now / rate * 3600)
        elif status == 'Charging' and full:
            left = round(max(full - now, 0) / rate * 3600)
    watts = (rate / 1e6 if rate else None) if unit == 'ENERGY' else (rate * volt / 1e12 if rate and volt else None)  # 완충 등 전력 0·없음이면 None
    return {'percent': num('CAPACITY'), 'status': status, 'watts': round(watts, 1) if watts else None,
            'left': left, 'health': round(100 * full / design) if full and design else None}


def battery():
    """첫 배터리(BAT*) 상태. 배터리가 없는 PC·읽기 실패면 None — 화면에서 칸을 숨긴다."""
    for d in sorted(Path('/sys/class/power_supply').glob('BAT*')):
        try:
            return battery_info((d / 'uevent').read_text())
        except (OSError, ValueError):
            return None
    return None


def frontmatter(lines):
    if not lines or lines[0].strip() != '---':
        return {}
    meta = {}
    for l in lines[1:]:
        if l.strip() == '---':
            break
        k, _, v = l.partition(':')
        meta[k.strip()] = v.strip()
    return meta


# 공고 정리 상태 기준(실제로 제출까지 간 공고들의 요약 파일로 맞춤). 템플릿 '[기입 필요]' 표시가 남았는지는 보지 않는다 —
# 템플릿에 "빈 칸을 부족으로 판정하지 않는다"고 적혀 있고, 표시를 지우지 않고 아래에 이어 적는 경우가 많다.
JD_DUTY = re.compile(r'(담당|주요|수행|세부)\s*업무|업무\s*(내용|소개)|직무\s*(내용|소개|설명)|어떤 일|하는 일|Responsibilit|What you.?ll do', re.I)
JD_REQ = re.compile(r'자격\s*(요건|사항|조건)|지원\s*자격|필수\s*(요건|조건|역량|사항)|필요\s*역량|우대|이런 분|찾습니다|Requirement|Qualification', re.I)
CI_ENOUGH = 500  # 글자 — 회사정보(2·3절) 이만큼 있으면 O. 자소서까지 쓴 공고(최소 1천 자대)와 덜 모은 공고(수백 자 미만) 사이


def sections(lines):
    """'## N.' 절 번호 → 내용 줄 목록. 인용(안내문)·제목·빈 줄·빈칸 표시('기입 필요') 줄은 뺀다 — 표시 줄 아래 이어 적은 줄은 내용으로 센다."""
    out, cur = {}, None
    for l in lines:
        if l.startswith('## '):  # '# '는 붙여넣은 글 속 제목일 수 있어 절을 끊지 않는다
            cur = (m[1] if (m := re.match(r'## (\d+)\.', l)) else None)
            continue
        t = l.strip()
        if cur is None or not t or t.startswith(('>', '#')) or '기입 필요' in t:
            continue
        out.setdefault(cur, []).append(t)
    return out


def jd_fill(sec):
    """JD(1절). full = 담당 업무와 자격 요건(또는 우대 사항)이 둘 다 보임, partial = 내용은 있는데 둘 중 하나 이상 없음, none = 내용 없음.
    줄 수가 아니라 항목을 본다 — 기업 문화 문구만 붙여넣어도 O가 되던 문제를 막는다."""
    body = sec.get('1', [])
    duty, req = any(map(JD_DUTY.search, body)), any(map(JD_REQ.search, body))
    chars = sum(map(len, body))
    state = 'none' if not body else 'full' if duty and req else 'partial'
    return {'state': state, 'chars': chars, 'why': f"담당 업무 {'O' if duty else 'X'} · 자격·우대 {'O' if req else 'X'} · {chars:,}자"}


def ci_fill(sec):
    """회사정보(2·3절). 3절은 '필요한 유형만'이라 항목 대신 양을 본다. full = CI_ENOUGH자 이상, partial = 그보다 적음, none = 없음."""
    chars = sum(len(t) for n in ('2', '3') for t in sec.get(n, []))
    state = 'none' if not chars else 'full' if chars >= CI_ENOUGH else 'partial'
    return {'state': state, 'chars': chars, 'why': f'{chars:,}자 (O 기준 {CI_ENOUGH}자)'}


def posting(summary, today):
    lines = summary.read_text(encoding='utf-8').splitlines()
    meta = frontmatter(lines)
    try:
        deadline = date.fromisoformat(meta.get('deadline', ''))
    except ValueError:
        deadline = None
    drafts = sorted(summary.parent.glob('자기소개서*.md'))
    return {'folder': summary.parent.name, 'company': meta.get('company') or summary.parent.name,
            'position': meta.get('position', ''), 'status': meta.get('status', ''),
            'deadline': deadline.isoformat() if deadline else None,
            'dday': (deadline - today).days if deadline else None,
            'jd': jd_fill(sec := sections(lines)), 'company_info': ci_fill(sec),
            'draft': {'files': [d.name for d in drafts],
                      'chars': sum(len(d.read_text(encoding='utf-8').strip()) for d in drafts)}}


def postings():
    """portfolio 바로 아래 폴더의 00_공고요약.md = 남은 공고(끝난 지원은 Done/ 아래라 잡히지 않는다). 읽기만 한다."""
    try:
        today = date.today()
        items = [posting(f, today) for f in PORTFOLIO.glob('*/00_공고요약.md')]
    except Exception as e:
        return [], f'{type(e).__name__}: {e}'
    return sorted(items, key=lambda p: (p['deadline'] is None, p['deadline'] or '', p['folder'])), None


def state():
    items, err = agents()
    posts, perr = postings()
    return {'checklist': parse(read_lines()), 'agents': items, 'agents_error': err, 'account': account(), 'quota': claude_quota(),
            'sys': sys_state(), 'battery': battery(), 'postings': posts, 'postings_error': perr,
            'file': str(CHECKLIST), 'now': time.strftime('%H:%M:%S')}


# tailnet 기기 이름 — 보통 hostname과 같다. 다르면 SUBDISPLAY_HOSTNAME으로 지정(안 하면 짧은 이름·ts.net 이름 접속이 403)
MACHINE = os.environ.get('SUBDISPLAY_HOSTNAME', socket.gethostname()).lower()
TAILNET_IP = re.compile(r'100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}')  # Tailscale 주소 대역 100.64.0.0/10
HOST = re.compile(r'([^:\[\]]+?)\.?(?::\d{1,5})?')  # 이름(끝 점 하나 허용) + 숫자 포트. IPv6 [..]는 맞지 않아 거부


def host_ok(host):
    """요청 Host가 이 PC를 가리키는 이름인가 — 127.0.0.1·localhost·기기 이름(짧은 이름·<이름>.<tailnet>.ts.net)·tailnet IP.
    악성 웹사이트가 자기 도메인을 이 PC 주소로 바꿔치기(DNS rebinding)해 화면 내용을 읽거나 고치는 것을 막는다.
    tailscale serve를 Host 검사 없는 TCP로 넘겨 IP로 접속하는 경우(브라우저 보안 DNS가 ts.net 이름을 못 찾을 때)에도 이 검사가 지킨다."""
    m = HOST.fullmatch((host or '').strip().lower())
    if not m:
        return False
    h = m[1]
    return (h in ('127.0.0.1', 'localhost', MACHINE)
            or (bool(TAILNET_IP.fullmatch(h)) and all(int(x) <= 255 for x in h.split('.')))
            or (h.startswith(MACHINE + '.') and h.endswith('.ts.net')))


def json_type(headers):
    """Content-Type이 정확히 application/json(대소문자·charset 무관) 하나인가 — application/jsonp 등·여러 개는 아님"""
    v = headers.get_all('Content-Type') or []
    return len(v) == 1 and v[0].split(';')[0].strip().lower() == 'application/json'


class Handler(BaseHTTPRequestHandler):
    def parse_request(self):  # 모든 요청 앞에서 Host 검사 — Host가 정확히 하나여야 한다(여러 개면 앞뒤 해석이 갈린다)
        if not super().parse_request():
            return False
        hosts = self.headers.get_all('Host') or []
        if len(hosts) != 1 or not host_ok(hosts[0]):
            self.send(403, {'error': 'host'})
            return False
        return True

    def send(self, code, body, ctype='application/json; charset=utf-8'):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == '/':
            return self.send(200, PAGE.read_bytes(), 'text/html; charset=utf-8')
        if self.path in STATIC:
            return self.send(200, PAGE.with_name(self.path[1:]).read_bytes(), STATIC[self.path])
        if self.path == '/api/state':
            return self.send(200, state())
        self.send(404, {'error': 'not found'})

    def do_POST(self):
        if self.path not in ('/api/checklist', '/api/nocompact'):
            return self.send(404, {'error': 'not found'})
        # 같은 PC 브라우저의 다른 페이지가 보내는 simple request(text/plain 등)를 막는다 — CORS preflight가 필요한 JSON만 받는다
        if not json_type(self.headers):
            return self.send(415, {'error': 'Content-Type must be application/json'})
        try:
            size = int(self.headers.get('Content-Length', 0))
            if not 0 <= size <= MAX_BODY:  # 음수면 rfile.read(-1)이 연결이 끊길 때까지 기다린다
                return self.send(413, {'error': 'body too large'})
            op = json.loads(self.rfile.read(size))
            with LOCK:
                if self.path == '/api/nocompact':
                    set_nocompact(op['sid'], op['off'])
                else:
                    write_lines(apply(read_lines(), op))
        except Conflict:
            return self.send(409, state())
        except (KeyError, ValueError, TypeError) as e:
            return self.send(400, {'error': str(e)})
        self.send(200, state())

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    if 'SUBDISPLAY_CHECKLIST' not in os.environ and not CHECKLIST.exists() and SAMPLE.exists():
        CHECKLIST.write_text(SAMPLE.read_text(encoding='utf-8'), encoding='utf-8')  # 데모: 추적 파일을 직접 고치지 않게 사본에 쓴다
    print(f'checklist={CHECKLIST.resolve()} http://127.0.0.1:{PORT}', flush=True)
    threading.Thread(target=sampler, daemon=True).start()
    if AUTO:
        threading.Thread(target=autocompactor, daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
