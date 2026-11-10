#!/usr/bin/env python3
"""Codex CLI 사용량(rate limit) 확인. codex app-server의 공식 JSON-RPC
account/rateLimits/read를 그대로 호출한다 (Claude와 달리 공식 % 제공).
"""
import json
import os
import select
import time
import subprocess
import sys
from datetime import datetime, timezone


def read_rate_limits(timeout=15, full=False):
    """full=True면 result 전체(rateLimitResetCredits 등 포함), 기본은 rateLimits만(기존 호출자 호환)."""
    proc = subprocess.Popen(
        ["codex", "app-server", "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,  # 안 읽는 PIPE는 64KB 차면 막힌다
    )
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"clientInfo": {"name": "codex-usage", "version": "0.1"}}})
        if _recv(proc, 1, timeout) is None:  # 초기화 응답부터 없으면 두 번째 대기 없이 바로 실패
            raise RuntimeError("codex app-server did not respond")
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": None})
        resp = _recv(proc, 2, timeout)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    if resp is None:
        raise RuntimeError("codex app-server did not respond")
    if "error" in resp:
        raise RuntimeError(resp["error"])
    return resp["result"] if full else resp["result"]["rateLimits"]


def _send(proc, obj):
    proc.stdin.write((json.dumps(obj) + "\n").encode())
    proc.stdin.flush()


def _recv(proc, want_id, timeout):
    """want_id 응답을 timeout 초 안에 받는다. readline은 출력이 없으면 영원히 막히므로 select로 deadline을 건다."""
    deadline = time.time() + timeout
    fd, buf = proc.stdout.fileno(), b""
    while (left := deadline - time.time()) > 0:
        if not select.select([fd], [], [], left)[0]:
            break
        chunk = os.read(fd, 65536)
        if not chunk:  # EOF
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want_id:
                return msg
    return None


def fmt_window(w, label):
    if not w:
        return f"{label}: no data"
    remaining = 100 - w["usedPercent"]
    mins_left = max(0, (w["resetsAt"] - int(datetime.now(timezone.utc).timestamp())) // 60)
    reset_local = datetime.fromtimestamp(w["resetsAt"]).strftime("%m/%d %H:%M")
    return f"{label}: {remaining:3d}% left, resets in {mins_left // 60}h{mins_left % 60:02d}m ({reset_local})"


def format_summary(rl):
    lines = [
        fmt_window(rl.get("primary"), "5h  "),
        fmt_window(rl.get("secondary"), "7d  "),
    ]
    plan = rl.get("planType")
    if plan:
        lines.append(f"plan: {plan}")
    credits = rl.get("credits")
    if credits and not credits.get("unlimited"):
        lines.append(f"credits: {credits.get('balance')}")
    return "\n".join(lines)


def _selftest():
    fake = {
        "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": 9999999999},
        "secondary": {"usedPercent": 25, "windowDurationMins": 10080, "resetsAt": 9999999999},
        "planType": "plus",
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
    }
    out = format_summary(fake)
    assert "5h  :   0% left" in out, out
    assert "7d  :  75% left" in out, out
    assert "plan: plus" in out, out
    assert "credits: 0" in out, out


if __name__ == "__main__":
    _selftest()
    try:
        rl = read_rate_limits()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(format_summary(rl))
