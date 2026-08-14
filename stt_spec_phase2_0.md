# 받아쓰기 — 2-0단계 작업지시서

> 대상 : Claude Code
> 기준 코드 : `app.py` v2026-08-14.1 (커밋 `8c56f89`)
> 선행 문서 : `stt_app_spec_phase1.md` · `stt_app_spec_phase1_addendum.md`
> 작성일 : 2026-08-14

---

## 0. 이 단계의 위치

Phase 1이 끝나고 새 PC에 설치한 결과, 두 가지가 드러났다.

**하나, GPU가 있는 PC에서 전사가 죽는다.** `Library cublas64_12.dll is not found or cannot be loaded`.

**원인이 확정됐다. DLL은 디스크에 있었고 PATH에 없었다.** 실측 결과다.

```
C:\Users\USER\...\site-packages\nvidia\cublas\bin\cublas64_12.dll   있음
C:\Users\USER\...\site-packages\nvidia\cudnn\bin\cudnn64_9.dll      있음
```

버전 세대도 맞았다. `ctranslate2` 4.x 가 요구하는 CUDA 12 · cuDNN 9 그대로다. **PATH에 폴더를 넣고 같은 창에서 실행하니 GPU로 돌았다.**

`pip install` 은 DLL을 `site-packages/nvidia/*/bin` 에 넣지만 그 경로를 PATH에 등록하지 않는다. 파이썬이 `import` 하는 패키지가 아니라 네이티브 라이브러리가 찾아야 하는 파일이라서다.

**설치와 실행 양쪽에서 이것을 처리해야 한다.**

**둘, 실패한 뒤 화면 상태가 어긋난다.** 오류 띠가 떠 있는데 "받아쓰는 중"이 남아 있고, 실패한 작업에 "현재 중단" 버튼이 붙어 있다. 새로 고침해도 그대로다.

원격 접속(2-1 이후)보다 이것이 먼저다. **다른 PC에서 돌지 않는 도구에 원격을 얹을 수 없다.**

우선순위는 **§2 → §3 → §4 → §5** 순이다. §2가 가장 급하다.

---

## 1. 원 지시서에서 유지되는 것

§6-1 금지 조항 아홉 개, §6-2 유지 항목, R1·R2 회귀 게이트, 세 시험(`--selftest`·`queue_test`·`screen_test`)은 그대로다.

이 단계에서 **`fix_terms` 로직과 화자 배정 로직은 건드리지 않는다.**

---

## 2. 실패 상태 정합 — 가장 급하다

### 2-1. 증상

새 PC의 실제 화면이다.

```
지금 하는 일   sample3.wav
받아쓰는 중                                  ← 단계는 진행 중
[진행 막대 0%]   0:00   0.0%
전사 중 멈췄다. Library cublas64_12.dll ...   ← 오류는 떠 있다
[현재 중단]  [전체 중지]                      ← 실패한 작업에 중단 버튼
```

새로 고침해도 같다. **화면 문제가 아니라 서버 상태가 어긋난 것이다.**

### 2-2. 종료 상태를 한 곳에서 정리한다

`done` · `error` · `cancelled` · `interrupted` 중 하나가 되면 반드시 함께 처리한다.

| 필드 | 값 |
|---|---|
| `phase` | `""` |
| `eta` | `0` |
| `speed` | 마지막 값 유지 |
| `processed` | 실패 지점 유지. 0으로 되돌리지 않는다 |
| `message` | 실패 사유. **어느 파일인지 포함** |
| `file` · `stem` | 유지 |

전이 지점이 여러 곳에 흩어져 있으면 **함수 하나로 모은다.**

```python
def finish_job(state: str, message: str = "") -> None:
    """작업을 끝맺는다. 종료 상태에서 phase 가 남으면 화면이 진행 중으로 보인다."""
    with LOCK:
        JOB.update(state=state, phase="", eta=0.0, message=message)
```

