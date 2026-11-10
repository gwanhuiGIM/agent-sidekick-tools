#!/usr/bin/env python3
"""Claude Code + Codex + agy 사용량을 GNOME 상단 패널에 표시한다 (ubuntu-appindicators 사용).

표시(이름 없이 순서 고정): <Claude 5h/7d 사용%> <5h 리셋까지 H:MM> | <Codex 5h/7d 사용%> <5h 리셋까지 H:MM> | <agy Gemini/Claude·GPT 주간 사용%>
공식값 출처(셋 다 토큰 0):
  Claude  `claude -p /usage --output-format json` 의 result 문장(영어) 파싱
  Codex   codex-usage.py (app-server RPC)
  agy     `agy -p /quota --output-format json` 의 command.data.groups (구조화 JSON)
메뉴의 시간별 막대(ccusage·transcript 기반)는 비용 근사치 그대로다.
"""
import glob
import html
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

# GTK·AppIndicator는 패널을 띄울 때만 import한다(main) — GTK가 없는 환경에서도 --selftest가 돌게
AppIndicator3 = GLib = Gtk = None

REFRESH_SEC = 60  # 공식 조회·ccusage 모두 1분
# 메뉴 시간대 행: 최근 24시간 중 시간당 토큰이 이 값 이상인 시간만 표시. 실측 5M이면 하루 4~6줄.
# 너무 많거나 적게 보이면 이 값만 조정한다.
HOT_HOUR_TOKENS = 5_000_000


