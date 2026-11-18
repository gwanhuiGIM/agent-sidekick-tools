# subdisplay

태블릿을 PC의 서브 디스플레이로 쓰는 작은 웹 앱입니다. 한 화면에 네 패널을 띄웁니다.

| 패널 | 내용 |
|---|---|
| herdr | agent 세션마다 `작업 폴더 · 탭 · 세션 이름`과 상태(완료 → 작업 중 순). Claude 세션은 prompt cache 만료가 다가오면(만료 20분 전) 주황 `캐시 N분`, 만료되면 빨강. 입력 직후 첫 응답 전에도 이번 입력이 cache에 적중했는지·만료 뒤라 context 전체를 다시 처리 중인지 보이고, context 15만 토큰 이상이면 `compact 권장`. 세션마다 `자동 compact` 끄기 버튼(목록에서 숨긴 대기 세션은 접힌 `대기 세션 N` 묶음에서). 제목 오른쪽은 로그인한 Claude 계정과 5시간·주간 사용량 |
| 체크리스트 | md 파일의 `- [ ]` 줄. 탭해서 체크, 추가·수정·삭제, `≡`를 끌어 순서 바꾸기 |
| 배터리(맨 윗줄) | 시각 왼쪽에 노트북 배터리 `잔량% · 충전/방전 전력(W) · 완충까지/남은 시간 · 수명%`(수명 = 현재 최대 용량 ÷ 설계 용량). `/sys/class/power_supply/BAT*/uevent`(energy·charge 계열 모두)를 요청마다 읽는다. 방전 중 20% 이하면 빨강, 배터리 없는 PC면 숨김. 남은 시간은 순간 전력 기준이라 부하에 따라 흔들린다 |
| PC 사용량 | CPU·메모리 최근 5분 그래프, 이름별 메모리 상위 프로세스 |
| 공고 | 지원할 공고 폴더마다 JD·회사정보·자기소개서 준비 상태(O·△·X)와 마감 D-day |

알림은 없습니다. 작업하는 동안 곁눈으로 보는 용도입니다.

## 핵심 설계
- **DB가 없습니다.** 원본은 md 파일입니다. 서버는 요청이 올 때마다 파일과 공고 폴더를 읽고 `herdr agent list`·`herdr tab list`를 실행해 결과를 넘깁니다. 그래서 Obsidian 같은 편집기로 같은 파일을 고쳐도 다음 조회(3초) 때 그대로 보입니다. 서버가 들고 있는 상태는 PC 사용량 최근 5분(메모리)뿐입니다.
- **동시 수정은 줄 내용으로 막습니다.** 클라이언트는 줄 번호와 화면에서 본 원래 줄(`expect`)을 함께 보냅니다. 지금 파일의 그 줄이 다르면 서버는 409와 최신 상태를 돌려주고, 화면은 "파일이 다른 곳에서 바뀌었습니다"를 띄운 뒤 다시 그립니다. 쓰기는 임시 파일에 쓰고 `os.replace`로 바꿔치기해서, 쓰다가 죽어도 원본은 남습니다.
- **터미널 출력은 가져오지 않습니다**(아래 자동 compact를 켰을 때 후보 pane의 입력줄 확인만 예외). agent 패널은 herdr가 주는 이름·상태·위치만 씁니다. Claude 세션은 transcript(`~/.claude/projects/*/<session>.jsonl`)의 끝부분만 읽어 마지막 답변 시각·context 크기·cache TTL을 뽑고, 줄 하나가 수 MB여도 범위를 4배씩 넓혀 찾습니다. 크기·mtime이 같으면 다시 읽지 않습니다.
- **입력 직후 판정**: transcript에는 Claude Code가 요청을 시작할 때 쓰는 `api-request` 줄이 있어, 첫 응답을 기다리지 않고 마지막 답변~이번 요청 간격으로 cache 적중·만료를 판정합니다.
- **표준 라이브러리만** 씁니다(`ThreadingHTTPServer`). 화면은 프레임워크 없는 HTML 한 장이고, manifest가 있어 Chrome에서 홈 화면 앱으로 설치할 수 있습니다.

