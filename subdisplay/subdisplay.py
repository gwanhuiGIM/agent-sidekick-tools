#!/usr/bin/env python3
"""서브 디스플레이 서버: 체크리스트(markdown) 편집 + herdr 에이전트 상태 + PC 사용량 + 공고 정리 상태. stdlib만 사용.

python3 subdisplay.py   # 기본 example/checklist.md(없으면 checklist.sample.md를 복사), 포트 8765 (SUBDISPLAY_CHECKLIST·SUBDISPLAY_PORT·SUBDISPLAY_PORTFOLIO로 변경)
127.0.0.1에만 연다. 외부(태블릿) 공개는 tailscale serve로 따로.
"""
import json
import os
import re
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
WARN_LEFT = 10 * 60  # 만료까지 이만큼 남으면 경고 (1h TTL → 답변 50분 뒤). TTL이 짧으면 TTL의 1/4
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
    """transcript jsonl 줄 → 마지막 답변(subagent 제외)의 시각(epoch)·context 토큰·cache TTL(초)·그 뒤 compact 여부. 답변이 없으면 None.
    TTL은 마지막으로 cache를 쓴 답변의 종류(ephemeral_5m/1h)를 따르고, 읽은 범위에 cache 쓰기가 없으면 None."""
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
                        'ttl': t, 'compacted': False}
                ttl = t
            elif d.get('subtype') == 'compact_boundary' and info:
                info['compacted'] = True
        except (ValueError, TypeError, AttributeError, KeyError):  # 잘린 첫 줄·형식이 다른 줄은 건너뛴다
            continue
    return info


def cache_state(info, now):
    """마지막 답변 기준 prompt cache 상태. warn = 만료 전 compact하거나 이어갈 때, expired = 다음 입력이 context 전체를 다시 처리."""
    if not info:
        return None
    age, ttl = now - info['at'], info['ttl'] or 3600  # TTL 근거가 파일 전체에 없으면 1h로 본다
    level = ('ok' if info['compacted'] or info['ctx'] < CTX_WARN or age < ttl - min(WARN_LEFT, ttl // 4)
             else 'warn' if age < ttl else 'expired')
    return {'age': int(age), 'left': int(ttl - age), 'ctx': info['ctx'], 'compacted': info['compacted'], 'level': level}


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
             'cache': cache_state(transcript((a.get('agent_session') or {}).get('value')), now) if a.get('agent') == 'claude' else None}
            for a in data], None


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
            'sys': sys_state(), 'postings': posts, 'postings_error': perr,
            'file': str(CHECKLIST), 'now': time.strftime('%H:%M:%S')}


class Handler(BaseHTTPRequestHandler):
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
        if self.path != '/api/checklist':
            return self.send(404, {'error': 'not found'})
        # 같은 PC 브라우저의 다른 페이지가 보내는 simple request(text/plain 등)를 막는다 — CORS preflight가 필요한 JSON만 받는다
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            return self.send(415, {'error': 'Content-Type must be application/json'})
        try:
            size = int(self.headers.get('Content-Length', 0))
            if not 0 <= size <= MAX_BODY:  # 음수면 rfile.read(-1)이 연결이 끊길 때까지 기다린다
                return self.send(413, {'error': 'body too large'})
            op = json.loads(self.rfile.read(size))
            with LOCK:
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
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