CCUSAGE = shutil.which("ccusage") or next(  # PATH 우선, 없으면 nvm 설치 위치(데스크톱 실행은 nvm PATH가 없을 수 있다)
    iter(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/ccusage"))[::-1]),
    "/usr/local/bin/ccusage",
)


def _ccusage(*args):
    out = subprocess.run(
        [CCUSAGE, *args], capture_output=True, text=True, timeout=30
    ).stdout
    return json.loads(out)


_codex_spec = importlib.util.spec_from_file_location(
    "codex_usage", os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex-usage.py")
)
codex_usage = importlib.util.module_from_spec(_codex_spec)
_codex_spec.loader.exec_module(codex_usage)


def _run_json(cmd, timeout=30):
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    return json.loads(out)


def _left(used):
    return 100 - round(used)


def _hm(reset, now=None):
    """패널용 남은 시간 'H:MM'."""
    now = now or datetime.now()  # noqa: DTZ005 -- naive 로컬 시각(_to_local과 같은 기준)
    mins = max(0, int((reset - now).total_seconds() // 60))
    return f"{mins // 60}:{mins % 60:02d}"


def _until(reset, now=None):
    """naive 로컬 datetime까지 남은 시간 'XhYYm' (일 단위 넘으면 'Nd')."""
    now = now or datetime.now()  # noqa: DTZ005 -- naive 로컬 시각(_to_local과 같은 기준)
    mins = max(0, int((reset - now).total_seconds() // 60))
    return f"{mins // 1440}d{mins % 1440 // 60:02d}h" if mins >= 1440 else f"{mins // 60}h{mins % 60:02d}m"


_CLAUDE_RE = re.compile(
    r"Current (session|week \(all models\)): (\d+)% used · resets (?:(\w{3} \d{1,2}), )?(\d{1,2})(?::(\d{2}))?([ap]m)"
)


_MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def parse_claude_usage(text, now=None):
    """/usage result 문장 → {'5h': (used%, reset), '7d': (used%, reset)}. reset은 naive 로컬.
    ponytail: 영어 문장 정규식 — Claude Code 문구가 바뀌면 빈 dict(=패널에 err). 그때 정규식만 고친다."""
    now = now or datetime.now()  # noqa: DTZ005 -- naive 로컬 시각(_to_local과 같은 기준)
    out = {}
    for kind, used, date, hh, mm, ap in _CLAUDE_RE.findall(text):
        h = int(hh) % 12 + (12 if ap == "pm" else 0)
        if date:  # %b strptime은 LC_TIME(ko_KR)에 따라 "Oct"를 못 읽는다 → 직접 매핑
            mon, d = date.split()
            day = datetime(now.year, _MONTHS[mon], int(d))  # noqa: DTZ001
        else:
            day = now
        reset = day.replace(hour=h, minute=int(mm or 0), second=0, microsecond=0)
        if date and reset < now - timedelta(days=1):  # 연말 넘김 (Dec → Jan)
            reset = reset.replace(year=reset.year + 1)
        elif not date and reset < now:  # 날짜 생략 = 다음에 오는 그 시각
            reset += timedelta(days=1)
        out["5h" if kind == "session" else "7d"] = (int(used), reset)
    return out


def claude_usage():
    r = _run_json(["claude", "-p", "/usage", "--output-format", "json", "--no-session-persistence"])
    u = parse_claude_usage(r.get("result", ""))
    if not u:
        raise RuntimeError("unparsed /usage")
    return u


def claude_account():
    """메뉴 제목용 계정 아이디(이메일 @ 앞)."""
    return _run_json(["claude", "auth", "status"])["email"].split("@")[0]


_AGY_AUTH_RE = re.compile(r"authenticated successfully as ([^@\s]+)@")


def agy_quota():
    """([(그룹명, 잔여 0~1, reset naive 로컬), ...], 계정 아이디|None).
    agy엔 계정 조회 명령이 없어 /quota 실행 로그의 OAuth 줄에서 뽑는다 (추가 호출 0)."""
    with tempfile.NamedTemporaryFile(suffix=".log") as log:
        r = _run_json(["agy", "-p", "/quota", "--output-format", "json", "--log-file", log.name])
        m = _AGY_AUTH_RE.search(open(log.name, errors="replace").read())
    groups = [
        (g["name"], b["remaining_fraction"], _to_local(b["reset_time"]))
        for g in r["command"]["data"]["groups"]
        for b in g["buckets"]
    ]
    return groups, m and m.group(1)


def fetch_official():
    """세 서비스 공식값. 실패한 항목은 Exception 객체로 둔다(패널 전체가 죽지 않게)."""
    fns = {"claude": claude_usage, "codex": lambda: codex_usage.read_rate_limits(timeout=10, full=True), "agy": agy_quota,
           "claude_id": claude_account}
    with ThreadPoolExecutor(len(fns)) as ex:  # 직렬이면 ~11s 동안 GTK 루프가 멈춘다
        futs = {k: ex.submit(fn) for k, fn in fns.items()}
    res = {k: f.exception() or f.result() for k, f in futs.items()}
    if not isinstance(res["agy"], Exception):
        res["agy"], res["agy_id"] = res["agy"]
    for k, v in res.items():
        if isinstance(v, Exception):
            _log(f"{k} fetch failed", v)
    return res


def _log(what, e):
    """stderr로 traceback (tok 터미널)."""
    print(f"[{datetime.now():%m-%d %H:%M:%S}] {what}", file=sys.stderr)  # noqa: DTZ005
    traceback.print_exception(type(e), e, e.__traceback__, file=sys.stderr)


QUOTA_FILE = os.path.expanduser("~/.cache/agent-quota.json")  # subdisplay 등 다른 도구가 추가 조회 없이 읽는다
_AGY_SHORT = {"Gemini Models": "Gemini", "Claude and GPT models": "Claude·GPT"}


def quota_snapshot(off, now=None):
    """hook용 상태: {agent: {창: {used, resets_at}}}. 조회 실패한 서비스는 빼서 hook이 침묵하게 한다."""
    now = now or datetime.now()  # noqa: DTZ005 -- naive 로컬 시각(_to_local과 같은 기준)
    snap = {"fetched_at": now.isoformat(timespec="seconds")}
    c, x, a = off["claude"], off["codex"], off["agy"]
    if not isinstance(c, Exception):
        snap["claude"] = {k: {"used": u, "resets_at": r.isoformat()} for k, (u, r) in c.items()}
    if not isinstance(x, Exception):
        rl = x["rateLimits"]
        snap["codex"] = {k: {"used": round(w["usedPercent"]),
                             "resets_at": datetime.fromtimestamp(w["resetsAt"]).isoformat()}
                         for k, w in (("5h", rl.get("primary")), ("7d", rl.get("secondary"))) if w}
    if not isinstance(a, Exception):
        snap["agy"] = {_AGY_SHORT.get(n, n): {"used": 100 - round(f * 100), "resets_at": r.isoformat()}
                       for n, f, r in a}
    return snap


def write_quota_file(off):
    tmp = QUOTA_FILE + ".tmp"
    os.makedirs(os.path.dirname(QUOTA_FILE), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(quota_snapshot(off), f, ensure_ascii=False)
    os.replace(tmp, QUOTA_FILE)  # hook이 반쯤 쓴 파일을 읽지 않게 원자적 교체


def label_text(off):
    c, x, a = off["claude"], off["codex"], off["agy"]
    if isinstance(c, Exception):
        parts = ["err"]
    else:
        five = c.get("5h")
        week = c.get("7d")
        s = f"{five[0] if five else '?'}%/{week[0] if week else '?'}%"
        parts = [s + (f" {_hm(five[1])}" if five else "")]
    if isinstance(x, Exception):
        parts.append("err")
    else:
        rl = x["rateLimits"]
        p, s7 = rl.get("primary") or {}, rl.get("secondary") or {}
        parts.append(f"{round(p['usedPercent']) if 'usedPercent' in p else '?'}%/"
                     f"{round(s7['usedPercent']) if 'usedPercent' in s7 else '?'}%"
                     + (f" {_hm(datetime.fromtimestamp(p['resetsAt']))}" if "resetsAt" in p else ""))
    parts.append("err" if isinstance(a, Exception) else "/".join(f"{100 - round(frac * 100)}%" for _, frac, _ in a))
    return " | ".join(parts)  # 순서 고정: Claude | Codex | agy (패널 폭을 아끼려고 이름 생략)


def _row(name, left, reset):
    return f"  {name:<10} {left:>3}% left  in {_until(reset):>6} ({reset:%m/%d %H:%M})"


def _ticket_rows(rc):
    """Codex 무료 리셋 티켓: 사용 가능 장수 + 가장 먼저 만료되는 것."""
    avail = [c for c in rc.get("credits", []) if c.get("status") == "available"]
    if not avail:
        return [f"  {'resets':<10} {rc.get('availableCount', 0):>3} tickets"]
    exp = datetime.fromtimestamp(min(c["expiresAt"] for c in avail))
    head = f"  {'resets':<10} {len(avail):>3} tickets  1st exp in {_until(exp)} ({exp:%m/%d %H:%M})"
    return [head]  # 가장 먼저 만료되는 1장만 (티켓별 행은 메뉴가 길어져 아래가 잘림)


def menu_lines(off):
    """서비스별 섹션: 굵은 제목 + 등폭 행. ""는 구분선. 값은 잔여 %."""
    c, x, a = off["claude"], off["codex"], off["agy"]
    esc = html.escape  # Pango markup 이스케이프
    head = lambda t: f"<b>{esc(t)}</b>"  # noqa: E731
    rows = lambda rs: [f"<tt>{esc(r)}</tt>" for r in rs]  # noqa: E731
    err = lambda e: rows([f"  err ({type(e).__name__})"])  # noqa: E731

    cid = off.get("claude_id")
    lines = [head("Claude Code" + (f" · {cid}" if isinstance(cid, str) else ""))]
    lines += err(c) if isinstance(c, Exception) else rows(
        _row(k, _left(c[k][0]), c[k][1]) for k in ("5h", "7d") if k in c)

    title = "Codex"
    if not isinstance(x, Exception):
        rl = x["rateLimits"]
        if rl.get("planType"):
            title += f" · {rl['planType']}"
        cr = rl.get("credits")
        if cr and not cr.get("unlimited"):
            title += f" · credits {cr.get('balance')}"
    lines += ["", head(title)]
    lines += err(x) if isinstance(x, Exception) else rows(
        _row(k, _left(w["usedPercent"]), datetime.fromtimestamp(w["resetsAt"]))
        for k, w in (("5h", x["rateLimits"].get("primary")), ("7d", x["rateLimits"].get("secondary"))) if w)
    if not isinstance(x, Exception):
        lines += rows(_ticket_rows(x.get("rateLimitResetCredits") or {}))

    aid = off.get("agy_id")
    lines += ["", head("agy" + (f" · {aid}" if aid else "") + " · weekly")]
    lines += err(a) if isinstance(a, Exception) else rows(
        _row(_AGY_SHORT.get(n, n), round(f * 100), r) for n, f, r in a)
    return lines


SPARK_CHARS = " ▁▂▃▄▅▆▇█"


def _real_blocks():
    """ccusage 실블록(gap 제외). tick당 1번만 불러 아래 두 함수에 넘긴다 — transcript 전체 파싱이라 무겁다."""
    return [b for b in _ccusage("blocks", "--json")["blocks"] if not b.get("isGap")]


def token_sparkline(n=12, blocks=None):
    """최근 n개 실블록(gap 제외) 토큰 사용량을 유니코드 막대로. (bar, tooltip) 반환."""
    blocks = (_real_blocks() if blocks is None else blocks)[-n:]
    if not blocks:
        return "no data", "no data"
    hi = max(b["totalTokens"] for b in blocks) or 1
    bar = "".join(SPARK_CHARS[min(8, b["totalTokens"] * 8 // hi)] for b in blocks)
    tooltip = "\n".join(
        f"{_to_local(b['startTime']):%H:%M} {b['totalTokens']:>9,} tok  ${b['costUSD']:.2f}"
        for b in blocks
    )
    return bar, tooltip


def _to_local(iso_utc):
    # 시스템 로컬 시간대로 바꾼 뒤 tzinfo를 뗀다 — datetime.now()(naive 로컬)와 비교해야 한다
    return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
_tail_cache = {}  # path -> [offset, [(local_datetime, tokens, project), ...], 직전 (message.id, requestId)]


def _scan_transcripts():
    """~/.claude/projects/**/*.jsonl에서 (시각, 토큰, 프로젝트) 목록. 증분 파싱(파일당 새 줄만).
    재귀: subagent 기록(<세션>/subagents/*.jsonl) 포함. 한 응답이 content 블록마다 같은 usage로 여러 줄
    기록되므로 연속된 같은 (message.id, requestId)는 1번만 센다 — 2026-10-03 실측으로 ccusage blocks와 ±0.0% 일치
    (중복 제거 전엔 +57~92% 과대)."""
    events = []
    for path in glob.glob(f"{PROJECTS_DIR}/**/*.jsonl", recursive=True):
        entry = _tail_cache.setdefault(path, [0, [], None])
        size = os.path.getsize(path)
        if size < entry[0]:  # 파일이 잘렸다 → 처음부터
            entry[0], entry[1], entry[2] = 0, [], None
        if size > entry[0]:
            with open(path, encoding="utf-8", errors="replace") as f:
                f.seek(entry[0])
                for line in f:
                    if not line.endswith("\n"):  # 쓰는 중인 마지막 줄은 다음 tick에
                        break
                    entry[0] += len(line.encode())
                    try:
                        d = json.loads(line)
                        u = d["message"]["usage"]
                    except (ValueError, KeyError, TypeError):
                        continue
                    key = (d["message"].get("id"), d.get("requestId"))
                    if key == entry[2]:
                        continue
                    entry[2] = key
                    tok = (
                        u.get("input_tokens", 0)
                        + u.get("output_tokens", 0)
                        + u.get("cache_creation_input_tokens", 0)
                        + u.get("cache_read_input_tokens", 0)
                    )
                    if not (tok and d.get("timestamp")):
                        continue
                    entry[1].append(
                        (_to_local(d["timestamp"]), tok, os.path.basename(d.get("cwd", "?")) or "?")
                    )
        events += entry[1]
    return events


def hourly_usage(hours=12, now=None, blocks=None):
    """최근 n시간을 시간당 (시각, 토큰, 비용$, 대표 프로젝트)로. 비용은 블록비용을 토큰비례 배분."""
    now = now or datetime.now()  # noqa: DTZ005 -- naive 로컬 시각(_to_local과 같은 기준)
    blocks = _real_blocks() if blocks is None else blocks
    spans = [(_to_local(b["startTime"]), _to_local(b["endTime"]), b["costUSD"]) for b in blocks]
    cutoff = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours - 1)

    tok_h, proj_h = defaultdict(int), defaultdict(Counter)
    tok_block = defaultdict(int)  # 블록 인덱스 -> 총 토큰 (비용 배분 분모)
    tok_h_block = defaultdict(int)  # (시각, 블록 인덱스) -> 토큰
    for ts, tok, proj in _scan_transcripts():
        bi = next((i for i, (s, e, _) in enumerate(spans) if s <= ts < e), None)
        if bi is not None:
            tok_block[bi] += tok
        if ts < cutoff:
            continue
        h = ts.replace(minute=0, second=0, microsecond=0)
        tok_h[h] += tok
        proj_h[h][proj] += tok
        if bi is not None:
            tok_h_block[h, bi] += tok

    rows = []
    for i in range(hours):
        h = cutoff + timedelta(hours=i)
        cost = sum(
            spans[bi][2] * tok / tok_block[bi]
            for (hh, bi), tok in tok_h_block.items()
            if hh == h and tok_block[bi]
        )
        proj = proj_h[h].most_common(1)[0][0] if proj_h[h] else ""
        rows.append((h, tok_h[h], cost, proj))
    return rows


def hot_rows(hours=24, now=None, blocks=None):
    """최근 hours시간 중 사용량 많았던 시간만 (라벨, 토큰, 비용$, 프로젝트), 시간순."""
    return [
        (f"{h:%m/%d %H}:00~{h + timedelta(hours=1):%H}:00", tok, cost, proj)  # 24h 창이라 날짜로 어제/오늘 구분
        for h, tok, cost, proj in hourly_usage(hours, now, blocks)
        if tok >= HOT_HOUR_TOKENS
    ]


HOURS = 24
BAR_W = 10


def _mono_item(text):
    """등폭 폰트 메뉴 항목(표시 전용). 기본 폰트는 비례폭이라 숫자 열이 어긋난다."""
    item = Gtk.MenuItem(label=text)
    item.get_child().set_use_markup(True)
    item.set_sensitive(False)
    return item


def _fmt_tok(n):
    return f"{n / 1e6:.1f}M" if n >= 1e5 else (f"{n / 1e3:.0f}k" if n else "-")


def row_labels(rows):
    """hot_rows() 결과를 막대 + 수치 + 프로젝트 줄로. 최대치에 ← peak."""
    hi = max((t for _, t, _, _ in rows), default=0) or 1
    out = []
    for label, tok, cost, proj in rows:
        filled = round(tok * BAR_W / hi)
        bar = "█" * filled + "·" * (BAR_W - filled)
        peak = "  ← peak" if tok == hi and tok else ""
        text = f"{label:<17} {bar} {_fmt_tok(tok):>5} ${cost:5.2f}  {proj}{peak}"
        out.append(html.escape(text))
    return out


def main():
    global AppIndicator3, GLib, Gtk
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3, GLib, Gtk  # noqa: F811

    ind = AppIndicator3.Indicator.new(
        "claude-usage",
        "utilities-system-monitor",
        AppIndicator3.IndicatorCategory.SYSTEM_SERVICES,
    )
    ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

    # 메뉴 순서: [공식값 섹션(Claude/Codex/agy)] ─ graph_item ─ [시간대별 행] ─ Quit
    menu = Gtk.Menu()
    menu.append(Gtk.SeparatorMenuItem())
    graph_item = _mono_item("loading...")
    menu.append(graph_item)
    menu.append(Gtk.SeparatorMenuItem())
    FIXED_BEFORE_ROWS = 3  # 구분선, graph_item, 구분선
    menu.append(Gtk.SeparatorMenuItem())
    quit_item = Gtk.MenuItem(label="Quit")
    quit_item.connect("activate", lambda _: Gtk.main_quit())
    menu.append(quit_item)
    menu.show_all()
    ind.set_menu(menu)  # AppIndicator3는 메뉴가 없으면 아이콘을 안 띄운다

    row_items = []  # 행 개수가 매 tick 바뀔 수 있어(블록 축약/확장) 매번 새로 만든다
    codex_items = []  # 공식값 상세 섹션 (Claude/Codex/agy)

    def tick():
        # 어떤 예외든 True를 반환해야 GLib가 타이머를 지우지 않는다(False/예외 = 갱신 영구 중단)
        official = None
        try:
            official = fetch_official()
            write_quota_file(official)  # 다른 에이전트의 hook이 한도 소진을 알게
            ind.set_label(label_text(official), "100%/100% 0:00 | 100%/100% 0:00 | 100%/100%")
        except Exception as e:  # noqa: BLE001
            _log("label failed", e)
            ind.set_label(f"err ({type(e).__name__})", "")
        try:
            for it in row_items + codex_items:
                menu.remove(it)
            row_items.clear()
            codex_items.clear()
            # 공식값 섹션은 ccusage와 무관 — ccusage가 없거나 실패해도 먼저 넣어 둔다
            for line in menu_lines(official) if official else []:  # 최상단
                it = Gtk.SeparatorMenuItem() if line == "" else _mono_item(line)
                menu.insert(it, len(codex_items))
                codex_items.append(it)
            blocks = _real_blocks()
            bar, tooltip = token_sparkline(blocks=blocks)
            graph_item.set_label(f"5h블록 {bar}")
            graph_item.get_child().set_use_markup(True)
            graph_item.set_tooltip_text(tooltip)
            rows_start = len(codex_items) + FIXED_BEFORE_ROWS
            labels = row_labels(hot_rows(HOURS, blocks=blocks)) or [f"no hour ≥ {HOT_HOUR_TOKENS // 1_000_000}M tok in {HOURS}h"]
            for label in labels:
                it = _mono_item(label)
                menu.insert(it, rows_start + len(row_items))
                row_items.append(it)
        except Exception as e:  # noqa: BLE001 -- 그래프 갱신 실패가 트레이 전체를 죽이면 안 됨
            _log("menu failed", e)
            graph_item.set_label(f"menu err ({type(e).__name__})")
        finally:
            menu.show_all()  # 중간에 실패해도 이미 넣은 항목은 보이게
        return True

    tick()
    GLib.timeout_add_seconds(REFRESH_SEC, tick)
    Gtk.main()


def _selftest():
    import time

    # 기대값은 KST(UTC+9) 기준으로 적었다 — 시스템 시간대와 무관하게 같은 결과가 나오게 고정하고,
    # _to_local이 고정 +9가 아니라 로컬 시간대를 따르는지 UTC에서 한 번 더 본다
    os.environ["TZ"] = "UTC"
    time.tzset()
    assert _to_local("2026-10-03T03:00:00Z") == datetime(2026, 10, 3, 3, 0)  # noqa: DTZ001
    os.environ["TZ"] = "Asia/Seoul"
    time.tzset()
    assert _to_local("2026-10-03T03:00:00Z") == datetime(2026, 10, 3, 12, 0)  # noqa: DTZ001
    # /usage 실제 출력(Claude Code 2.1.288) 문구
    now0 = datetime(2026, 10, 3, 12, 18)  # noqa: DTZ001
    u = parse_claude_usage(
        "Current session: 6% used · resets Oct 3, 3:59pm (Asia/Seoul)\n"
        "Current week (all models): 84% used · resets Oct 6, 2:59pm (Asia/Seoul)\n", now0)
    assert u["5h"] == (6, datetime(2026, 10, 3, 15, 59)), u  # noqa: DTZ001
    assert u["7d"] == (84, datetime(2026, 10, 6, 14, 59)), u  # noqa: DTZ001
    assert _until(u["5h"][1], now0) == "3h41m"
    assert _until(u["7d"][1], now0) == "3d02h"
    u2 = parse_claude_usage("Current session: 0% used · resets 12am (Asia/Seoul)", now0)  # 날짜·분 생략형
    assert u2["5h"] == (0, datetime(2026, 10, 4, 0, 0)), u2  # noqa: DTZ001 -- 지난 시각 → 다음날
    u4 = parse_claude_usage("Current session: 1% used · resets 3:59pm", datetime(2026, 10, 3, 9, 0))  # noqa: DTZ001
    assert u4["5h"][1] == datetime(2026, 10, 3, 15, 59), u4  # noqa: DTZ001 -- 아직 안 온 시각 → 오늘
    import locale
    try:  # 월 파싱이 LC_TIME에 안 흔들리는지 (ko_KR 로캘이 없으면 건너뜀)
        locale.setlocale(locale.LC_TIME, "ko_KR.UTF-8")
        assert parse_claude_usage("Current week (all models): 1% used · resets Oct 6, 2:59pm", now0)["7d"][1] == \
            datetime(2026, 10, 6, 14, 59)  # noqa: DTZ001
    except locale.Error:
        pass
    finally:
        locale.setlocale(locale.LC_TIME, "C")
    u3 = parse_claude_usage("Current week (all models): 1% used · resets Jan 2, 9am", datetime(2026, 12, 30))  # noqa: DTZ001
    assert u3["7d"][1] == datetime(2027, 1, 2, 9, 0), u3  # noqa: DTZ001 -- 연말 넘김
    assert parse_claude_usage("some new wording") == {}
    fake_blocks = {"blocks": [
        {"isGap": True, "totalTokens": 0},
        {"isGap": False, "totalTokens": 100, "startTime": "2026-07-31T00:00:00.000Z", "costUSD": 1.0},
        {"isGap": False, "totalTokens": 400, "startTime": "2026-07-31T05:00:00.000Z", "costUSD": 2.0},
    ]}
    orig = globals()["_ccusage"]
    globals()["_ccusage"] = lambda *_args: fake_blocks
    try:
        bar, tooltip = token_sparkline()
    finally:
        globals()["_ccusage"] = orig
    assert bar == SPARK_CHARS[2] + SPARK_CHARS[8], bar  # 100/400*8=2, 400/400*8=8
    assert "09:00" in tooltip and "14:00" in tooltip, tooltip  # UTC 00:00/05:00 -> KST

    rows = [
        ("13:00", 0, 0.0, ""),
        ("14:00", 4_000_000, 12.93, "robot_ws"),
        ("15:00", 1_000_000, 3.1, "web_app"),
    ]
    labels = row_labels(rows)
    assert "← peak" in labels[1] and "← peak" not in labels[2], labels
    assert "█" * BAR_W in labels[1], labels[1]  # 최대값 = 꽉 찬 막대
    assert "·" * BAR_W in labels[0], labels[0]  # 0 = 빈 막대
    assert "4.0M" in labels[1] and "$12.93" in labels[1], labels[1]
    assert "%" not in labels[1], labels[1]  # 비용 근사 % 열은 제거됨(공식 %는 패널)

    # hot_rows: HOT_HOUR_TOKENS 이상인 시간만, 시간순
    orig_ccusage, orig_scan = globals()["_ccusage"], globals()["_scan_transcripts"]
    globals()["_ccusage"] = lambda *_args: {"blocks": []}
    globals()["_scan_transcripts"] = lambda: [
        (datetime(2026, 8, 18, 6, 10), HOT_HOUR_TOKENS - 1, "quiet_ws"),  # noqa: DTZ001 -- 미만 → 숨김
        (datetime(2026, 8, 18, 11, 5), HOT_HOUR_TOKENS, "busy_ws"),  # noqa: DTZ001 -- 경계값 → 표시
        (datetime(2026, 8, 18, 12, 0), HOT_HOUR_TOKENS // 2, "busy_ws"),  # noqa: DTZ001 -- 같은 시간 합산 → 표시
        (datetime(2026, 8, 18, 12, 30), HOT_HOUR_TOKENS // 2, "busy_ws"),  # noqa: DTZ001
        (datetime(2026, 8, 17, 9, 0), HOT_HOUR_TOKENS * 9, "old_ws"),  # noqa: DTZ001 -- 24h 밖 → 숨김
    ]
    try:
        hr = hot_rows(24, now=datetime(2026, 8, 18, 13, 0))  # noqa: DTZ001
    finally:
        globals()["_ccusage"], globals()["_scan_transcripts"] = orig_ccusage, orig_scan
    assert [r[0] for r in hr] == ["08/18 11:00~12:00", "08/18 12:00~13:00"], hr
    assert hr[1][1] == HOT_HOUR_TOKENS and hr[1][3] == "busy_ws", hr

    # _scan_transcripts: 재귀(subagents 포함) + 연속 중복 제거 + 증분(두 번째 호출에 중복 누적 없음)
    import tempfile
    def _ln(mid, tok, ts="2026-10-03T03:00:00Z"):
        return json.dumps({"timestamp": ts, "cwd": "/w/ws1", "requestId": "r" + mid,
                           "message": {"id": mid, "usage": {"input_tokens": tok}}}) + "\n"
    orig_dir = globals()["PROJECTS_DIR"]
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(f"{tmp}/proj/sess/subagents")
        with open(f"{tmp}/proj/sess.jsonl", "w") as f:
            f.write(_ln("a", 10) + _ln("a", 10) + _ln("a", 10) + _ln("b", 5))  # a는 블록 3줄 = 1회
        with open(f"{tmp}/proj/sess/subagents/agent-1.jsonl", "w") as f:
            f.write(_ln("c", 7))
        globals()["PROJECTS_DIR"] = tmp
        _tail_cache.clear()
        try:
            ev1 = _scan_transcripts()
            with open(f"{tmp}/proj/sess.jsonl", "a") as f:
                f.write(_ln("b", 5) + _ln("d", 1))  # 증분: 직전 b와 연속이면 제외
            ev2 = _scan_transcripts()
        finally:
            globals()["PROJECTS_DIR"] = orig_dir
            _tail_cache.clear()
    assert sorted(t for _, t, _ in ev1) == [5, 7, 10], ev1
    assert sorted(t for _, t, _ in ev2) == [1, 5, 7, 10], ev2
    assert ev1[0][2] == "ws1" and ev1[0][0] == datetime(2026, 10, 3, 12, 0), ev1  # noqa: DTZ001 -- UTC+9

    # label_text/menu_lines: 일부 서비스 실패해도 나머지는 표시
    off = {
        "claude": {"5h": (6, datetime(2099, 1, 1)), "7d": (84, datetime(2099, 1, 1))},  # noqa: DTZ001
        "codex": {"rateLimits": {"primary": {"usedPercent": 42, "resetsAt": 9999999999},
                                 "secondary": {"usedPercent": 7, "resetsAt": 9999999999}},
                  "rateLimitResetCredits": {"availableCount": 2, "credits": [
                      {"status": "available", "expiresAt": 9999999999},
                      {"status": "available", "expiresAt": 4102444800},  # 2100-01-01 -> 먼저 만료
                      {"status": "redeeming", "expiresAt": 1}]}},
        "agy": [("Gemini Models", 0.3339, datetime(2099, 1, 1)), ("Claude and GPT models", 1, datetime(2099, 1, 1))],  # noqa: DTZ001
    }
    snap = quota_snapshot(off, datetime(2026, 10, 3, 12, 0))  # noqa: DTZ001
    assert snap["claude"]["7d"] == {"used": 84, "resets_at": "2099-01-01T00:00:00"}, snap
    assert snap["codex"]["5h"]["used"] == 42 and snap["agy"]["Gemini"]["used"] == 67, snap
    assert snap["agy"]["Claude·GPT"]["used"] == 0 and snap["fetched_at"] == "2026-10-03T12:00:00", snap
    assert "codex" not in quota_snapshot({**off, "codex": RuntimeError()}), "실패 서비스는 빠져야 hook이 침묵"
    lt = label_text(off)
    assert re.fullmatch(r"6%/84% \d+:\d\d \| 42%/7% \d+:\d\d \| 67%/0%", lt), lt  # 셋 다 사용량 %, 시간 H:MM
    assert _hm(datetime(2026, 10, 3, 15, 59), datetime(2026, 10, 3, 12, 18)) == "3:41"  # noqa: DTZ001
    assert _hm(datetime(2026, 10, 3, 12, 20), datetime(2026, 10, 3, 12, 18)) == "0:02"  # noqa: DTZ001
    assert menu_lines({**off, "claude_id": "alice"})[0] == "<b>Claude Code · alice</b>"
    assert "<b>agy · bob · weekly</b>" in menu_lines({**off, "agy_id": "bob"})
    assert _AGY_AUTH_RE.search("] OAuth: authenticated successfully as bob.k@example.com\n").group(1) == "bob.k"
    ml = menu_lines(off)
    assert ml[0] == "<b>Claude Code</b>" and "<b>Codex</b>" in ml and "<b>agy · weekly</b>" in ml, ml
    assert any(l.startswith("<tt>  7d          16% left") for l in ml), ml
    assert any(l.startswith("<tt>  Gemini      33% left") for l in ml), ml
    assert any("Claude·GPT 100% left" in l for l in ml), ml
    i = next(i for i, l in enumerate(ml) if "resets       2 tickets" in l)
    assert "(01/01 " in ml[i], ml[i]  # redeeming 제외, 최조기 만료
    assert "#" not in ml[i + 1], ml  # 티켓별 행 없음
    assert _ticket_rows({"availableCount": 0, "credits": []}) == ["  resets       0 tickets"]
    assert ml.count("") == 2, ml  # 서비스 사이 구분선
    off["codex"] = RuntimeError("boom")
    off["claude"] = RuntimeError("boom")
    assert label_text(off) == "err | err | 67%/0%", label_text(off)
    ml = menu_lines(off)
    assert ml[1] == "<tt>  err (RuntimeError)</tt>" and ml[ml.index("<b>Codex</b>") + 1] == ml[1], ml

if __name__ == "__main__":
    # selftest는 시작 시 돌리지 않는다 — 상수만 바꿔도 단언이 깨져 패널이 안 뜨는 함정
    if "--selftest" in sys.argv:
        _selftest()
        print("selftest PASS")
    else:
        main()