## 실행
```bash
python3 subdisplay.py      # http://127.0.0.1:8765, Ctrl+C로 종료
```
- 기본 데이터는 `example/`입니다. 처음 실행하면 `example/checklist.sample.md`를 `example/checklist.md`로 복사해서 그 사본을 고칩니다(사본은 git에 들어가지 않음).
- 환경변수: `SUBDISPLAY_CHECKLIST`(체크리스트 md 경로), `SUBDISPLAY_PORTFOLIO`(공고 폴더), `SUBDISPLAY_PORT`(기본 8765), `SUBDISPLAY_AUTOCOMPACT`(`1`이면 자동 compact 켬, 기본 꺼짐).
- 태블릿에서 보기: 서버는 127.0.0.1에만 열립니다. `tailscale serve --bg 8765`로 같은 tailnet 안에서만 HTTPS로 엽니다. 인증은 서버 코드에 없고 Tailscale에 맡깁니다. 대신 서버는 요청 Host가 이 PC(127.0.0.1·localhost·기기 이름·`<기기>.<tailnet>.ts.net`·tailnet IP 100.64.0.0/10)일 때만 응답해 DNS rebinding을 막고(아니면 403), POST는 `application/json`만 받습니다. 브라우저 보안 DNS(광고 차단 DoH 등)가 `ts.net` 이름을 못 찾는 기기는 `sudo tailscale serve --bg --tcp 8765 tcp://localhost:8765`로 IP 입구를 열어 `http://<tailnet IP>:8765`로 들어올 수 있습니다(`--http`는 Host가 기기 이름일 때만 넘겨 IP로는 404).

### 요구 사항
- Linux(`/proc`을 읽음), Python 3.10+
- 선택: `herdr`(없으면 agent 패널에 오류 문구, 나머지는 동작), Claude Code(계정·cache 표시), [`usage-indicator`](../usage-indicator/README.md)(켜 두면 Claude 사용량 표시 — `~/.cache/agent-quota.json`을 읽고 5분보다 오래된 값은 숨김)

## 공고 폴더 형식
`SUBDISPLAY_PORTFOLIO` 바로 아래 폴더마다 `00_공고요약.md` 하나가 공고 하나입니다(예시는 `example/portfolio/`). 끝난 공고는 하위 폴더(예: `Done/`)로 옮기면 잡히지 않습니다.
```markdown
---
company: 회사A
position: 로봇 소프트웨어 엔지니어
deadline: 2026-11-20      # 없으면 마감 '미정'
---
## 1. 직무 정보             ← JD
## 2. 기업 소개             ← 회사정보
## 3. 회사 정보             ← 회사정보
```
- **JD**: O = 담당 업무와 자격 요건(또는 우대 사항)이 둘 다 보임, △ = 둘 중 하나가 없음, X = 내용 없음. 공고마다 표기가 달라 키워드로 찾습니다(`JD_DUTY`·`JD_REQ`). 기업 문화 문구만 길게 붙여넣은 JD는 △입니다.
- **회사정보**: 2·3절 합쳐 500자(`CI_ENOUGH`) 이상이면 O, 그보다 적으면 △.
- **자소서**: 그 폴더에 `자기소개서*.md`가 있으면 O.
- `>` 인용, 제목, `기입 필요` 표시가 든 줄은 세지 않습니다. 폴더 이름이 `00_`으로 시작하면 상시모집으로 봅니다. 마감이 10일 이상 남은 공고 대신 상시모집 공고를 접은 화면에 보여 줍니다.
- 기호를 길게 누르면 판정 근거(`담당 업무 O · 자격·우대 X · N자`)가 나옵니다.

## 자동 compact (선택 — `SUBDISPLAY_AUTOCOMPACT=1`일 때만)
agent를 여러 개 돌리다 자리를 비우면, 큰 세션의 prompt cache(1시간)가 만료되고 돌아와서 첫 입력 때 context 전체를 다시 처리하느라 사용량을 크게 씁니다. 그래서 서버가 60초마다 세션을 보고, 만료되기 직전에 그 세션에 herdr로 `/compact`를 넣어 줍니다(`herdr agent prompt <pane> /compact`).