`transcribe()`의 모든 종료 경로가 이 함수를 지나야 한다. 예외 경로도 포함한다.

### 2-3. 버튼을 상태에 묶는다

| 상태 | 버튼 |
|---|---|
| `running` · `diarizing` · `merging` | 현재 중단 · 전체 중지 |
| **`error` · `failed`** | **다시 시도 · 대기열에서 제거** |
| `done` | 결과 열기 |
| `cancelled` | 다시 시작 · 제거 |
| `interrupted` | 다시 시작 · 제거 |
| `idle` | 없음 |

**실패한 작업에 "현재 중단"은 뜻이 없다.**

"다시 시도"는 같은 설정으로 대기열 맨 앞에 다시 넣는다. 이력의 "같은 설정으로 다시"와 같은 경로를 쓴다.

### 2-4. 오류 문구에 대상을 밝힌다

지금은 무엇이 실패했는지 알 수 없다.

```
전사 중 멈췄다. Library cublas64_12.dll is not found or cannot be loaded
```

이렇게 바꾼다.

```
sample3.wav — 전사 중 멈췄다.
Library cublas64_12.dll is not found or cannot be loaded
→ GPU를 쓸 수 없다. 설정에서 기기를 cpu 로 바꾸거나 진단 화면을 확인해달라.
```

**무엇이 · 왜 · 무엇을 하면 되는지** 세 줄이다.

### 2-5. 진행 막대가 100%에 닿지 않는다

실제 화면이다. **작업이 끝나고 파일까지 만들어졌는데 98.1%에 멈춰 있다.**

```
화자 2명으로 나눴다
▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░   0:26   98.1%   0:27
끝났다.
저장 위치  C:\dev\STT\out_text\sample3_canon.md
```

원인은 진행률을 **마지막 발화의 시작 시각**으로 계산하기 때문이다. 음원 끝에 무음이 있거나 마지막 발화가 26초에서 시작해 27초에 끝나면 영영 100%가 되지 않는다.

| 요구 | 내용 |
|---|---|
| 완료 시 | `processed = duration` 으로 맞춘다. **막대는 반드시 100%** |
| 진행 중 | 지금처럼 실제 위치를 쓴다 |
| `duration` 미상 | 막대 대신 "진행 중" 표시. 백분율을 만들지 않는다 |
| 실패·중단 시 | 그 지점에 고정. 100%로 채우지 않는다 |

**끝난 일이 98%로 남으면 도구를 못 믿는다.**

### 2-6. 종료 상태에서 남은 시간·경과가 어긋난다

같은 화면에 `남은 시간 —` 과 `경과 0:23` 이 함께 떠 있다. 종료 뒤에는 이렇게 바꾼다.

| 항목 | 종료 후 표시 |
|---|---|
| 배속 | 최종 배속 유지 |
| 남은 시간 | **숨긴다** |
| 경과 | **총 소요**로 이름을 바꿔 유지 |
| 글자 | 유지 |

### 2-7. 대기열이 멈추지 않는다

1단계 인수에서 `['done','error','done']`을 확인했으니 구조는 맞다. **화면이 그 사실을 보여주지 못하는 것이 문제다.**

| 요구 | 내용 |
|---|---|
| 실패 항목 | 즉시 기록으로 내린다 |
| 다음 항목 | "지금 하는 일"에 올라온다 |
| 실패 알림 | 사라지지 않고 기록에 남는다. "지금 하는 일" 영역을 점유하지 않는다 |

**실패가 다음 작업의 화면을 가리면 안 된다.**

### 2-8. 전체 중지가 듣지 않는다

실제로 눌러도 반응이 없다. 규격을 명확히 한다.

