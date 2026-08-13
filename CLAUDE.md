# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

한국어 음성 → 텍스트 로컬 변환 도구. 이 저장소의 문서·주석·UI 문구는 모두 한국어 평어체("~한다")다. 새로 쓰는 것도 같은 문체로 맞춘다.

## 두 갈래를 혼동하지 않는다

한 폴더에 성격이 다른 두 자산이 같이 있다. 서로 코드를 공유하지 않는다.

| 갈래 | 파일 | 성격 |
|---|---|---|
| **제품** (Phase 1) | `app.py` | 로컬 웹 앱. `faster-whisper` + `pyannote` 하나만 쓴다. 무료·오프라인. **여기를 확장한다** |
| **평가 도구** | `run_stt.py` · `stt_bench.py` · `go.py` · `convert_clovanote.py` | 유료 엔진 5종 비교·채점용. Phase 2 자산 — **수정 금지**. `stt_bench.py`는 회귀 시험에만 쓴다 |

작업 지시서는 [stt_app_spec_phase1.md](stt_app_spec_phase1.md)다. **작업 전에 §6 금지 조항을 읽는다.** 그 목록은 이미 실패해 본 것들이다.

## 명령

```powershell
python app.py                      # 앱 실행 → http://127.0.0.1:8765 (브라우저 자동 열림)
$env:PORT = "8766"; python app.py  # 포트를 바꿔 띄운다
python app.py --selftest           # fix_terms 16개 사례. 교정 로직을 고쳤으면 먼저 돌린다
```

회귀 시험 — `app.py`를 고쳤으면 매번 돌린다. **절차와 기준값은 [baseline/README.md](baseline/README.md)에 있다.** R1(정확도)과 R2(구조) 둘이다.

```powershell
$env:PYTHONIOENCODING = "utf-8"     # 없으면 채점기가 cp949에서 죽는다
python stt_bench.py sample3.txt "out_text/sample3_canon.md" --nouns 이승은 황미혜 이대표님 --detail
```

| | R1 — 정확도 | R2 — 구조 |
|---|---|---|
| 음원 | `audio/sample3.wav` (26.7초) | `audio/이승은코치.m4a` (18:14) |
| 정답지 | `sample3.txt` | 없음 |
| 기준값 | 오귀속 0% · 정답률 43.33% · CER 12.5% · 간투사 44.44% · 고유명사 66.67% · 삽입 0 · 화자 2 | 화자 2 · 발화 143 · 어절 1358 · 교정 0반영 1보류 · 종료 1087초 · 배속 22.30× |

기준선은 **CUDA · RTX 3060**에서 잰 값이다. 기기가 바뀌면 다시 잡는다. 원 지시서 §9-1의 다섯 수치는 폐기됐다 — 재현되지 않았다.

**화자정답률 43.33%를 화자 성능으로 읽지 않는다.** 오귀속률이 0%다. 정답지가 `만나뵙게`처럼 붙여 쓰고 엔진은 띄어 써서 어절 정렬이 13/30만 성공한 결과다. 편차 0이라 회귀 감지에는 문제가 없다.

평가 도구 쪽 (유료 API 키 필요, 건드릴 일 없음):

```powershell
python go.py -p                                              # 5개 엔진 사전 점검
python go.py sample3.wav sample3.txt 2 이승은,황미혜,이대표님   # 점검 → 전사 → 채점
python run_stt.py --check                                    # 인증 정보만 확인
```

`run_all.ps1` · `run_all.cmd`는 **2026-08-14에 삭제했다.** 지시서 §6-1 첫 줄이 `.ps1`·`.cmd` 래퍼를 금지한다 — 실행 정책·LF 줄바꿈·인코딩으로 세 번 막혀 폐기하고 `go.py`로 대체한 물건이다. 저장소에 남으면 다음 작업자가 다시 쓴다. 되살리지 않는다.

## `app.py` 구조

단일 파일 ~1300줄. 표준 라이브러리 `http.server`만 쓴다. **Flask·FastAPI 등 프레임워크를 도입하지 않는다** (설치 관문).

### 전역 상태와 잠금