- **대상**: 대기·완료 상태(작업 중·입력 대기 blocked 제외) · 1h TTL · 만료까지 7분 이하(답변 53분 뒤 — cache가 살아 있을 때 해야 compact 요청이 싸다. 만료 뒤엔 보내지 않는다) · context 15만 토큰 이상 · compact 전 · 응답 대기 요청 없음 · Claude 5시간 사용량 90% 미만
- **사고 방지**: 그 pane 화면 끝 입력 칸이 비어 있어야 보낸다(쳐 둔 글 뒤에 붙지 않게, 여러 줄 입력 포함). 입력 칸을 읽는 사이 상황이 바뀔 수 있어 보내기 직전 transcript로 다시 판정하고 만료까지 30초 미만이면 안 보낸다. 한 번에 한 세션, 같은 답변 시점엔 한 번(compact 뒤 이어 쓰다 다시 자리를 비우면 다시 대상)
- **예외**: 열어 두고 계속 볼 세션은 화면의 `자동 compact` 버튼으로 끈다. 목록은 `~/.cache/subdisplay-nocompact.json`(session id)에 남는다
- **조건을 정한 근거**: 실제 세션 transcript 14일치를 보니 보통 세션은 모두 1h TTL로 동작하고 5분 cache 쓰기가 드물게 섞였다 — 그게 마지막 쓰기면 5분 TTL로 잡혀 4분 만에 compact되므로 1h만 대상으로 했다. 실제 세션(69k 토큰)에 `/compact`를 보내 68,796 → 9,083 토큰, 11.9초를 확인했다(150k 이상은 미실측)
- 보낸 세션은 cache 줄 끝에 `· 자동 compact HH:MM`, 서버 터미널에 `autocompact …` 한 줄

## API
| 요청 | 하는 일 |
|---|---|
| `GET /api/state` | 체크리스트·agent·계정·사용량·PC 사용량·배터리·공고를 JSON 하나로 |
| `POST /api/checklist` | `{action: add\|toggle\|edit\|delete\|move, line, expect, text, to, to_expect}`. `Content-Type: application/json`만 받고(같은 PC 브라우저의 다른 페이지가 보내는 simple request 차단) 본문은 64KB까지(음수·초과 길이는 413). 줄이 바뀌었으면 409 |
| `POST /api/nocompact` | `{sid, off}` — off=true면 그 세션을 자동 compact 예외로, false면 해제. sid가 UUID가 아니거나 off가 boolean이 아니면 400 |
| `GET /`, manifest, 아이콘 | 이 4개 경로만 서빙 |

## 시험
```bash
python3 -B test_subdisplay.py   # OK OK2 OK3 OK4 OK5 OK6 OK7 OK8
```
- `OK`: 체크리스트 파싱·다섯 동작·충돌(409)·줄바꿈 주입 차단
- `OK2`: CPU·메모리 계산(`/proc` 텍스트를 직접 넣음), 프로세스 묶기, 공고 판정
- `OK3`: transcript cache 만료 판정(경계값), 입력 직후 적중·만료 판정(`api-request` 줄), `compact 권장` 경계, 큰 줄 너머 탐색, 계정·사용량 파일 읽기
- `OK4`: 실제 HTTP 서버를 띄워 `text/plain` 요청 415·큰 본문 413·정상 추가 200, 기본 공고 경로가 `example/`
- `OK6`: 자동 compact 대상 판정(150k·7분·1h TTL·사용량 경계), 입력 칸 비었는지(여러 줄 입력 포함)
- `OK7`: 예외 목록 저장·해제, 잘못된 sid·off 거부
- `OK8`: herdr·transcript를 가짜로 바꾼 판정 루프 — 작업 중·예외 건너뜀, 한 번에 하나, 같은 답변 시점엔 한 번, 입력 칸을 읽는 사이 만료 직전이 되거나 예외 버튼을 누르면 안 보냄

## 한계
- 자동 compact의 만료 시각은 Anthropic 서버에서 조회한 값이 아니라 `마지막 답변 시각 + TTL` 추정입니다. 입력 칸 판정은 Claude Code 화면(`❯` 줄과 구분선)에 기댑니다.
- herdr JSON, Claude Code transcript 형식에 기댑니다. 공식 API가 아니라 업데이트로 깨질 수 있습니다.
- 외부 편집기와 동시에 저장하는 경우: 서버가 파일을 읽은 뒤 바꿔치기하기 전의 짧은 틈에 다른 편집기(Obsidian 등)가 저장하면, 서버가 쓴 내용이 그 수정을 덮어씁니다. 락은 서버 프로세스 안에서만 걸립니다.
- PC 사용량의 프로세스 CPU는 3초 샘플 두 시점에 모두 살아 있던(또는 사이에 뜬) 프로세스만 셉니다. 3초 안에 떴다 사라진 짧은 프로세스는 빠집니다.
- 요청마다 herdr를 실행하고 파일을 전부 읽습니다. 태블릿 1대 기준으로 `/api/state` 1회 약 10ms였고, 클라이언트가 많아지면 짧은 캐시가 필요합니다.