| 순서 | 동작 |
|---|---|
| 1 | 확인 창을 띄운다 — "진행 중인 작업을 멈추고 대기열을 비웁니다. 계속할까요?" |
| 2 | 취소하면 아무 일도 없다 |
| 3 | 확인하면 현재 작업을 중단한다 |
| 4 | **대기열의 남은 항목을 전부 제거한다** |
| 5 | 실행기를 놀림 상태로 되돌린다 |
| 6 | 화면이 초기 상태로 돌아간다. "지금 하는 일" 구역이 사라진다 |
| 7 | 중단된 작업은 기록에 `cancelled` 로 남는다. **부분 산출물은 지운다** |

**"현재 중단"과 구분한다.**

| 버튼 | 대상 | 대기열 |
|---|---|---|
| 현재 중단 | 지금 작업만 | **유지. 다음 항목이 이어서 실행된다** |
| **전체 중지** | 지금 작업 + 대기열 전부 | **비운다** |

7번의 부분 산출물 처리를 정한다. 전체 중지는 "없던 일로 한다"는 뜻이므로 **지우는 것이 맞다.** 다만 지운 사실을 기록에 남긴다. 현재 중단은 §6-3에 따라 보존한다.

확인 창은 브라우저 `confirm()` 으로 충분하다. **외부 자원 금지 조항에 걸리지 않는다.**

### 2-9. 종료 상태에서 버튼이 남는다

같은 화면에서 작업이 끝났는데 `현재 중단` · `전체 중지` 가 그대로 있다. §2-3의 버튼 표를 따른다. **`done` 상태이고 대기열이 비었으면 두 버튼 모두 사라진다.**

---

## 3. 기기 자동 판정 — 실제로 돌려 보고 정한다

### 3-1. 현재 결함

`auto`가 "GPU가 있는가"만 보고 실제 구동을 확인하지 않는다. 그래서 DLL이 없는 PC에서 죽었다.

**만들어 봐야 아는 것이지 물어봐서 아는 것이 아니다.**

### 3-2. DLL 경로 등록

윈도우에서 `nvidia-*` 휠의 DLL은 `site-packages/nvidia/*/bin` 에 들어가고 PATH에 없다. `ctranslate2`가 `cublas64_12.dll`을 못 찾는 가장 흔한 원인이다.

**`nvidia.__file__` 은 `None` 이다.** 네임스페이스 패키지라 그렇다. 실측에서 아래 예외를 확인했다.

```
TypeError: expected str, bytes or os.PathLike object, not NoneType
```

`nvidia.__path__` 를 순회해야 한다.

```python
CUDA_DLL_DIRS: List[str] = []


def register_cuda_dlls() -> List[str]:
    """
    nvidia 휠의 DLL 폴더를 프로세스에 등록한다. 한 번만 돈다.

    pip 는 DLL 을 site-packages/nvidia/*/bin 에 넣지만 PATH 에 올리지 않는다.
    ctranslate2 가 cublas64_12.dll 을 못 찾는 가장 흔한 원인이 이것이다.
    nvidia 는 네임스페이스 패키지라 __file__ 이 None 이므로 __path__ 를 쓴다.
    """
    global CUDA_DLL_DIRS
    if getattr(register_cuda_dlls, "_done", False):
        return CUDA_DLL_DIRS
    register_cuda_dlls._done = True
    try:
        import glob
        import nvidia
        for root in list(nvidia.__path__):
            for d in sorted(glob.glob(os.path.join(root, "*", "bin"))):
                if not os.path.isdir(d):
                    continue
                CUDA_DLL_DIRS.append(d)
                if os.name == "nt":
                    os.add_dll_directory(d)
                # 자식 프로세스와 일부 로더를 위해 PATH 에도 넣는다
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    except ImportError:
        log("CUDA 휠 없음 — CPU 로 동작한다")
    except Exception as e:
        log(f"CUDA DLL 경로 등록 실패 — {e}")
    log(f"CUDA DLL 경로 {len(CUDA_DLL_DIRS)}곳 등록")
    return CUDA_DLL_DIRS
```