```
JOB        dict — 지금 도는 항목의 진행 상태. /status 가 그대로 내려준다
LOCK       JOB 접근을 감싼다
CANCEL     현재 항목만 중단. 전사 루프와 pyannote hook이 함께 본다
STOP_ALL   전체 중지. 현재 항목은 끝내고 다음을 집지 않는다
SETTINGS · QUEUE · HISTORY   data/*.json 에 실린 상태
STATE_LOCK 위 셋을 감싼다
```

**잠금 순서는 `STATE_LOCK → LOCK → _LOG_LOCK` 하나로 고정한다.** 역순으로 잡는 경로가 하나라도 생기면 교착한다. `_LOG_LOCK`은 잎 잠금이다 — 쥔 채로 다른 잠금을 잡지 않는다.

### 대기열

`queue_loop()`가 데몬 스레드 하나로 돈다. **실행기가 하나뿐이라 동시 실행 금지가 플래그 검사가 아니라 구조로 보장된다.** `large-v3`와 pyannote가 같이 뜨면 수 GB다.

**상태 전이는 언제나 "파일 먼저, 실행 나중"이다.** `take_next()`가 `running`으로 바꾸고 저장한 뒤에 항목을 돌려준다. 저장 전에 죽으면 `waiting`으로 남아 다시 시도되고, 저장 후 실행 전에 죽으면 `running`으로 남아 다음 기동에서 `interrupted`가 된다. 순서를 뒤집으면 같은 항목이 두 번 도는 길이 열린다.

`interrupted`는 **자동으로 다시 돌리지 않는다.** 부분 산출이 이미 있는데 처음부터 다시 돌면 덮어쓴다. `/queue/resume`에서 `mode=keep`이면 기존 산출을 `이름_부분.*`로 옮기고 다시 담는다.

끝난 항목은 큐에서 빠져 `history.json`으로 간다. `queue.json`에는 `waiting`·`running`·`interrupted`만 남는다 — 대기열 화면이 곧 남은 일이다.

**JSON 쓰기는 원자적이다.** 임시 파일 → `flush` → `fsync` → `os.replace`. 그리고 **진행률로는 저장하지 않는다.** 상태가 바뀔 때만 쓴다.

### 모델 캐시

`(model, device, compute)`를 키로 `WhisperModel`을, `DIA_MODEL`을 키로 pyannote 파이프라인을 잡아 둔다. 조합이 바뀌면 이전 것을 먼저 놓고 새로 만든다. 대기열이 비고 10분이 지나면 놓는다.

효과가 크다 — 화자 분리를 켠 R1 3회에서 **15.69초 → 1.12초**다. Whisper 적재와 pyannote 파이프라인 적재를 함께 아낀다. **R2의 배속이 이 캐시의 회귀 감지기다.**

### 전사 3단계 — [app.py:627](app.py#L627) `transcribe()`

1. **전사** — `WhisperModel.transcribe()`. 구간마다 `f.write()` + `f.flush()`로 즉시 저장한다. 2시간 50분에 죽어도 거기까지 남는다.
2. **화자 분리** — `run_diarization()`. 실패해도 빈 목록을 돌려주고 넘어간다. 텍스트는 이미 있다.
3. **재작성** — `apply_speakers()` → `fix_terms()` → `write_outputs()` → `write_ledger()`.

이 2단계 분리가 핵심 안전장치다. 합치지 않는다.

### 알고리즘 요점

