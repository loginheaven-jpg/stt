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

`run_all.ps1` · `run_all.cmd`는 `go.py`로 대체된 잔재다. 쓰지 않는다.

## `app.py` 구조

단일 파일 ~1300줄. 표준 라이브러리 `http.server`만 쓴다. **Flask·FastAPI 등 프레임워크를 도입하지 않는다** (설치 관문).

### 전역 상태

```
JOB    dict — 진행 상태 전부. /status 가 그대로 직렬화해 내려준다
LOCK   threading.Lock — JOB 접근을 감싼다
CANCEL threading.Event — 중단 신호. 전사 루프와 pyannote hook이 함께 본다
```

전사는 배경 스레드 하나에서 돈다. 동시 실행 금지 — `large-v3` + pyannote가 같이 뜨면 수 GB다.

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

`GET /` `/files` `/diacheck` `/version` `/status` `/open?p=` · `POST /start` `/cancel`

`/open`은 `OUTDIR` 밖 경로를 거부한다. 새 엔드포인트를 추가할 때도 경로 검사를 넣는다.

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

**0단계는 끝났다.** `baseline/`에 R1·R2 산출물과 측정치가 동결돼 있고, §6-1 무창 실행 로깅이 `app.py`에 들어갔다(v2026-08-13.8).

1단계(대기열·이력·설정 백엔드)부터가 남았다. `data/`에는 `app.log`만 있고 `settings.json`·`queue.json`·`history.json`은 아직 없다. `start.vbs`·`setup.py`·`설치안내.md`도 없다.

보완서가 1단계에 추가한 요구 — 모델·pyannote 캐시 재사용(§6-2) · 재시작 시 `running`을 자동 재실행하지 말고 `interrupted`로 두기(§6-3) · 잠금 순서를 `STATE_LOCK → LOCK`으로 고정(§6-4) · `/open`은 등록된 출력 폴더 목록만 허용(§6-5).

### `fix_terms()` — 머리 고정과 꼬리 검증 (v.9에서 고쳤다)

첫 기준선 측정에서 이 함수가 어절을 깨뜨리는 것이 드러났다. R2의 교정 3건이 **전부** 파괴였다 — `상황이라서 → 상황미혜서` · `사이클을 → 사이승은` · `이승입니다 → 이승은니다`.

두 규칙으로 막았다. 약화하지 않는다.

- **조각은 어절 머리에서만 찾는다.** 가운데를 훑으면 `상황이라서`의 `황이라`가 `황미혜`로 잡힌다
- **교정 뒤 남는 꼬리가 `TAILS`(조사·어미)에 있어야 반영한다.** `이승님|입니다`는 되고 `이승입|니다`는 안 된다. 목록에 없으면 **본문을 그대로 두고 대장에만 올린다** — 보류다

거리 상한은 3 그대로 둔다. 2로 내리면 거리 3짜리 `이승님 → 이승은` 같은 정상 교정이 죽는다.

대장에 `처리` 열이 있고 화면 안내도 "n건 반영, m건 보류"다. 단정할 수 없으면 고치지 않는다.

**한 번에 전부 만들지 않는다.** 단계마다 감수를 받는다. 1단계(대기열·이력·설정 백엔드)에서는 화면을 건드리지 않는다 — 백엔드와 화면을 같이 바꾸면 회귀 원인을 못 찾는다.

JSON 쓰기는 원자적으로 한다. 임시 파일에 쓴 뒤 `os.replace`. 각 파일에 `version` 필드를 둔다.

## 그 밖

- **git 저장소가 아니다.** 되돌릴 수 없으니 큰 수정 전에는 사본을 둔다.
- `env.local`에 RTZR·CLOVA·OpenAI·HuggingFace 실키가 평문으로 들어 있다. 출력·로그·커밋에 값이 새지 않게 한다 (진단 화면도 길이만 표시한다).
- 입출력은 전부 UTF-8. 한글 파일명이 깨지지 않아야 한다.
- `sample_.wav`(180MB)·`sample2.wav`·`sample3.wav`는 시험 음원이다. 지우지 않는다.