**앱 기동 시 한 번, 모델을 만들기 전에 다시 호출한다.** `os.add_dll_directory` 는 현재 프로세스에만 듣고, `PATH` 추가는 자식 프로세스까지 미친다. 둘 다 한다.

### 3-3. 판정과 자동 전환

```python
CUDA_HINTS = ("cublas", "cudnn", "CUDA", "cuda", "libcu", "cudart", "nvrtc")


def make_whisper(name: str, device: str, compute: str):
    """
    (모델, 실제기기, 안내문) 을 돌려준다.
    auto 는 CUDA 를 실제로 만들어 보고 실패하면 CPU 로 내려간다.
    cuda 를 명시로 고른 경우에는 내려가지 않는다. 조용히 느려지면 원인을 못 찾는다.
    """
    register_cuda_dlls()
    want = "cuda" if device == "auto" else device

    try:
        m = WhisperModel(name, device=want, compute_type=compute)
        return m, want, ""
    except Exception as e:
        msg = str(e)
        if device != "auto" or not any(k in msg for k in CUDA_HINTS):
            raise
        log(f"CUDA 사용 불가 — CPU 로 전환한다. {msg[:150]}")
        m = WhisperModel(name, device="cpu", compute_type="int8")
        return m, "cpu", f"GPU를 쓸 수 없어 CPU로 처리했다. {msg[:100]}"
```

| 요청 | GPU 정상 | GPU 불가 |
|---|---|---|
| `auto` | GPU | **CPU로 전환 + 안내** |
| `cuda` | GPU | **오류. 전환하지 않는다** |
| `cpu` | CPU | CPU |

`compute_type`도 함께 내린다. **CPU에서 `float16`은 더 느리거나 실패한다.**

### 3-4. pyannote도 같다

`torch.cuda.is_available()`이 참이어도 실제 연산에서 깨질 수 있다. `.to("cuda")`를 `try` 안에 두고 실패하면 CPU로 둔다. 그 사실을 로그와 화면에 남긴다.

### 3-5. 모델 캐시 키

**캐시 키에는 요청 기기가 아니라 실제 기기를 넣는다.** `auto`로 요청해 CPU로 내려갔는데 키가 `auto`면 다음 항목이 또 CUDA를 시도한다.

```
키 = (모델명, 실제기기, 실제정밀도)
```

### 3-6. 상태에 반영

`JOB`에 두 필드를 더한다.

| 필드 | 내용 |
|---|---|
| `device` | 실제 사용 기기. `"cuda"` · `"cpu"` |
| `device_note` | 전환이 일어났으면 사유. 아니면 `""` |

---

## 4. 기기 인디케이터

### 4-0. 요구의 핵심 — 두 가지를 따로 보여준다

실제 화면에서 배속이 `1.1×` 로 나왔다. **GPU가 있는 PC인데 CPU로 돌았다는 뜻이다.** 그런데 화면에는 그 사실이 어디에도 없다.

**"이 PC에 GPU가 있는가"와 "지금 무엇으로 도는가"는 다른 질문이다.** 둘을 각각 답해야 한다.

| 질문 | 어디에 |
|---|---|
| 이 PC에 GPU가 있는가 | **머리말 · 항상 보인다** |
| 지금 무엇으로 도는가 | 진행 화면 · 배속 옆 |
| 왜 GPU를 못 쓰는가 | 진행 화면 한 줄 + 진단 화면 |

### 4-1. 머리말 — 항상 보이는 기기 상태

버전 배지 옆에 둔다. 작업을 시작하기 전에도 보여야 한다.

```
받아쓰기  [이 PC에서만 처리 · 비용 없음]      [GPU 사용 가능]   v____   [종료]
받아쓰기  [이 PC에서만 처리 · 비용 없음]      [CPU만 사용]      v____   [종료]
받아쓰기  [이 PC에서만 처리 · 비용 없음]      [GPU 있으나 사용 불가]  v____   [종료]
```

