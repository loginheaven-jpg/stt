# 받아쓰기

한국어 음성을 텍스트로 바꾸는 **로컬 전용 데스크톱 도구**다.

음성이 인터넷으로 나가지 않고 비용이 들지 않는다. 3시간짜리 강의 파일을 끝까지 돌릴 수 있는지가 이 도구의 기준이다.

전사는 `faster-whisper`가, 화자 분리는 `pyannote`가 맡는다. 브라우저 화면은 파이썬 표준 라이브러리 `http.server`로 띄운다 — 설치할 프레임워크가 없다.

---

## 왜 무료 조합인가

같은 음원, 같은 정답지로 잰 결과다.

| 엔진 | 화자 오귀속률 | 화자정답률 | CER | 고유명사 | 비용 |
|---|---|---|---|---|---|
| **whisper_local + pyannote** | **4.35%** | **73.3%** | **9.38%** | **100%** | **0원** |
| CLOVA Speech | 14.29% | 60.0% | 11.46% | 100% | 1,200원/시간 |
| OpenAI diarize | 5.88% | 53.3% | 20.83% | 0% | 약 500원/시간 |
| RTZR sommers | 33.33% | 53.3% | 10.42% | 100% | 1,000원/시간 |

무료 조합이 유료 셋을 모두 앞선다. 유료 엔진을 붙일 이유가 지금은 없다.

> 이 표는 별도 세션에서 잰 값이고 산출물이 이 저장소에 없다. **판단 근거로만 쓰고 회귀 기준으로 쓰지 않는다.** 첫 행의 무료 조합 수치는 이름·용어 교정을 사후 적용한 추정치다. 실제 회귀 기준은 [baseline/](baseline/)에 있다.

---

## 설치

설치 스크립트(`setup.py`)는 아직 없다. 지금은 손으로 넷을 한다.

**하나, 파이썬 3.10 이상을 설치한다.** 윈도우라면 설치 화면의 `Add Python to PATH`를 반드시 체크한다.

**둘, 전사 엔진을 넣는다.**

```powershell
pip install faster-whisper
```

**셋, 화자 분리를 쓰려면 두 가지를 더 넣는다.** 약 2.5GB다. 화자를 나눌 필요가 없으면 건너뛴다.

```powershell
pip install pyannote.audio torch
```

**넷, HuggingFace 토큰을 받아 `env.local`에 넣는다.** 화자 분리에만 필요하다.

1. [huggingface.co](https://huggingface.co)에 가입한다
2. [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)에서 약관에 동의한다
3. [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)에서 `Create new token` → 종류를 **Read**로 만든다
4. `env.local.example`을 `env.local`로 복사하고 `HF_TOKEN=` 뒤에 붙여넣는다

`env.local`은 `.gitignore`에 걸려 있다. 값을 넣은 채로 올라가지 않는다.

---

## 실행

```powershell
python app.py
```

브라우저가 열리지 않으면 http://127.0.0.1:8765 로 들어간다. 끝내려면 그 창에서 `Ctrl+C`를 누른다.

음원은 `audio/` 폴더에 넣는다. 결과는 `out_text/`에 쌓인다.

```powershell
$env:PORT = "8766"; python app.py   # 포트를 바꿔 띄운다
python app.py --selftest            # 이름·용어 교정 규칙 자가 시험
```

기록은 `data/app.log`에 남는다. 창 없이 띄웠을 때도 실패 사유가 여기 남는다.

### 화면에서 고르는 것

모델 · 기기 · 연산 정밀도 · 언어 · 탐색 폭 · 무음 기준 · 이름과 전문용어 · 무음 건너뛰기 · 인식 실패 시 재시도 · 말버릇 살리기 · 이름·용어 교정 · 화자 분리 · 전환 민감도 · 화자 수 · 저장 형식 4종(평문 txt · 시각 포함 txt · 자막 srt · 정본화 초안 md).

**이름·전문용어 칸을 채우면 오인식이 크게 준다.** 비워 두면 회색 예시 글씨는 아무 일도 하지 않는다.

---

## 파일 구성

| 파일 | 하는 일 |
|---|---|
| `app.py` | 본체. 화면·전사·화자 분리·교정·기록이 한 파일에 들어 있다 |
| `run_stt.py` | 유료 엔진 5종 일괄 전사. 비교용이다 |
| `stt_bench.py` | 채점기. CER 외에 간투사 보존율과 화자 오귀속률을 잰다 |
| `go.py` | 점검 → 전사 → 채점을 한 번에 돌린다 |
| `convert_clovanote.py` | 클로바노트 내려받기 파일을 채점기 형식으로 바꾼다 |
| `baseline/` | 회귀 기준선. 산출물과 측정치와 재현 절차 |
| `sample3.txt` | 회귀 시험 정답지 |
| `env.local.example` | 인증 정보 본보기 |

실행하면 `audio/`(음원) · `out_text/`(산출) · `data/`(기록)가 만들어진다. 셋 다 버전 관리에 넣지 않는다.

문서는 셋이다. [stt_app_spec_phase1.md](stt_app_spec_phase1.md)가 원 작업지시서, [stt_app_spec_phase1_addendum.md](stt_app_spec_phase1_addendum.md)가 보완서(**이쪽이 우선한다**), [구축계획.md](구축계획.md)가 실행 계획이다. [CLAUDE.md](CLAUDE.md)는 코드를 만지는 사람이 먼저 읽는다.

---

## 회귀 시험

**`app.py`를 고쳤으면 돌린다.** 절차와 기준값은 [baseline/README.md](baseline/README.md)에 있다.

R1은 26.7초 음원으로 정확도를 재고, R2는 18분 음원으로 구조가 무너지지 않았는지 본다. 음원은 저장소에 없다 — 실존 인물의 녹음이라 넣지 않았다.

교정 규칙만 확인하려면 음원 없이 돌릴 수 있다.

```powershell
python app.py --selftest
```

16개 사례가 돈다. 이 규칙은 두 번 뒤집혔으므로 사례를 코드에 박아 뒀다.

---

## 진행 상황

Phase 1은 로컬 무료 트랙만 만든다. 유료 엔진 통합은 Phase 2다.

| 단계 | 내용 | 상태 |
|---|---|---|
| 0 | 회귀 기준선 동결 · 무창 실행 로깅 | **끝났다** |
| 1 | 대기열 · 이력 · 설정 저장 (백엔드) | 착수 |
| 2 | 화면 재구성 | — |
| 3 | 무창 실행 · 종료 버튼 · 진단 화면 | — |
| 4 | 설치 프로그램 · 설치 문서 | — |

지금은 파일을 하나씩 돌려야 한다. 1단계가 끝나면 여러 개를 걸어두고 자리를 뜰 수 있다.