- **화자 배정** [app.py:385](app.py#L385) — pyannote 턴을 그대로 따른다. 어절을 턴에 넣을 뿐 턴을 합치거나 지우지 않는다. `SENSITIVITY` 표의 `high`(기본)는 짧은 조각 흡수 단계를 아예 건너뛴다. 코칭 대화에서 맞장구는 잡음이 아니다.
- **파형 직접 전달** [app.py:232](app.py#L232) — pyannote 4.x에 파일 경로를 주면 `torchcodec is not available`로 막힌다. `load_waveform()`이 16kHz 모노 텐서를 만들어 넘긴다.
- **고유명사 교정** [app.py:511](app.py#L511) — 자모 편집거리. 첫 글자 일치 + 길이별 거리 상한 + 어절 안 조각 탐색(조사·어미 대응). **사용자가 이름·용어 칸에 적은 것만 고친다.** 그 밖의 교정은 창작이다. 바꾼 건 전부 `_corrections.md` 대장에 남긴다.
- **`condition_on_previous_text=False` 고정** — `True`면 침묵 구간에서 앞 문장을 무한 반복한다.
- **`env.local` 파싱** [app.py:86](app.py#L86) — BOM·CRLF·따옴표·`export `/`$env:` 접두·한 줄 다중 항목을 모두 받는다. 관대함이 의도다. 약화하지 않는다.

### 화면

`PAGE` 문자열 하나(raw string, [app.py:772](app.py#L772)~)에 HTML·CSS·JS가 다 들어 있다. `do_GET`이 `__VER__` `__MODELS__` `__DEVICES__` `__COMPUTES__` `__SPEED__` 자리표시자를 치환한다. **외부 자원 금지** — CDN·웹폰트·아이콘 폰트를 쓰지 않는다. 오프라인에서 돌아야 한다. **`localStorage`·`sessionStorage` 금지** — 상태는 서버 JSON에 둔다.

CSS 변수 팔레트는 그대로 쓴다. 새 색을 도입하지 않는다. 시각·수치는 고정폭 글꼴, 진행 막대는 백분율이 아니라 음원 시간 눈금을 쓴다. 화면의 주인공은 배속 숫자다.

### HTTP

| | 경로 |
|---|---|
| GET | `/` `/files` `/diacheck` `/version` `/status` `/log?n=` `/open?p=` `/queue` `/history?n=` `/settings` `/state` |
| POST | `/start` `/cancel` `/queue/add` `/queue/move` `/queue/remove` `/queue/resume` `/queue/stopall` `/history/remove` `/history/again` `/settings` `/outdir/check` |

**`POST /start`를 없애지 않았다.** 큐에 1건 넣고 곧바로 도는 것으로 속만 바꿨다. 기존 화면의 시작 버튼·`/status` 폴링·중단 버튼이 손대지 않아도 그대로 동작한다. 이것이 1단계에서 회귀를 지킨 방식이다. 다만 작업 중에 눌러도 이제 오류가 아니라 대기열에 쌓인다.

`/state`는 2단계 화면용이다. 구역이 넷이라고 요청을 넷 보내지 않는다.

**`/open`은 `startswith`를 쓰지 않는다.** `out_text`가 `out_text2`를 통과시킨다. `under()`가 구분자를 붙여 검사하고, 허용 뿌리는 **설정에 등록된 출력 폴더뿐**이다. 임의 경로를 받지 않는다. 새 엔드포인트를 만들 때도 같은 검사를 넣는다.

### 절대 건드리지 않는 것

| 항목 | 이유 |
|---|---|
| `Cache-Control: no-store` 헤더 | 브라우저가 옛 화면을 붙들어 수정이 안 보였다 |
| `port_busy()` 검사 | 윈도우에서 옛 프로세스가 살아 응답했다 |
| `allow_reuse_address = (os.name != "nt")` | 위와 같은 사고 |
| `APP_VERSION` 3곳 표시 (콘솔·탭 제목·화면 배지) | 갱신 여부 확인용 |
| `dia_check()` 사전 점검 | 20분 돌린 뒤 실패를 아는 일을 막았다 |

**`app.py`를 고쳤으면 `APP_VERSION`을 올린다.** 안 올리면 화면이 갱신됐는지 알 수 없다.

## 셸 스크립트 금지

`.ps1`·`.cmd` 래퍼를 새로 만들지 않는다. 실행 정책·LF 줄바꿈·인코딩으로 세 번 막혔다. **파이썬과 `.vbs`만 쓴다.** 무창 실행이 필요하면 `start.vbs`가 `pythonw.exe`를 부른다.

## 평가 도구 형식

`run_stt.py` 출력과 `stt_bench.py` 입력이 공유하는 형식이다. `app.py`의 `_timed.txt`(형식 A)와 `_canon.md`(형식 B)가 그대로 채점기에 들어간다.

```
형식 A   [01:23] 코치<TAB>어 안녕하세요 반갑습니다
형식 B   **[01:23] 코치**
         어 안녕하세요 반갑습니다
```

`stt_bench.py`는 CER 외에 **간투사 보존율**과 **화자 오귀속률**을 잰다. 정본화 규칙에서 이 둘이 CER보다 중요하다. 엔진이 "어, 음"을 지우면 감점이다.

## Phase 1 진행 상태

지시서는 둘이다. **[stt_app_spec_phase1_addendum.md](stt_app_spec_phase1_addendum.md)가 원 지시서보다 우선한다.** 충돌하면 보완서를 따른다. 실행 계획은 [구축계획.md](구축계획.md)다.

보완서가 바꾼 것 — §1-3 비교표는 근거 자료일 뿐 인수 조건이 아니다 · §9-1은 폐기하고 R1·R2로 대체한다 · `stt_bench.py`는 채점 로직만 동결이고 인코딩 수정은 허용한다 · 0단계(기준선 동결)를 맨 앞에 넣는다.

**0단계와 1단계가 끝났다.** `baseline/`에 R1·R2가 동결돼 있고, 무창 실행 로깅(§6-1)·대기열·이력·설정 저장·모델 캐시(§6-2)·중단 작업 처리(§6-3)·잠금 순서(§6-4)·`/open` 허용 목록(§6-5)이 모두 들어갔다.

**2단계(화면 재구성)부터가 남았다.** 화면은 아직 0단계 그대로다 — 파일 하나를 골라 시작하는 단일 작업 화면이고, 대기열·이력·프리셋·출력 폴더 고르개가 화면에 없다. 백엔드는 `/state` 하나로 다 내려줄 준비가 돼 있다. `start.vbs`·`setup.py`·`설치안내.md`도 없다.

### `fix_terms()` — 꼬리 우선 분할

**이 함수의 규칙은 두 번 뒤집혔다. 고치기 전에 `python app.py --selftest`를 돌린다.**

어절 가운데를 훑던 v.8이 `상황이라서 → 상황미혜서` · `사이클을 → 사이승은` · `이승입니다 → 이승은니다` 셋을 깨뜨렸다. 머리를 먼저 자른 v.10은 파괴는 막았으나 `이승|입니다`를 못 찾아 이름을 흘렸다.

**v.11은 꼬리를 먼저 정하고 머리를 맞춘다.** `입니다`가 `TAILS`에 있으니 그 앞이 이름 자리다.

```
이승입니다  →  이승 | 입니다      머리를 "이승은"과 대조 (거리 3) → 이승은입니다
상황이라서  →  맞는 꼬리가 없다    →  건드리지 않는다
```

지켜야 할 것 셋이다.

- **`if t in core` 가드.** 없으면 `이승은` → `이승은은`이 된다
- **거리 상한 3.** 2로 내리면 거리 3짜리 `이승님 → 이승은`이 죽는다
- **꼬리가 성립하지 않으면 본문을 두고 대장에만 올린다** — 보류. 단정할 수 없으면 고치지 않는다

남는 오탐은 `이승만은 → 이승은은`이다. 지정 용어와 자모 거리 2인 다른 이름은 걸린다. 모든 이름 교정에 내재한 위험이고 대장에 남으므로 감수한다.

**한 번에 전부 만들지 않는다.** 단계마다 감수를 받는다. 1단계(대기열·이력·설정 백엔드)에서는 화면을 건드리지 않는다 — 백엔드와 화면을 같이 바꾸면 회귀 원인을 못 찾는다.

JSON 쓰기는 원자적으로 한다. 임시 파일에 쓴 뒤 `os.replace`. 각 파일에 `version` 필드를 둔다.

## 그 밖

- **git 저장소가 아니다.** 되돌릴 수 없으니 큰 수정 전에는 사본을 둔다.
- `env.local`에 RTZR·CLOVA·OpenAI·HuggingFace 실키가 평문으로 들어 있다. 출력·로그·커밋에 값이 새지 않게 한다 (진단 화면도 길이만 표시한다).
- 입출력은 전부 UTF-8. 한글 파일명이 깨지지 않아야 한다.
- `sample_.wav`(180MB)·`sample2.wav`·`sample3.wav`는 시험 음원이다. 지우지 않는다.