세 번째가 이번 사고의 상태다. **누르면 진단 화면으로 간다.**

판정은 앱 기동 시 한 번 한다. `nvidia-smi` 로 물리적 유무를, `WhisperModel(device="cuda")` 로 실제 사용 가능 여부를 본다. 후자는 `small` 모델로 가볍게 한다.

| 물리 GPU | 실제 사용 | 배지 |
|---|---|---|
| 있음 | 가능 | `GPU 사용 가능` |
| 있음 | 불가 | **`GPU 있으나 사용 불가`** |
| 없음 | — | `CPU만 사용` |

### 4-2. 진행 화면

배속 숫자 옆이 자리다. 이 앱의 주인공은 시간이고, **기기가 그 숫자를 설명한다.**

```
   22.9× 실시간   [GPU]                     남은 24:31
   ▓▓▓▓▓▓▓░░░░│░░░░░░░░░░░░
```

```
    2.1× 실시간   [CPU]                     남은 1:24:03
```

자동 전환이 일어났으면 사유를 한 줄 더한다.

```
    2.1× 실시간   [CPU · GPU 사용 불가]
    cublas64_12.dll 없음. 진단 화면에서 해결 방법을 볼 수 있다
```

**배지 색은 기존 CSS 변수 안에서 쓴다.** GPU는 `--signal`, CPU는 `--muted` 계열이면 충분하다. 새 색을 만들지 않는다.

### 4-3. 진단 화면

```
  기기        CPU  (요청 auto · CUDA 사용 불가)
              cublas64_12.dll 없음
              해결 — pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
  GPU         NVIDIA GeForce RTX 3060 (드라이버 감지됨)
  CUDA 휠     nvidia-cublas-cu12 없음 · nvidia-cudnn-cu12 없음
```

**GPU가 물리적으로 있는지와 쓸 수 있는지를 구분해 보여준다.** 둘이 다르다는 것이 이번 사고의 핵심이다.

### 4-4. 이력

이력 항목에도 기기를 남긴다. 나중에 배속을 비교할 때 근거가 된다.

```
08-14 09:22  sample3.wav  0:27  22.9×  GPU  화자 2명
```

---

## 5. 설치기 — GPU를 기본으로 설치하고 PATH까지 잡는다

### 5-1. 방침 변경

앞서 "조건부 설치"로 정했으나 **NVIDIA GPU가 감지되면 묻지 않고 설치한다.** 실측에서 확인됐듯 GPU가 있으면 20배 빠르고, 사용자가 그 이득을 판단할 근거가 설치 시점에 없다.

| 감지 | 동작 |
|---|---|
| NVIDIA GPU 있음 | **묻지 않고 설치.** 용량과 소요를 알리고 진행 |
| 없음 | 건너뛴다. "CPU로 동작한다"고 알린다 |
| 판정 실패 | 물어본다. 기본값은 설치 |

건너뛰기는 `--no-gpu` 인자로 남긴다. 화면에서 매번 묻지 않는다.

### 5-2. 감지

```
nvidia-smi  실행 성공 → NVIDIA GPU 사용 가능
실패        → GPU 없음 또는 드라이버 없음
```

`wmic path win32_VideoController get name` 은 드라이버가 없어도 이름이 나오므로 보조로만 쓴다. **`nvidia-smi` 가 판정 기준이다.**

`nvidia-smi` 출력에서 `CUDA Version` 을 함께 읽는다. **12.0 미만이면 드라이버 갱신이 필요하다고 알린다.**

### 5-3. 설치 대상

```
nvidia-cublas-cu12
nvidia-cudnn-cu12>=9
```

버전 하한이 중요하다. `ctranslate2` 4.x 는 **cuDNN 9** 를 요구한다. `>=9` 를 빼면 8이 깔려 같은 증상이 난다.

`requirements-gpu.lock` 에 실측 버전을 고정한다.

### 5-4. PATH 등록 — 이번 사고의 핵심

**설치만으로는 부족하다.** DLL 폴더를 찾을 수 있게 만들어야 한다. 세 겹으로 건다.

| 겹 | 방법 | 적용 범위 |
|---|---|---|
| 1 | 앱이 기동 시 `register_cuda_dlls()` | **주 방어.** 이것만으로 대개 해결된다 |
| 2 | `setup.py` 가 사용자 환경변수 `PATH` 에 영구 등록 | 다른 도구·명령창에서도 듣는다 |
| 3 | 설치 직후 실측 확인 | 1·2가 실제로 통했는지 |

2번은 윈도우에서 `setx` 대신 레지스트리로 한다. `setx` 는 1024자 제한이 있어 기존 PATH를 잘라먹는다.

```python
def add_user_path(dirs: List[str]) -> bool:
    """
    사용자 PATH 에 영구 등록한다.
    setx 는 1024자 제한이 있어 기존 PATH 를 자른다. 레지스트리를 직접 쓴다.
    """
    if os.name != "nt" or not dirs:
        return False
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as k:
        try:
            cur, typ = winreg.QueryValueEx(k, "PATH")
        except FileNotFoundError:
            cur, typ = "", winreg.REG_EXPAND_SZ
        have = [p for p in cur.split(os.pathsep) if p]
        add = [d for d in dirs if d not in have]
        if not add:
            return False
        winreg.SetValueEx(k, "PATH", 0, typ or winreg.REG_EXPAND_SZ,
                          os.pathsep.join(have + add))
    # 열려 있는 창에는 즉시 반영되지 않는다. 새 창부터 듣는다
    return True
```

**등록 후 열려 있는 명령창에는 반영되지 않는다.** 설치 요약에 그 사실을 적는다.

### 5-5. 실측 점검

패키지 존재만 보지 않는다. **끊긴 고리를 짚어야 한다.**

```
GPU 진단
  통과   NVIDIA GeForce RTX 3060 · 드라이버 CUDA 12.4
  통과   ctranslate2 4.5.0 → CUDA 12 · cuDNN 9 계열 필요
  통과   nvidia-cublas-cu12 12.4.5.8
  통과   nvidia-cudnn-cu12 9.1.0.70
  통과   cublas64_12.dll   ...\nvidia\cublas\bin
  통과   cudnn64_9.dll     ...\nvidia\cudnn\bin
  통과   DLL 경로 3곳 등록 · 사용자 PATH 등록
  통과   시험 전사 (cuda) 0.9초
```

| # | 검사 | 방법 |
|---|---|---|
| 1 | 드라이버와 CUDA 버전 | `nvidia-smi` 출력 파싱 |
| 2 | `ctranslate2` 세대 | 버전에서 요구 CUDA·cuDNN 판정 |
| 3 | 휠 설치 여부와 버전 | `importlib.metadata.version` |
| 4 | **DLL 실물 존재** | `cublas64_12.dll` · `cudnn64_9.dll` 파일 확인 |
| 5 | 경로 등록 | `register_cuda_dlls()` 결과 |
| 6 | **실제 구동** | `small` 모델로 `device="cuda"` 생성 |

**6번만이 최종 판정이다.** 1~5는 실패 시 원인을 짚기 위한 것이다.

같은 검사를 진단 화면에서도 돌린다. 함수 하나를 공유한다.

### 5-6. 파이썬 버전 경고

실측 PC가 **파이썬 3.14** 였다. 지금은 하한(3.10)만 본다.

| 버전 | 판정 |
|---|---|
| 3.10 미만 | **실패.** 설치 중단 |
| 3.10 ~ 3.12 | 통과 |
| 3.13 이상 | **주의.** "일부 패키지가 아직 지원하지 않을 수 있다" |

주의는 설치를 막지 않는다. 문제가 났을 때 원인을 짚을 근거가 된다.

### 5-7. 의존성 고정

Phase 1 잔여 항목이다. 이 단계에서 함께 처리한다.

| 파일 | 내용 |
|---|---|
| `requirements.lock` | 필수 의존성의 현재 실측 버전 |
| `requirements-gpu.lock` | `nvidia-cublas-cu12` · `nvidia-cudnn-cu12>=9` |
| `LICENSE` | MIT |
| `NOTICE` | 의존성 라이선스 고지. **`pyannote/speaker-diarization-community-1` 은 CC-BY-4.0** |

`setup.py` 가 `requirements.lock` 으로 설치한다. **기준선을 동결해 놓고 환경을 동결하지 않으면 재현이 보장되지 않는다.**

`README.md` 첫머리에 한 줄을 넣는다.

> 이 도구는 개인 PC 전용이다. `127.0.0.1` 에만 바인딩하며 인증이 없다. 외부에 공개하도록 만들어지지 않았다.

공개 저장소이므로 누군가 `0.0.0.0` 으로 바꿔 열 수 있다. 경고가 그 사고를 막는다.

---

## 6. 시험

### 6-1. 추가할 시험

`WhisperModel`을 예외를 던지도록 흉내 내면 **음원 없이 시험된다.**

| 시험 | 확인 |
|---|---|
| 모델 생성 실패 | `state="error"` · `phase=""` · `eta=0` |
| 실패 후 `/state` | 종료 상태를 그대로 준다. 새로 고침해도 같다 |
| 실패 후 버튼 | 다시 시도 · 제거로 바뀐다 |
| 실패 후 다음 항목 | "지금 하는 일"이 다음 파일로 바뀐다 |
| `auto` + CUDA 실패 | CPU로 완주. `device="cpu"` · `device_note` 있음 |
| `cuda` 명시 + 실패 | **전환하지 않고 오류** |
| 캐시 키 | `auto`→CPU 전환 뒤 다음 항목이 CUDA를 재시도하지 않는다 |
| 손상 파일 | 0바이트·비미디어 파일이 안전하게 실패하고 대기열이 계속 돈다 |
| **진행 막대 완료** | **`done` 이면 `processed == duration`. 백분율 100.0%** |
| 종료 후 표시 | 남은 시간이 숨고 경과가 총 소요로 바뀐다 |
| **전체 중지** | **확인 후 현재 작업 중단 + 대기열 비움 + 화면 초기화** |
| 전체 중지 취소 | 아무 일도 일어나지 않는다 |
| 현재 중단 | 대기열은 유지되고 다음 항목이 실행된다 |
| 종료 후 버튼 | `done` + 대기열 빔 → 중단 버튼 둘 다 사라진다 |
| 머리말 기기 배지 | 세 상태가 각각 맞게 표시된다 |
| **`nvidia.__path__` 순회** | **`__file__` 이 None 이어도 예외 없이 경로를 찾는다** |
| DLL 등록 | 등록 전후로 `os.environ["PATH"]` 에 폴더가 늘어난다 |
| 휠 없음 | `import nvidia` 실패해도 앱이 죽지 않고 CPU로 간다 |
| cuDNN 8 상황 | 버전 부족을 진단이 짚어낸다 |

마지막 항목은 참고의견에서 받아들인 지적이다.

### 6-2. 회귀

| 항목 | 기준 |
|---|---|
| R1 · R2 정확도 지표 | **변동 없음** |
| R1 · R2 배속 | **기기가 다르면 비교하지 않는다** |
| 세 시험 | 전부 통과 |

`baseline.json`에 측정 기기가 이미 적혀 있다. **시험이 현재 기기와 대조해 다르면 배속 항목을 건너뛰도록 고친다.** 기준선을 다시 잡지 않는다.

---

## 6-3. 설치안내.md 갱신

문서가 GPU를 다루지 않는다. 아래를 고친다.

| 절 | 고칠 것 |
|---|---|
| 3절 설치 | 묻는 항목 표에 GPU 줄 추가. "그래픽카드가 있으면 관련 파일 700MB를 자동으로 받는다" |
| 3절 요약 | `GPU` 항목이 요약에 나온다는 사실 |
| 5절 지금 하는 일 | **배속 옆 `[GPU]`·`[CPU]` 배지 설명** |
| 5절 머리말 | **항상 보이는 기기 배지 세 상태 설명** |
| **7절 새 항목** | **"그래픽카드가 있는데 CPU로 돈다"** |
| 8절 새 PC | 속도 표에 "설치 때 GPU 파일을 받으면" 조건 명시 |

7절에 넣을 새 항목이다.

> ### 그래픽카드가 있는데 CPU 로 돈다
>
> 머리말에 `GPU 있으나 사용 불가` 가 떠 있으면 이것이다.
>
> `진단` 을 눌러 `GPU 진단` 을 본다. 어느 줄에서 `못 했다` 가 나는지가 원인이다.
>
> 대개는 `setup.py` 를 다시 더블클릭하면 해결된다. 그래픽카드용 파일을 받고 경로를 잡아준다.
>
> 그래도 안 되면 그래픽카드 드라이버가 오래된 것이다. `진단` 화면의 `드라이버 CUDA` 가 **12.0 이상**이어야 한다.

5절 "지금 하는 일" 에 넣을 것이다.

> 배속 옆의 `[GPU]` 또는 `[CPU]` 가 지금 무엇으로 돌고 있는지 알려준다. `[CPU · GPU 사용 불가]` 로 나오면 그래픽카드가 있는데 못 쓰고 있는 것이다. 7절을 본다.

**문서와 코드를 같은 커밋에 넣는다.** 화면이 바뀌었는데 문서가 옛 화면을 설명하면 더 헷갈린다.

---

## 7. 작업 순서

| 순서 | 내용 | 인수 |
|---|---|---|
| 1 | §2 실패 상태 정합 · 진행 막대 · 전체 중지 | 흉내 낸 실패로 화면이 올바르게 바뀐다. 막대가 100%에 닿는다. 전체 중지가 듣는다 |
| 2 | §3 기기 자동 판정 | 표 세 줄(`auto`·`cuda`·`cpu`)이 각각 맞게 동작 |
| 3 | §4 인디케이터 | 머리말 배지 세 상태 · 진행 화면 배지 · 전환 사유가 보인다 |
| 4 | §5 설치기·PATH 등록·의존성 고정 | **깨끗한 PC에서 설치만으로 GPU가 잡힌다** |
| 4b | §6-3 설치안내.md 갱신 | 코드와 같은 커밋 |
| 5 | 회귀 확인 | §6-2 |

**1번을 마치면 중간 보고한다.** 나머지는 이어서 해도 된다.

보고에 담을 것이다.

- 추가한 시험과 결과
- R1·R2 대조 (배속은 기기 표기와 함께)
- 종료 상태 전이 지점을 몇 곳에서 모았는지
- 새 PC에서 `auto`가 CPU로 내려가 완주하는 것을 실제로 확인했는지
- **`setup.py` 만으로 GPU가 잡히는지.** PATH를 손으로 넣지 않고
- 판단이 갈린 지점

---

## 8. 다음 단계 예고

이 단계가 끝나면 원격 접속이다.

| 단계 | 내용 |
|---|---|
| 2-1 | 원격 모드 골격 — 바인딩 선택 · 토큰 인증 · 원격에서 임의 경로 차단 |
| 2-2 | Cloudflare Tunnel + Access 연결과 문서 |
| 2-3 | 청크 업로드 (Cloudflare 무료 요금제 100MB 상한 회피) |
| 2-4 | 소품 — 파일명 충돌 자동 번호 · 간투사 보존 개선 시험 |

**2-0 없이는 2-1로 가지 않는다.** 다른 PC에서 돌지 않는 도구에 원격을 얹을 수 없다.
