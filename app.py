#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — 받아쓰기. 이 PC에서만 도는 한국어 음성 텍스트 변환기.

whisper_local(faster-whisper) 하나만 쓴다. 외부로 음성을 보내지 않고 비용도 없다.
3시간짜리 긴 파일을 끝까지 돌릴 수 있는지 확인하는 것이 이 앱의 목적이다.

준비
    pip install faster-whisper

실행
    python app.py
    브라우저가 열리지 않으면 http://127.0.0.1:8765 로 접속한다.

표준 라이브러리만으로 서버를 띄운다. Flask 등 추가 설치가 필요 없다.
"""

import html
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.parse
import webbrowser

APP_VERSION = "2026-08-15.2"     # 화면 우상단과 콘솔에 찍힌다. 갱신 확인용이다.

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8765"))
# 변환 대상 폴더. 실행 위치와 무관하게 스크립트 옆의 audio 를 본다.
AUDIODIR = os.path.abspath(os.environ.get("AUDIODIR", os.path.join(BASE, "audio")))
OUTDIR = os.path.abspath(os.environ.get("OUTDIR", os.path.join(BASE, "out_text")))
AUDIO_EXT = (".wav", ".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".aac",
             ".wma", ".webm", ".mkv", ".mov", ".amr", ".opus")

MODELS = ["large-v3-turbo", "large-v3", "medium", "small"]
DEVICES = ["auto", "cpu", "cuda"]

# 연산 정밀도는 int8 하나로 고정한다. 화면에서 고르지 않는다.
#
# 한때 int8 · int8_float16 · float16 셋을 열어 뒀다. 실측해 보니 이득이 없었다.
# (2026-08-15 · RTX 3060 · audio/이승은코치.m4a 18:14 · 다른 인자는 동일)
#
#   int8          31.53x   구간 184 · 5490자
#   int8_float16  31.71x   구간 184 · 5490자    산출물이 같다. 0.6% 는 잡음이다
#   float16       30.99x   구간 187 · 5570자    느린 데다 산출물이 달라진다
#
# float16 은 R1 을 통과하지 못한다. 화자 오귀속률이 0.00% → 7.14% 로 올랐다
# (허용 +2%p). 낮은 정밀도 누적이 시각을 1초 밀어 어절 하나가 화자 경계를
# 넘어갔다. 이 프로젝트가 CER 보다 중요하게 치는 지표다.
#
# 그리고 CPU 는 int8_float16 · float16 을 지원하지 않는다. 조용히 내려가지
# 않고 ValueError 를 던진다. 화면이 기기=cpu 와 함께 고르게 놔뒀으므로
# 이득 없는 선택지가 고장 경로를 하나 만들어 두고 있었다.
#
#   cpu  지원 = {int8, int16, float32, int8_float32}
#   cuda 지원 = {int8, int8_float32, float16, bfloat16, int8_float16, ...}
#
# int8 은 두 기기 모두에서 도는 유일한 값이기도 하다. 되살리지 않는다.
COMPUTE = "int8"

# 모델·기기별 대략의 실시간 배속. 시작 전 예상치에만 쓰고, 시작 후에는 실측으로 바꾼다.
ROUGH_SPEED = {
    ("large-v3-turbo", "cpu"): 2.0, ("large-v3-turbo", "cuda"): 30.0,
    ("large-v3", "cpu"): 0.7,       ("large-v3", "cuda"): 12.0,
    ("medium", "cpu"): 1.5,         ("medium", "cuda"): 20.0,
    ("small", "cpu"): 4.0,          ("small", "cuda"): 45.0,
}

# ─────────────────────────────────────────────────────────────
# 기록 — data/app.log
#
# pythonw.exe로 창 없이 띄우면 sys.stdout이 None이 된다. 그때 print()는
# 예외를 내지 않고 조용히 사라진다. 화자 분리 실패 사유도, 포트 충돌
# 안내도 함께 사라진다. 그래서 모든 출력이 이 파일을 거친다.
#
# 잠금 순서 — STATE_LOCK → LOCK → _LOG_LOCK.
# _LOG_LOCK은 잎 잠금이다. 이것을 쥔 채 다른 잠금을 잡지 않는다.
# ─────────────────────────────────────────────────────────────

DATA = os.path.abspath(os.environ.get("DATADIR", os.path.join(BASE, "data")))
LOG_PATH = os.path.join(DATA, "app.log")
LOG_MAX = 5 * 1024 * 1024                  # 이 크기를 넘으면 회전한다
LOG_KEEP = 3                               # app.log.1 ~ app.log.3

_LOG_LOCK = threading.Lock()
_CONSOLE = sys.stdout is not None          # 무창 실행이면 False


def mask(v: str) -> str:
    """토큰·키를 기록할 때 쓴다. 값은 남기지 않는다. 앞 4자와 길이까지다."""
    v = (v or "").strip()
    return f"{v[:4]}…({len(v)}자)" if v else "없음"


def _rotate() -> None:
    """app.log.3을 버리고 .2 → .3, .1 → .2, app.log → .1 로 민다."""
    try:
        if os.path.getsize(LOG_PATH) < LOG_MAX:
            return
    except OSError:
        return
    last = f"{LOG_PATH}.{LOG_KEEP}"
    if os.path.exists(last):
        os.remove(last)
    for i in range(LOG_KEEP - 1, 0, -1):
        src = f"{LOG_PATH}.{i}"
        if os.path.exists(src):
            os.replace(src, f"{LOG_PATH}.{i + 1}")
    os.replace(LOG_PATH, f"{LOG_PATH}.1")


def log(msg: str = "") -> None:
    """콘솔이 있으면 콘솔에도 쓴다. 파일에는 언제나 시각과 함께 남긴다."""
    if _CONSOLE:
        try:
            print(msg)
        except Exception:
            pass                           # 콘솔 인코딩 사고가 작업을 멈추지 않는다
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    body = "".join(f"{stamp}  {ln.rstrip()}\n" for ln in msg.splitlines() if ln.strip())
    with _LOG_LOCK:
        try:
            os.makedirs(DATA, exist_ok=True)
            _rotate()
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(body or f"{stamp}\n")
        except OSError:
            pass                           # 기록에 실패해도 본 작업은 계속한다


class _LogStream:
    """
    무창 실행에서 sys.stdout·sys.stderr를 대신한다. 스레드 역추적도 여기로 들어온다.

    파이썬은 역추적을 여러 번에 나눠 쓴다. 줄바꿈이 올 때까지 모았다가
    한 줄로 넘긴다. 그러지 않으면 한 문장이 세 줄로 쪼개진다.
    """

    def __init__(self):
        self._buf = ""
        self._lock = threading.Lock()      # _LOG_LOCK 보다 먼저 잡는다

    def write(self, s):
        with self._lock:
            self._buf += s
            lines = self._buf.split("\n")
            self._buf = lines.pop()
            out = [ln for ln in lines if ln.strip()]
        for ln in out:
            log(ln)

    def flush(self):
        with self._lock:
            rest, self._buf = self._buf, ""
        if rest.strip():
            log(rest)

    def isatty(self):
        return False


def setup_log() -> None:
    """기동 직후 가장 먼저 부른다. 이 뒤로는 print() 대신 log()를 쓴다."""
    os.makedirs(DATA, exist_ok=True)
    if not _CONSOLE:                       # 무창 실행 — 사라질 출력에 물길을 낸다
        sys.stdout = sys.stderr = _LogStream()
    elif not sys.stdout.isatty():          # 파이프·파일 리다이렉트 — cp949에서 죽는다
        for s in (sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    log("")
    log(f"── 받아쓰기 v{APP_VERSION} 시작 · PID {os.getpid()} · "
        f"콘솔 {'있음' if _CONSOLE else '없음'}")


def log_tail(n: int = 200) -> list:
    """진단 화면에 보일 최근 줄."""
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


# ─────────────────────────────────────────────────────────────
# 상태 — data/settings.json · queue.json · history.json
#
# 쓰기는 원자적으로 한다. 임시 파일에 쓰고 flush·fsync 한 뒤 os.replace 로
# 바꾼다. 3시간짜리 전사 도중에 죽어도 JSON이 깨지지 않아야 한다.
#
# 진행률로는 저장하지 않는다. 상태가 바뀔 때만 쓴다.
# 초당 몇 번씩 디스크를 치면 전사가 디스크를 기다린다.
# ─────────────────────────────────────────────────────────────

SETTINGS_PATH = os.path.join(DATA, "settings.json")
QUEUE_PATH = os.path.join(DATA, "queue.json")
HISTORY_PATH = os.path.join(DATA, "history.json")

STATE_LOCK = threading.RLock()             # 잠금 순서 — STATE_LOCK → LOCK → _LOG_LOCK
HISTORY_MAX = 200

# 기본값은 "코칭 대화를 받아쓴다" 를 향한다. 이 도구의 주 용도다.
#   화자 분리 켬    둘 이상이 말하는 녹음이 대부분이다
#   자동 추정       인원을 모르고 담는 일이 잦다. 틀린 수를 못 박으면
#                  자동 추정보다 나쁘다 — pyannote 가 찾을 여지까지 없앤다
#   민감            코칭 대화에서 맞장구는 잡음이 아니다
#   시각 포함 · 정본화 초안   본문을 온전히 담는 둘. 평문 txt 는 그 부분집합이라 껐다
DEFAULT_OPT = {
    "model": "large-v3-turbo", "device": "auto", "compute": COMPUTE,
    "lang": "ko", "beam": 5, "silence": 500,
    "vad": True, "fallback": True, "prompt": False, "fixterms": True,
    "hotwords": "",
    "diarize": True, "nspk": 0, "sens": "high",
    "formats": {"plain": False, "timed": True, "srt": False, "canon": True},
}


def norm_opt(o) -> dict:
    """저장된 설정을 지금 코드가 아는 모양으로 맞춘다.

    없는 키만 기본값으로 채운다. **사람이 고른 값은 건드리지 않는다** —
    판올림이 남의 선택을 되돌리면 안 된다. 예외는 `compute` 하나다.
    없앤 GPU 전용 정밀도가 남아 있으면 CPU 에서 ValueError 로 죽으므로
    그것만 되돌린다. 죽이지 않고 고쳐서 쓴다.
    """
    o = dict(DEFAULT_OPT, **(o or {}))
    o["compute"] = COMPUTE
    o["formats"] = dict(DEFAULT_OPT["formats"], **(o.get("formats") or {}))
    return o

# 지시서 §3-4 의 기본 제공 두 개. 값을 바꾸지 않는다.
DEFAULT_PRESETS = [
    {"name": "코칭 시연", "settings": dict(
        DEFAULT_OPT, diarize=True, nspk=2, sens="high",
        formats={"plain": False, "timed": True, "srt": False, "canon": True})},
    {"name": "강의 받아쓰기", "settings": dict(
        DEFAULT_OPT, diarize=False,
        formats={"plain": True, "timed": False, "srt": True, "canon": False})},
]

SETTINGS = {"version": 1, "last": dict(DEFAULT_OPT),
            "recent_outdirs": [OUTDIR], "presets": DEFAULT_PRESETS}
QUEUE = {"version": 1, "items": []}        # waiting · running · interrupted 만 남는다
HISTORY = {"version": 1, "items": []}      # 끝난 것은 여기로 옮긴다

_ID_SEQ = [0]


def new_id(prefix: str) -> str:
    with STATE_LOCK:
        _ID_SEQ[0] += 1
        return f"{prefix}_{int(time.time())}_{_ID_SEQ[0]}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load_json(path: str, default: dict) -> dict:
    """깨진 파일은 .bad 로 밀어두고 기본값으로 시작한다. 앱이 못 뜨는 일은 없어야 한다."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "version" in data:
            return data
        raise ValueError("version 필드가 없다")
    except FileNotFoundError:
        return json.loads(json.dumps(default))
    except Exception as e:
        bad = path + ".bad"
        try:
            os.replace(path, bad)
            log(f"   {os.path.basename(path)} 를 읽지 못해 {os.path.basename(bad)} 로 옮겼다. {e}")
        except OSError:
            pass
        return json.loads(json.dumps(default))


def save_json(path: str, obj: dict) -> None:
    """임시 파일 → flush → fsync → os.replace. 중간에 죽어도 원본이 살아 있다."""
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        log(f"   {os.path.basename(path)} 저장 실패. {e}")


def save_settings():
    with STATE_LOCK:
        save_json(SETTINGS_PATH, SETTINGS)


def save_queue():
    with STATE_LOCK:
        save_json(QUEUE_PATH, QUEUE)


def save_history():
    with STATE_LOCK:
        save_json(HISTORY_PATH, HISTORY)


def load_state() -> None:
    """기동 시 한 번 부른다. 돌던 항목은 interrupted 로 되돌린다."""
    global SETTINGS, QUEUE, HISTORY
    with STATE_LOCK:
        SETTINGS = load_json(SETTINGS_PATH, SETTINGS)
        QUEUE = load_json(QUEUE_PATH, QUEUE)
        HISTORY = load_json(HISTORY_PATH, HISTORY)
        SETTINGS.setdefault("last", dict(DEFAULT_OPT))
        SETTINGS.setdefault("recent_outdirs", [OUTDIR])
        SETTINGS.setdefault("presets", DEFAULT_PRESETS)
        QUEUE.setdefault("items", [])
        HISTORY.setdefault("items", [])

        # 없앤 정밀도가 옛 파일에 남아 있을 수 있다. 읽을 때 되돌린다.
        # 큐에 담긴 항목까지 훑는다 — 담아 두고 판올림한 경우가 있다.
        SETTINGS["last"] = norm_opt(SETTINGS["last"])
        for p in SETTINGS["presets"]:
            if isinstance(p, dict) and isinstance(p.get("settings"), dict):
                p["settings"] = norm_opt(p["settings"])
        for it in QUEUE["items"]:
            if isinstance(it.get("settings"), dict):
                it["settings"] = norm_opt(it["settings"])

        # 앱이 죽어 running 으로 남은 항목. 자동으로 다시 돌리지 않는다.
        # 부분 산출 파일이 이미 있는데 처음부터 다시 돌면 덮어쓴다. 사람이 고른다.
        n = 0
        for it in QUEUE["items"]:
            if it.get("state") == "running":
                it["state"] = "interrupted"
                it["message"] = "앱이 중단됐다. 다시 할지 고르면 된다."
                n += 1
        if n:
            save_queue()
            log(f"   중단된 작업 {n}건을 찾았다. 자동으로 다시 돌리지 않는다.")


def remember_outdir(path: str) -> None:
    with STATE_LOCK:
        rec = [p for p in SETTINGS.get("recent_outdirs", []) if p != path]
        SETTINGS["recent_outdirs"] = ([path] + rec)[:5]
        save_settings()


def under(path: str, root: str) -> bool:
    """접두 검사에 구분자를 붙인다. out_text 가 out_text2 를 통과시키면 안 된다."""
    a = os.path.normcase(os.path.abspath(path))
    b = os.path.normcase(os.path.abspath(root))
    return a == b or a.startswith(b + os.sep)


def allowed_roots() -> list:
    """/open 이 열어도 되는 뿌리. 설정에 등록된 출력 폴더만이다. 임의 경로를 받지 않는다."""
    with STATE_LOCK:
        return [OUTDIR] + [p for p in SETTINGS.get("recent_outdirs", []) if p]


def check_outdir(path: str) -> dict:
    """없으면 만들고, 실제로 써 보고 결과를 돌려준다. 브라우저는 폴더 대화상자를 못 연다."""
    path = os.path.abspath(os.path.expanduser((path or "").strip().strip('"')))
    if not path:
        return {"ok": False, "why": "경로가 비었다.", "path": ""}
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        return {"ok": False, "why": f"폴더를 만들지 못했다. {e}", "path": path}
    probe = os.path.join(path, ".write_test")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("")
        os.remove(probe)
    except OSError as e:
        return {"ok": False, "why": f"쓸 수 없는 폴더다. {e}", "path": path}
    return {"ok": True, "why": "", "path": path}


# ─────────────────────────────────────────────────────────────
# 모델 캐시
#
# 대기열에 5건을 걸면 모델을 5번 적재한다. CPU에서 회당 30~60초다.
# 같은 조합이면 그대로 쓰고, 바뀌면 이전 것을 놓고 새로 만든다.
# 실행기가 하나뿐이라 이 캐시에 동시에 손대는 스레드는 없다.
# ─────────────────────────────────────────────────────────────

CACHE_IDLE_SEC = 600                       # 대기열이 비고 이만큼 지나면 놓는다

_WHISPER = {"key": None, "obj": None}
_DIA = {"key": None, "obj": None}
_CACHE_USED = [0.0]

# ─────────────────────────────────────────────────────────────
# CUDA DLL 경로
#
# pip 는 DLL 을 site-packages 안에 넣지만 PATH 에 올리지 않는다. 파이썬이
# import 하는 패키지가 아니라 네이티브 라이브러리가 찾아야 하는 파일이라서다.
# ctranslate2 가 cublas64_12.dll 을 못 찾는 가장 흔한 원인이 이것이다.
#
# 두 곳을 본다. 기계마다 어디서 얻는지가 다르다.
#   nvidia/*/bin  — nvidia-cublas-cu12 같은 휠. 네임스페이스 패키지라
#                   __file__ 이 None 이므로 __path__ 를 순회한다
#   torch/lib     — 만든 PC 는 휠이 하나도 없고 여기서 얻는다
#
# torch 를 여기서 명시적으로 import 하는 것도 목적이다. 지금까지는
# import faster_whisper 의 부작용으로 우연히 불려 왔다. 우연에 기대지 않는다.
# ─────────────────────────────────────────────────────────────

CUDA_DLL_DIRS = []
_DLL_HANDLES = []          # add_dll_directory 가 준 손잡이. 놓으면 등록이 풀린다
_AUTO_DEV = [None]         # auto 가 마지막에 무엇으로 풀렸는지. 헛시도를 막는다


def register_cuda_dlls() -> list:
    """
    CUDA DLL 폴더를 이 프로세스에 등록한다. 한 번만 돈다.

    STT_NO_CUDA_DLL=1 이면 아무것도 하지 않는다. 고장 난 상태를 재현해
    탐침이 실제로 실패하는지 보기 위한 손잡이다. 실패하지 않는 탐침은 쓸모가 없다.
    """
    if getattr(register_cuda_dlls, "_done", False):
        return CUDA_DLL_DIRS
    register_cuda_dlls._done = True
    if os.environ.get("STT_NO_CUDA_DLL") == "1":
        log("   CUDA DLL 등록을 건너뛴다 (STT_NO_CUDA_DLL=1)")
        return CUDA_DLL_DIRS

    import glob
    cands = []
    try:
        import nvidia
        for root in list(nvidia.__path__):
            cands += sorted(glob.glob(os.path.join(root, "*", "bin")))
    except Exception:
        pass                               # 휠이 없어도 torch 쪽이 있을 수 있다
    try:
        import torch
        cands.append(os.path.join(os.path.dirname(torch.__file__), "lib"))
    except Exception:
        pass

    for d in cands:
        if not os.path.isdir(d) or d in CUDA_DLL_DIRS:
            continue
        CUDA_DLL_DIRS.append(d)
        if os.name == "nt":
            try:
                # 손잡이를 붙들어 둔다. 버리면 GC 가 등록을 되돌린다.
                _DLL_HANDLES.append(os.add_dll_directory(d))
            except OSError:
                pass
        # 자식 프로세스와 일부 로더를 위해 PATH 에도 넣는다
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

    if CUDA_DLL_DIRS:
        log(f"   CUDA DLL 경로 {len(CUDA_DLL_DIRS)}곳 등록 — "
            + " · ".join(os.path.basename(os.path.dirname(d)) for d in CUDA_DLL_DIRS))
    else:
        log("   CUDA DLL 폴더를 찾지 못했다. CPU 로 돈다.")
    return CUDA_DLL_DIRS


# ─────────────────────────────────────────────────────────────
# 기기 상태
#
# "이 PC 에 GPU 가 있는가" 와 "지금 무엇으로 도는가" 는 다른 질문이다.
# 앞의 것은 즉답할 수 있고, 뒤의 것은 실제로 돌려 봐야 안다.
#
# 기동          get_cuda_device_count() 로 물리 GPU 유무만. 모델을 만들지 않는다
# 첫 작업 후     실제 사용 가능 여부가 확정된다
# 진단에서 누를 때  탐침을 돌린다
#
# 기동 시 탐침을 돌리지 않는다. 무창 실행에서 몇 초를 이유 없이 기다리게 된다.
# ─────────────────────────────────────────────────────────────

GPU = {"count": None, "usable": None, "why": "", "name": ""}
PROBE_WAV = os.path.join(BASE, "tests", "probe.wav")


def device_options() -> list:
    """기기 목록. `auto` 옆에 기동 시 판정을 적어 준다.

    값은 `auto` 그대로 둔다. 기본값을 `cuda` 로 못 박으면 대체 경로가 꺼진다
    (`transcribe()` 의 `auto만` 규칙). GPU 가 말썽인 PC 에서 그대로 오류로
    끝난다는 뜻이다. 부족했던 것은 동작이 아니라 표시였다 — `auto` 라고만
    적혀 있으니 그것이 GPU 를 쓴다는 뜻인지 알 수가 없었다.
    """
    auto = "auto · GPU 사용" if gpu_count() > 0 else "auto · CPU만"
    return [["auto", auto], ["cpu", "cpu"], ["cuda", "cuda"]]


def gpu_count() -> int:
    """물리 GPU 개수. 즉답이다 — 모델을 만들지 않으므로 내려받기가 없다."""
    if GPU["count"] is None:
        try:
            register_cuda_dlls()
            import ctranslate2
            GPU["count"] = int(ctranslate2.get_cuda_device_count())
        except Exception as e:
            GPU["count"] = 0
            GPU["why"] = GPU["why"] or f"{type(e).__name__}: {e}"[:200]
    return GPU["count"]


def note_gpu(usable: bool, why: str = "") -> None:
    """실제 결과로 판정을 갱신한다. 작업이 끝나거나 탐침이 돌면 불린다."""
    GPU["usable"] = usable
    if why:
        GPU["why"] = why[:300]
    elif usable:
        GPU["why"] = ""


def gpu_badge() -> dict:
    """머리말에 늘 보이는 배지. 세 상태뿐이다."""
    n = gpu_count()
    if not n:
        return {"state": "cpu", "label": "CPU만 사용", "why": GPU["why"], "count": 0}
    if GPU["usable"] is False:
        return {"state": "broken", "label": "GPU 있으나 사용 불가",
                "why": GPU["why"], "count": n}
    return {"state": "gpu", "label": "GPU 사용 가능", "why": "", "count": n}


def gpu_probe() -> dict:
    """
    실제로 짧게 전사해 본다. **모델을 만드는 것만으로는 알 수 없다** —
    CUDA 라이브러리는 실제 음성을 인코딩할 때 비로소 적재된다. 무음이나
    잡음은 인코더를 돌리지 않아 탐침이 되지 못한다. tests/probe.wav 는
    2.8초짜리 실제 음성이다.
    """
    if not os.path.isfile(PROBE_WAV):
        return {"ok": False, "why": f"탐침 음원이 없다 — {PROBE_WAV}", "sec": 0.0}
    if not gpu_count():
        return {"ok": False, "why": "이 PC 에서 GPU 를 찾지 못했다.", "sec": 0.0}
    t0 = time.time()
    try:
        with STATE_LOCK:
            name = (SETTINGS.get("last") or {}).get("model") or DEFAULT_OPT["model"]
        m, cached, dev, comp = get_whisper(name, "cuda", COMPUTE)
        segs, _ = m.transcribe(PROBE_WAV, language="ko", vad_filter=False)
        n = sum(1 for _ in segs)          # 다 소진해야 인코더가 실제로 돈다
        note_gpu(True)
        log(f"   GPU 탐침 통과 — {time.time() - t0:.1f}초 · 구간 {n}개")
        return {"ok": True, "sec": round(time.time() - t0, 2), "segments": n,
                "device": dev, "model": name, "cached": cached}
    except Exception as e:
        why = f"{type(e).__name__}: {e}"
        note_gpu(False, why)
        log(f"   GPU 탐침 실패 — {why[:200]}")
        return {"ok": False, "why": why[:400], "sec": round(time.time() - t0, 2)}


def nvidia_smi() -> dict:
    """
    드라이버가 있는지 본다. **nvidia-smi 가 판정 기준이다.**
    wmic 로 카드 이름만 보면 드라이버가 없어도 이름이 나와 헛짚는다.
    """
    import re
    import subprocess
    try:
        r = subprocess.run(["nvidia-smi"], capture_output=True, timeout=20)
    except FileNotFoundError:
        return {"ok": False, "why": "nvidia-smi 가 없다. NVIDIA 드라이버가 깔려 있지 않다."}
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    if r.returncode != 0:
        return {"ok": False, "why": "nvidia-smi 가 오류를 냈다. 드라이버를 확인해달라."}
    out = r.stdout.decode("utf-8", "replace")
    cuda = re.search(r"CUDA Version:\s*([\d.]+)", out)
    name = re.search(r"\|\s+\d+\s+(NVIDIA[^|]+?)\s{2,}", out)
    ver = cuda.group(1) if cuda else ""
    old = False
    try:
        old = bool(ver) and float(".".join(ver.split(".")[:2])) < 12.0
    except ValueError:
        pass
    return {"ok": True, "cuda": ver, "name": (name.group(1).strip() if name else "NVIDIA GPU"),
            "old": old,
            "why": ("드라이버가 낡았다. CUDA 12.0 이상이 필요하다." if old else "")}


def gpu_report(probe: bool = False) -> list:
    """
    설치기와 진단 화면이 함께 쓴다. 항목마다 (이름, 상태, 설명).
    상태는 ok · fail · skip 셋이다.

    **끊긴 고리를 짚는 것이 목적이다.** 패키지가 있는지만 보면 이번 사고를 놓친다 —
    DLL 은 디스크에 있었고 경로에 없었다.
    """
    import importlib.metadata as md
    rows = []

    smi = nvidia_smi()
    if not smi["ok"]:
        rows.append(("드라이버", "fail", smi["why"]))
    elif smi["old"]:
        rows.append(("드라이버", "fail", f"{smi['name']} · CUDA {smi['cuda']} — {smi['why']}"))
    else:
        rows.append(("드라이버", "ok", f"{smi['name']} · CUDA {smi['cuda'] or '?'}"))

    try:
        import ctranslate2
        v = ctranslate2.__version__
        gen = "CUDA 12 · cuDNN 9" if v.split(".")[0] == "4" else "판정 못 함"
        rows.append(("ctranslate2", "ok", f"{v} → {gen} 계열이 필요하다"))
    except Exception:
        rows.append(("ctranslate2", "fail", "없다. faster-whisper 가 설치되지 않았다"))

    for pkg in ("nvidia-cublas-cu12", "nvidia-cudnn-cu12"):
        try:
            rows.append((pkg, "ok", md.version(pkg)))
        except Exception:
            rows.append((pkg, "skip", "휠이 없다. torch 가 들고 있으면 그것으로도 된다"))

    dirs = register_cuda_dlls()
    for dll in ("cublas64_12.dll", "cudnn64_9.dll"):
        where = next((d for d in dirs if os.path.isfile(os.path.join(d, dll))), "")
        rows.append((dll, "ok" if where else "fail",
                     where or "등록된 폴더 어디에도 없다"))

    rows.append(("DLL 경로 등록", "ok" if dirs else "fail",
                 f"{len(dirs)}곳 — " + " · ".join(
                     os.path.basename(os.path.dirname(d)) for d in dirs) if dirs
                 else "찾은 폴더가 없다"))

    if probe:
        r = gpu_probe()
        rows.append(("시험 전사", "ok" if r["ok"] else "fail",
                     f"{r['sec']}초 · {r.get('device', '')}" if r["ok"] else r.get("why", "")))
    else:
        rows.append(("시험 전사", "skip", "탐침을 돌리지 않았다"))
    return rows


def get_whisper(model: str, device: str, compute: str):
    """
    (모델, 캐시적중, 실제기기, 실제정밀도) 를 돌려준다.

    auto 는 CUDA 를 먼저 만들어 보고 안 되면 CPU 로 내려간다.
    cuda 를 명시로 고른 경우에는 내려가지 않는다 — 조용히 느려지면 원인을 못 찾는다.

    **캐시 키에는 요청 기기가 아니라 실제 기기를 넣는다.** auto 로 요청해 CPU 로
    내려갔는데 키가 auto 로 남으면 다음 항목이 또 CUDA 를 시도한다.
    """
    from faster_whisper import WhisperModel
    _CACHE_USED[0] = time.time()

    want = "cuda" if device == "auto" else device
    # 한 번 내려갔으면 다음 항목부터는 CUDA 를 다시 시도하지 않는다.
    # 이것이 없으면 항목마다 실패를 되풀이하며 몇 초씩 버린다.
    if device == "auto" and _AUTO_DEV[0] == "cpu":
        want, compute = "cpu", COMPUTE
    if want == "cuda":
        register_cuda_dlls()

    key = (model, want, compute)
    if _WHISPER["key"] == key and _WHISPER["obj"] is not None:
        return _WHISPER["obj"], True, want, compute

    _WHISPER.update(key=None, obj=None)    # 새로 만들기 전에 먼저 놓는다. 메모리 때문이다
    try:
        obj = WhisperModel(model, device=want, compute_type=compute)
    except Exception as e:
        if device != "auto" or not is_cuda_error(e):
            raise
        log(f"   CUDA 로 모델을 못 올렸다. CPU 로 내려간다. {str(e)[:120]}")
        want, compute = "cpu", COMPUTE
        _AUTO_DEV[0] = "cpu"
        key = (model, want, compute)
        if _WHISPER["key"] == key and _WHISPER["obj"] is not None:
            return _WHISPER["obj"], True, want, compute
        obj = WhisperModel(model, device=want, compute_type=compute)

    if device == "auto":
        _AUTO_DEV[0] = want
    _WHISPER.update(key=key, obj=obj)
    return obj, False, want, compute


def get_dia_pipeline(tok: str):
    from pyannote.audio import Pipeline
    key = DIA_MODEL
    _CACHE_USED[0] = time.time()
    if _DIA["key"] == key and _DIA["obj"] is not None:
        return _DIA["obj"], True
    _DIA["obj"] = None
    _DIA["key"] = None
    try:
        pipe = Pipeline.from_pretrained(DIA_MODEL, token=tok)
    except TypeError:                      # 3.x 계열은 인자 이름이 다르다
        pipe = Pipeline.from_pretrained(DIA_MODEL, use_auth_token=tok)
    if pipe is None:
        raise RuntimeError("모델 접근이 거부됐다. HuggingFace에서 약관에 동의했는지 확인해달라.")
    # torch.cuda.is_available() 이 참이어도 실제 연산에서 깨질 수 있다.
    # 실패하면 CPU 에 둔다. 조용히 넘기지 않고 남긴다 — 배속이 왜 낮은지의 단서다.
    try:
        import torch
        if torch.cuda.is_available():
            pipe.to(torch.device("cuda"))
            log("   화자 분리 — GPU")
        else:
            log("   화자 분리 — CPU (GPU를 찾지 못했다)")
    except Exception as e:
        log(f"   화자 분리 — CPU 로 둔다. GPU 전환에 실패했다. {str(e)[:120]}")
    _DIA.update(key=key, obj=pipe)
    return pipe, False


def free_gpu_cache() -> float:
    """torch 가 쥔 GPU 블록을 드라이버에 돌려준다. 돌려준 MB 를 준다.

    참조를 놓는 것만으로는 돌아가지 않는다. torch 의 할당기가 해제한 블록을
    자기 풀에 담아 두기 때문이다. 그런데 **whisper 는 ctranslate2 의 별도
    할당기를 쓴다.** 그래서 pyannote 가 한 번 돌아 풀이 부풀면 다음 항목의
    전사가 쓸 GPU 메모리가 남지 않는다.

    실측 (RTX 3060 12GB · 2026-08-15) — 이 호출이 없을 때

        시작 전                      593 MiB
        1번 sample3.wav (26초)     11938 MiB   ← 화자 분리가 여기서 부풀린다
        2번 이승은코치 (18분)   전사 12.32x
        3번 이승은코치 (18분)   전사 14.98x

    같은 음원을 혼자 돌리면 전사가 35.9x 다. 항목 안 순서가 전사 → 화자
    분리라 첫 항목만 메모리가 넉넉하고, 둘째부터 셋 중 하나로 느려진다.
    """
    try:
        import torch
        if not torch.cuda.is_available():
            return 0.0
        before = torch.cuda.memory_reserved()
        torch.cuda.empty_cache()
        return max(0.0, (before - torch.cuda.memory_reserved()) / 2 ** 20)
    except Exception:
        return 0.0


def release_cache(why: str = "") -> None:
    if _WHISPER["obj"] is None and _DIA["obj"] is None:
        return
    _WHISPER.update(key=None, obj=None)
    _DIA.update(key=None, obj=None)
    try:
        import gc
        gc.collect()
    except Exception:
        pass
    # 참조를 놓는 것만으로는 GPU 메모리가 드라이버로 돌아가지 않는다.
    # 앱을 띄워 둔 채로는 다른 프로그램이 GPU 를 못 쓴다는 뜻이다.
    mb = free_gpu_cache()
    freed = f" GPU {mb:.0f}MB 반환." if mb else ""
    log(f"   모델을 메모리에서 놓았다.{freed}{(' ' + why) if why else ''}")


# ─────────────────────────────────────────────────────────────
# 작업 끝맺기
#
# 종료 상태에서 phase 가 남으면 화면이 진행 중으로 보인다. 오류 띠가 떠 있는데
# "받아쓰는 중" 이 함께 남고 실패한 작업에 중단 버튼이 붙는다. 실제로 겪었다.
# 끝나는 길이 여럿이므로 전이를 이 함수 하나로 모은다.
# ─────────────────────────────────────────────────────────────

TERMINAL = ("done", "error", "cancelled")

# CUDA 계열 실패를 알아보는 표지. 오류 문구에 이것들이 섞여 나온다.
CUDA_HINTS = ("cublas", "cudnn", "cudart", "nvrtc", "libcu", "CUDA", "cuda")


def is_cuda_error(e) -> bool:
    return any(k in str(e) for k in CUDA_HINTS)


def finish_job(state: str, message: str = "", hint: str = "") -> None:
    """
    작업을 끝맺는다. 모든 종료 경로가 여기를 지난다. 예외 경로도 포함한다.

    processed 는 손대지 않는다 — 실패 지점을 그대로 보여야 어디서 멈췄는지 안다.
    다만 done 이면 duration 으로 맞춘다. 끝난 일이 98% 로 남으면 도구를 못 믿는다.
    """
    with LOCK:
        if state == "done" and JOB.get("duration"):
            JOB["processed"] = JOB["duration"]
        if hint:
            message = (message + "\n→ " + hint) if message else hint
        JOB.update(state=state, phase="", eta=0.0, message=message)


JOB = {
    "state": "idle",       # idle · loading · running · diarizing · merging · done · error · cancelled
    "phase": "",           # 화면에 띄울 단계 설명
    "dia_pct": 0.0,        # 화자 분리 진행률
    "speakers": 0,         # 찾아낸 화자 수
    "file": "", "stem": "",
    "duration": 0.0, "processed": 0.0,
    "started": 0.0, "elapsed": 0.0,
    "speed": 0.0, "eta": 0.0,
    "segments": 0, "chars": 0,
    "corrections": 0, "holds": 0,      # 이름·용어 교정 반영 건수와 보류 건수
    "qid": "", "hid": "", "outdir": "",   # 대기열 항목 · 이력 항목 · 출력 폴더
    "cached": False,                   # 모델을 캐시에서 꺼냈는지. 재적재 회귀를 본다
    "device": "", "device_note": "",   # 실제로 쓴 기기와, 전환이 있었다면 그 사유
    "tail": [], "outputs": [], "message": "",
}
LOCK = threading.Lock()
CANCEL = threading.Event()


# ─────────────────────────────────────────────────────────────
# 음원 살피기
# ─────────────────────────────────────────────────────────────

def probe_duration(path: str) -> float:
    """재생 길이를 초로 돌려준다. 못 읽으면 0."""
    try:
        import av
        with av.open(path) as c:
            if c.duration:
                return float(c.duration) / av.time_base
    except Exception:
        pass
    return 0.0


ENV_INFO = {"file": "", "keys": [], "searched": []}


def load_env() -> None:
    """
    env.local 등에서 HF_TOKEN 같은 값을 읽어 환경변수로 올린다.
    한 줄에 항목이 여러 개 붙어 있어도 쪼개서 읽는다.
    """
    import re
    pat = re.compile(r"^[^A-Za-z_]*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
    keyat = re.compile(r"(?:(?<=^)|(?<=\s))(?:\$[Ee]nv:|export |set |SET )?"
                       r"([A-Z][A-Z0-9_]{2,})\s*=")

    # 스크립트 폴더를 먼저 보고, 실행 위치도 함께 본다
    roots = [BASE] + ([os.getcwd()] if os.path.abspath(os.getcwd()) != BASE else [])
    for root in roots:
        for name in ("env.local", ".env.local", ".env", "env.txt"):
            path = os.path.join(root, name)
            ENV_INFO["searched"].append(path)
            if not os.path.isfile(path):
                continue

            text = open(path, encoding="utf-8-sig", errors="replace").read()
            for odd in ("\u2028", "\u2029", "\u0085"):
                text = text.replace(odd, "\n")

            for raw in text.splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                for pre in ("export ", "$env:", "$Env:", "set ", "SET "):
                    if line.startswith(pre):
                        line = line[len(pre):].lstrip()
                        break
                starts = [m.start(1) for m in keyat.finditer(line)]
                parts = ([line] if len(starts) <= 1 else
                         [line[a:b].strip() for a, b in zip(starts, starts[1:] + [len(line)])])
                for rec in parts:
                    m = pat.match(rec)
                    if not m:
                        continue
                    k = m.group(1)
                    v = m.group(2).strip().rstrip(";").strip("\"'").strip()
                    ENV_INFO["keys"].append(k)
                    if k not in os.environ:
                        os.environ[k] = v
            ENV_INFO["file"] = path
            return


def hf_token() -> str:
    for k in ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN"):
        v = os.environ.get(k, "").strip()
        if v.startswith("hf_"):
            return v
    return ""


_DUR_CACHE = {}


def cached_duration(path: str) -> float:
    """길이를 캐시한다. 폴더를 새로 고칠 때마다 다시 읽지 않는다."""
    key = (path, os.path.getmtime(path))
    if key not in _DUR_CACHE:
        _DUR_CACHE[key] = probe_duration(path)
    return _DUR_CACHE[key]


def list_audio() -> dict:
    """audio 폴더를 하위 한 단계까지 훑는다. 없으면 스크립트 폴더를 본다."""
    roots = [AUDIODIR] if os.path.isdir(AUDIODIR) else [BASE]
    if os.path.isdir(AUDIODIR):
        for name in sorted(os.listdir(AUDIODIR)):
            sub = os.path.join(AUDIODIR, name)
            if os.path.isdir(sub):
                roots.append(sub)

    found = []
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            p = os.path.join(root, name)
            if not (os.path.isfile(p) and name.lower().endswith(AUDIO_EXT)):
                continue
            stem = os.path.splitext(name)[0]
            # 변환됨 판정은 등록된 출력 폴더 전부에서 본다. 항목마다 폴더가 다를 수 있다.
            done = any(os.path.exists(os.path.join(d, stem + ext))
                       for d in allowed_roots()
                       for ext in (".txt", "_timed.txt", ".srt", "_canon.md"))
            found.append({
                "path": p,
                "name": os.path.relpath(p, roots[0]) if root != roots[0] else name,
                "size": os.path.getsize(p),
                "duration": cached_duration(p),
                "done": done,
            })
    return {"dir": AUDIODIR, "exists": os.path.isdir(AUDIODIR),
            "files": found[:200]}


def hms(sec: float) -> str:
    sec = max(0, int(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def srt_time(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ─────────────────────────────────────────────────────────────
# 전사
# ─────────────────────────────────────────────────────────────

DIA_MODEL = "pyannote/speaker-diarization-community-1"


def dia_check() -> dict:
    """화자 분리를 돌릴 수 있는 상태인지 미리 본다. 전사 전에 알려주기 위해서다."""
    if not hf_token():
        found = ", ".join(ENV_INFO["keys"]) or "없음"
        raw = os.environ.get("HF_TOKEN", "")
        if not ENV_INFO["file"]:
            why = ("설정 파일을 찾지 못했다. app.py와 같은 폴더에 env.local 이 있어야 한다. "
                   f"찾아본 곳 — {ENV_INFO['searched'][0]}")
        elif raw:
            why = (f"HF_TOKEN을 읽었으나 값이 hf_ 로 시작하지 않는다 "
                   f"(앞 4자 '{raw[:4]}', 길이 {len(raw)}자). 토큰이 잘렸는지 확인해달라.")
        else:
            why = (f"{os.path.basename(ENV_INFO['file'])} 를 읽었지만 HF_TOKEN이 없다. "
                   f"읽은 항목 — {found}. 앱을 껐다 켜야 반영된다.")
        return {"ok": False, "why": why}
    try:
        import pyannote.audio  # noqa: F401
    except ImportError:
        return {"ok": False, "why": "pyannote.audio가 없다.  pip install pyannote.audio"}
    try:
        import torch  # noqa: F401
    except ImportError:
        return {"ok": False, "why": "torch가 없다.  pip install torch"}
    return {"ok": True, "why": "준비됐다. 받아쓰기가 끝난 뒤 화자를 나눈다."}


def load_waveform(path: str):
    """
    음원을 16kHz 모노 파형으로 읽어 torch 텐서로 만든다.
    pyannote 4.x는 파일 경로를 받으려면 torchcodec을 요구하는데 윈도우 지원이 부실하다.
    faster-whisper에 딸려 오는 디코더로 우리가 직접 읽어 넘기면 그 의존이 사라진다.
    """
    import torch
    from faster_whisper.audio import decode_audio
    wav = decode_audio(path, sampling_rate=16000)          # float32 모노
    t = torch.from_numpy(wav).float().unsqueeze(0)          # (채널, 시간)
    return {"waveform": t, "sample_rate": 16000}


def run_diarization(path: str, want: int, upd) -> list:
    """
    pyannote로 화자 구간을 뽑는다. [(시작, 끝, 화자번호), ...]
    실패하면 빈 목록을 돌려준다. 텍스트는 이미 저장돼 있으므로 작업이 헛되지 않는다.
    """
    tok = hf_token()
    if not tok:
        upd(message="HF_TOKEN이 없어 화자 분리를 건너뛴다. "
                    "env.local에 HF_TOKEN=hf_... 한 줄을 넣어달라.")
        return []

    try:
        import pyannote.audio  # noqa: F401
    except ImportError:
        upd(message="pyannote.audio가 없어 화자 분리를 건너뛴다. "
                    "pip install pyannote.audio")
        return []

    upd(state="diarizing", phase="화자 분리 모델을 준비하고 있다", dia_pct=0.0)
    try:
        pipe, cached = get_dia_pipeline(tok)
        if cached:
            log("   화자 분리 모델을 캐시에서 꺼냈다")
    except Exception as e:
        msg = (f"화자 분리 모델을 불러오지 못했다. {type(e).__name__}: {e}  "
               f"HuggingFace에서 {DIA_MODEL} 약관에 동의했는지, 토큰이 Read 권한인지 확인해달라.")
        log("   " + msg)
        upd(message=msg)
        return []

    def hook(step, artifact=None, file=None, total=None, completed=None, **kw):
        if CANCEL.is_set():
            raise KeyboardInterrupt
        pct = (completed / total * 100) if (total and completed is not None) else None
        with LOCK:
            JOB["phase"] = f"화자 분리 · {step}"
            if pct is not None:
                JOB["dia_pct"] = pct

    kw = {"hook": hook}
    if want > 0:
        kw["num_speakers"] = want               # 아는 경우 못 박는다

    upd(phase="음원을 메모리로 읽는 중")
    try:
        source = load_waveform(path)
    except Exception as e:
        log(f"   파형 변환 실패, 파일 경로로 시도한다. {e}")
        source = path

    def call(**extra):
        return pipe(source, **kw, **extra)

    try:
        try:
            ann = call(exclusive=True)          # 겹침 없는 배타 분리
        except KeyboardInterrupt:
            raise
        except Exception:
            ann = call()                        # 이 판이 안 받으면 기본 방식으로
    except KeyboardInterrupt:
        return []
    except Exception as e:
        msg = f"화자 분리 중 멈췄다. {type(e).__name__}: {e}"
        if "torchcodec" in str(e):
            msg += ("  음원을 파형으로 바꾸지 못했다. "
                    "pip install -U faster-whisper 로 디코더를 갱신하거나 "
                    "pip install torchcodec 을 시도해달라.")
        log("   " + msg)
        upd(message=msg)
        return []
    finally:
        source = None
        # 파이프라인은 캐시에 남긴다. 돌려주는 것은 이번 판이 쓴 활성 메모리다.
        # 이것을 안 하면 다음 항목의 전사가 GPU 메모리를 못 얻어 3배 느려진다.
        # 실패 경로에서도 돌려준다 — 실패했다고 쥐고 있을 이유가 없다.
        mb = free_gpu_cache()
        if mb >= 256:
            log(f"   화자 분리 뒤 GPU {mb:.0f}MB 를 돌려줬다")

    if hasattr(ann, "speaker_diarization"):     # 4.x 배타 출력 형태
        ann = ann.speaker_diarization

    turns, names = [], {}
    for seg, _, label in ann.itertracks(yield_label=True):
        if label not in names:
            names[label] = len(names) + 1
        turns.append((float(seg.start), float(seg.end), names[label]))
    turns.sort(key=lambda t: t[0])
    upd(speakers=len(names), dia_pct=100.0)
    return turns


def speaker_at(turns: list, t0: float, t1: float) -> int:
    """구간과 가장 많이 겹치는 화자를 고른다. 겹치는 것이 없으면 0."""
    best, best_ov = 0, 0.0
    for s0, s1, spk in turns:
        if s1 <= t0:
            continue
        if s0 >= t1:
            break
        ov = min(t1, s1) - max(t0, s0)
        if ov > best_ov:
            best, best_ov = spk, ov
    return best


# 화자 전환 민감도. 값이 작을수록 짧은 맞장구를 살린다.
SENSITIVITY = {
    "high":   (0.00, 0),      # 맞장구 보존 — 코칭·인터뷰
    "normal": (0.35, 1),      # 보통
    "low":    (0.80, 2),      # 과분리 억제 — 잡음 많은 녹음
}
# 같은 화자의 잇단 턴을 한 발화로 볼 최대 간격
MERGE_GAP_SEC = 1.2


def turn_index(turns: list, t0: float, t1: float) -> int:
    """어절이 어느 턴에 속하는지 고른다. 겹침이 가장 큰 턴, 없으면 가장 가까운 턴."""
    best, best_ov = -1, 0.0
    for i, (s0, s1, _) in enumerate(turns):
        if s1 <= t0:
            continue
        if s0 >= t1:
            break
        ov = min(t1, s1) - max(t0, s0)
        if ov > best_ov:
            best, best_ov = i, ov
    if best >= 0:
        return best
    mid = (t0 + t1) / 2                      # 어느 턴에도 안 걸리면 가장 가까운 턴
    return min(range(len(turns)),
               key=lambda i: min(abs(turns[i][0] - mid), abs(turns[i][1] - mid)),
               default=-1)


def apply_speakers(rows: list, turns: list, sens: str = "high") -> list:
    """
    화자 구조는 pyannote가 나눈 턴을 그대로 따른다.
    어절을 턴에 배정할 뿐, 턴을 임의로 합치거나 지우지 않는다.

    앞서 짧은 발화를 잡음으로 보고 흡수하는 방식을 썼다가
    "네 반갑습니다" 같은 실제 맞장구가 통째로 상대 발화에 붙었다.
    코칭 대화에서 맞장구는 잡음이 아니라 분석 대상이다.
    """
    if not turns:
        return []
    min_sec, min_words = SENSITIVITY.get(sens, SENSITIVITY["high"])

    words = []
    for r in rows:
        ws = r.get("words") or []
        if ws:
            for w in ws:
                t = w["text"].strip()
                if t:
                    words.append({"start": w["start"], "end": w["end"], "text": t})
        else:
            words.append({"start": r["start"], "end": r["end"], "text": r["text"]})
    if not words:
        return []

    for w in words:
        w["ti"] = turn_index(turns, w["start"], w["end"])

    # 같은 턴에 배정된 어절을 묶는다. 턴이 바뀌면 발화도 바뀐다.
    groups = []
    for w in words:
        if groups and groups[-1]["ti"] == w["ti"]:
            groups[-1]["words"].append(w)
            groups[-1]["end"] = w["end"]
        else:
            groups.append({"ti": w["ti"], "spk": turns[w["ti"]][2] if w["ti"] >= 0 else 0,
                           "start": w["start"], "end": w["end"], "words": [w]})

    # 민감도에 따라 아주 짧은 조각만 걷어낸다. high면 이 단계를 건너뛴다.
    if min_sec > 0 or min_words > 0:
        changed = True
        while changed and len(groups) > 1:
            changed = False
            for i, g in enumerate(groups):
                if not ((g["end"] - g["start"]) < min_sec and len(g["words"]) < min_words):
                    continue
                prev = groups[i - 1] if i > 0 else None
                nxt = groups[i + 1] if i + 1 < len(groups) else None
                cand = [c for c in (prev, nxt) if c and len(c["words"]) > len(g["words"])]
                if not cand:
                    continue
                tgt = min(cand, key=lambda c: (g["start"] - c["end"]) if c is prev
                          else (c["start"] - g["end"]))
                tgt["words"] += g["words"]
                tgt["start"] = min(tgt["start"], g["start"])
                tgt["end"] = max(tgt["end"], g["end"])
                tgt["words"].sort(key=lambda w: w["start"])
                groups.pop(i)
                changed = True
                break

    # 같은 화자의 잇단 턴이 바짝 붙어 있으면 한 발화로 본다
    out = []
    for g in groups:
        text = " ".join(w["text"] for w in g["words"]).strip()
        if not text:
            continue
        if (out and out[-1]["spk"] == g["spk"]
                and g["start"] - out[-1]["end"] <= MERGE_GAP_SEC):
            out[-1]["text"] = (out[-1]["text"] + " " + text).strip()
            out[-1]["end"] = g["end"]
        else:
            out.append({"start": g["start"], "end": g["end"],
                        "spk": g["spk"], "text": text})
    return out


# ─────────────────────────────────────────────────────────────
# 고유명사 교정
# ─────────────────────────────────────────────────────────────

_CHO = list("ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ")
_JUNG = list("ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ")
_JONG = [""] + list("ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ")


def jamo(text: str) -> str:
    """한글을 자모로 푼다. 음절 단위보다 발음 거리에 가깝다."""
    out = []
    for ch in text:
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            out.append(_CHO[code // 588])
            out.append(_JUNG[(code % 588) // 28])
            out.append(_JONG[code % 28])
        else:
            out.append(ch)
    return "".join(out)


def edit(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def max_distance(term: str) -> int:
    """
    허용 편집거리. 짧은 말일수록 좁게 잡는다.
    두 음절짜리는 우연히 닮은 말이 많아 거의 정확히 일치할 때만 고친다.
    """
    n = len(jamo(term))
    if n <= 6:          # 두 음절 이하
        return 1
    return 3            # 세 음절 이상 — 이름이 대부분 여기 든다


# 교정한 조각 뒤에 남아도 되는 꼬리. 여기 없으면 어절을 깨뜨린 것으로 본다.
# "이승님|입니다" 는 되고 "이승입|니다" 는 안 된다.
TAILS = {
    "", "은", "는", "이", "가", "을", "를", "와", "과", "도", "만", "의",
    "에", "에서", "에게", "에겐", "께", "께서", "로", "으로", "랑", "이랑",
    "라고", "이라고", "라는", "이라는", "라며", "이라며", "한테", "보다",
    "처럼", "같이", "마다", "부터", "까지", "조차", "밖에", "뿐", "나", "이나",
    "님", "씨", "님이", "님은", "님을", "님의", "님과", "님도", "님께", "님께서",
    "입니다", "이다", "예요", "이에요", "이라", "이야", "야", "요", "이요",
    "코치", "코치님", "대표", "대표님", "선생", "선생님", "이었다", "였다",
}

# 긴 꼬리를 먼저 본다. "이대포님이라고" 에서 "라고" 가 먼저 걸리면
# 머리가 "이대포님이" 가 되어 거리가 벌어진다. "이라고" 가 먼저여야 한다.
_TAILS_LONG_FIRST = sorted(TAILS, key=len, reverse=True)


def fix_terms(rows: list, terms: list) -> list:
    """
    사용자가 지정한 이름·용어에 대해서만 음가가 가까운 오인식을 되돌린다.
    지정하지 않은 말은 건드리지 않는다. 바꾼 내역은 전부 대장에 남긴다.

    첫 글자가 같아야 한다는 조건을 둔다.
    "김미영"이 "황미혜"로 바뀌는 사고를 막는 가장 값싼 장치다.
    """
    terms = [t.strip() for t in terms if len(t.strip()) >= 2]
    if not terms:
        return []

    hits, jt = [], {t: jamo(t) for t in terms}     # log() 를 가리지 않게 한다
    for r in rows:
        parts, hit = r["text"].split(), False
        for i, w in enumerate(parts):
            core = "".join(c for c in w if c.isalnum())     # 문장부호 제외
            if not core:
                continue
            for t in terms:
                if t in core:                # 이미 맞게 적혔다. 이 가드가 없으면
                    break                    # "이승은" 이 "이승은은" 이 된다
                best = None
                # 꼬리를 먼저 정하고 머리를 맞춘다.
                # "입니다" 가 꼬리 목록에 있으니 그 앞이 이름 자리다.
                # 머리를 먼저 자르면 "이승|입니다" 를 못 찾아 보류로 빠진다.
                for tail in _TAILS_LONG_FIRST:
                    if tail and not core.endswith(tail):
                        continue
                    head = core[:len(core) - len(tail)] if tail else core
                    if len(head) < 2 or head[0] != t[0]:
                        continue
                    d = edit(jamo(head), jt[t])
                    if d and d <= max_distance(t) and d / len(jt[t]) <= 0.45:
                        if best is None or d < best[0]:
                            best = (d, head, tail)
                if best:
                    d, head, tail = best
                    parts[i] = w.replace(head, t, 1)
                    hits.append({"time": r["start"], "was": head, "now": t,
                                 "dist": d, "of": len(jt[t]), "hold": False})
                    hit = True
                    break

                # 꼬리가 성립하는 자리가 없다. 머리만 닮았으면 본문은 그대로 두고
                # 대장에만 올린다. 마스터 프롬프트 §4-2 — 단정할 수 없으면 고치지 않는다.
                n = len(t)
                for ln in (n, n + 1, n - 1):
                    if ln < 2 or ln > len(core) or core[:ln][0] != t[0]:
                        continue
                    d = edit(jamo(core[:ln]), jt[t])
                    if d and d <= max_distance(t) and d / len(jt[t]) <= 0.45:
                        hits.append({"time": r["start"], "was": core, "now": t,
                                     "dist": d, "of": len(jt[t]), "hold": True})
                        break
                else:
                    continue
                break
        if hit:
            r["text"] = " ".join(parts)
    return hits


def write_ledger(entries: list, stem: str, off: float, outdir: str = None) -> str:
    """
    교정 대장. 정본화 규칙 부록 A 형식이다.
    무엇을 왜 바꿨는지 남기지 않으면 보정이 또 다른 왜곡이 된다.
    """
    path = os.path.join(outdir or OUTDIR, f"{stem}_corrections.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 교정 대장 — {stem}\n\n")
        f.write("지정한 이름·용어와 음가가 가까운 오인식을 되돌린 내역이다.\n")
        f.write("거리는 자모 단위 편집거리다. 잘못 바뀐 것이 있으면 알려달라.\n\n")
        f.write("| # | 시각 | 원문 | 후보 | 자모 거리 | 처리 |\n|---|---|---|---|---|---|\n")
        for i, e in enumerate(entries, 1):
            t = max(0.0, e["time"] - off)
            mark = "보류" if e.get("hold") else "반영"
            f.write(f"| {i} | {int(t)//60:02d}:{int(t)%60:02d} | {e['was']} | "
                    f"{e['now']} | {e['dist']}/{e['of']} | {mark} |\n")
        n_fix = sum(1 for e in entries if not e.get("hold"))
        f.write(f"\n반영 {n_fix}건 · 보류 {len(entries) - n_fix}건.\n\n")
        f.write("보류는 바꾸면 어절이 깨지는 경우다. 본문을 그대로 두었으니 "
                "직접 확인해달라.\n")
    return path


def write_outputs(rows: list, stem: str, want: dict, diarized: bool,
                  outdir: str = None) -> list:
    """최종 파일을 쓴다. 화자 분리 후에는 이 함수로 다시 쓴다."""
    d = outdir or OUTDIR
    paths, made = {
        "plain": os.path.join(d, f"{stem}.txt"),
        "timed": os.path.join(d, f"{stem}_timed.txt"),
        "srt": os.path.join(d, f"{stem}.srt"),
        "canon": os.path.join(d, f"{stem}_canon.md"),
    }, []

    def spk(r):
        return f"화자{r['spk']}" if diarized and r.get("spk") else ""

    def tag(r, suffix=""):
        v = spk(r)
        return (v + suffix) if v else ""

    for kind in ("plain", "timed", "srt", "canon"):
        if not want.get(kind):
            continue
        with open(paths[kind], "w", encoding="utf-8") as f:
            if kind == "plain":
                for r in rows:
                    f.write(tag(r, ": ") + r["text"] + "\n")
            elif kind == "timed":
                for r in rows:
                    v = spk(r)
                    f.write(f"[{hms(r['start'])}]" + (f" {v}" if v else "")
                            + f"\t{r['text']}\n")
            elif kind == "srt":
                for i, r in enumerate(rows, 1):
                    end = rows[i]["start"] if i < len(rows) else r["start"] + 4
                    f.write(f"{i}\n{srt_time(r['start'])} --> {srt_time(end)}\n"
                            f"{tag(r, ': ')}{r['text']}\n\n")
            elif kind == "canon":
                off = rows[0]["start"] if rows else 0.0      # 첫 발화를 00:00으로
                if not diarized:
                    f.write("> 화자 분리를 하지 않았다. 화자 표기가 없다.\n\n")
                for r in rows:
                    t = max(0.0, r["start"] - off)
                    v = spk(r)
                    f.write(f"**[{int(t)//60:02d}:{int(t)%60:02d}]"
                            + (f" {v}" if v else "") + "**\n"
                            + r["text"] + "\n\n")
        made.append({"name": os.path.basename(paths[kind]), "path": paths[kind]})
    return made


def transcribe(path: str, opt: dict, outdir: str = None) -> None:
    """대기열 실행기에서 부른다. 1단계 전사, 2단계 화자 분리, 3단계 재작성."""
    stem = os.path.splitext(os.path.basename(path))[0]
    outdir = os.path.abspath(outdir or OUTDIR)
    os.makedirs(outdir, exist_ok=True)

    def upd(**kw):
        with LOCK:
            JOB.update(kw)

    def fail(msg: str, state: str = "error", hint: str = ""):
        """
        실패를 알린다. 무엇이 · 왜 · 무엇을 하면 되는지 세 조각이다.

        어느 파일이 실패했는지 밝히지 않으면 대기열에서 무엇이 죽었는지 알 수 없다.
        기록에도 남긴다 — 무창 실행에서는 그것만이 단서다.
        """
        finish_job(state, f"{os.path.basename(path)} — {msg}", hint)
        log(f"   실패 — {msg}" + (f" / {hint}" if hint else ""))

    upd(state="loading", file=path, stem=stem, phase="모델을 준비하고 있다", message="",
        processed=0.0, segments=0, chars=0, tail=[], outputs=[], speakers=0, dia_pct=0.0,
        corrections=0, holds=0, outdir=outdir, cached=False,
        duration=0.0, hid="",          # 앞 작업 값이 남으면 막대와 [다시 시도] 가 엉뚱해진다
        device="", device_note="",     # 기기와 전환 사유도 앞 작업 것을 물려받지 않는다
        started=time.time(), elapsed=0.0, speed=0.0, eta=0.0)

    fmt = ",".join(k for k in ("plain", "timed", "srt", "canon") if opt["formats"].get(k))
    log(f"\n▶ 시작  {os.path.basename(path)}")
    log(f"   설정  {opt['model']} · {opt['device']} · {opt['compute']} · beam {opt['beam']} · "
        f"무음 {opt['silence']}ms · vad {opt['vad']} · 재시도 {opt['fallback']} · "
        f"말버릇 {opt['prompt']} · 교정 {opt.get('fixterms')}")
    log(f"         화자분리 {opt.get('diarize')} · 화자 {opt.get('nspk')} · "
        f"민감도 {opt.get('sens')} · 형식 {fmt}")
    log(f"         이름·용어 {opt.get('hotwords') or '없음'}")

    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        fail("받아쓰기 엔진이 없다.",
             hint="setup.py 를 다시 더블클릭하면 설치된다.")
        return

    # 기기와 무관한 준비는 한 번만 한다. CPU 로 다시 시작해도 되풀이하지 않는다.
    diarize = bool(opt.get("diarize"))
    if diarize:
        chk = dia_check()
        if not chk["ok"]:
            diarize = False
            upd(message="화자 분리를 못 한다 — " + chk["why"] + " 받아쓰기는 그대로 진행한다.")
            log("   화자 분리 불가 — " + chk["why"])

    kwargs = dict(
        beam_size=opt["beam"],
        condition_on_previous_text=False,   # 침묵 구간 반복 환각을 막는다
        word_timestamps=diarize,            # 화자를 어절 단위로 붙이려면 필요하다
        vad_filter=opt["vad"],
    )
    if opt["lang"] != "auto":
        kwargs["language"] = opt["lang"]
    if opt["vad"]:
        kwargs["vad_parameters"] = dict(min_silence_duration_ms=opt["silence"],
                                        speech_pad_ms=200)
    if opt["fallback"]:
        kwargs["temperature"] = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    if opt["prompt"]:
        kwargs["initial_prompt"] = "어, 음, 그, 이제 같은 말버릇을 그대로 적는다."

    hot = ", ".join(w.strip() for w in opt.get("hotwords", "").split(",") if w.strip())

    # 구간마다 즉시 적는 임시 파일. 2시간 50분에 죽어도 거기까지 남는다.
    tmp = os.path.join(outdir, f"{stem}.txt")

    class PassError(Exception):
        """한 판이 실패했다. 어느 단계였는지를 들고 다닌다."""

        def __init__(self, stage, err):
            super().__init__(str(err))
            self.stage, self.err = stage, err

    def one_pass(device: str, compute: str):
        """모델을 올리고 구간을 훑는다. (rows, dur, stopped) 를 돌려준다."""
        try:
            t_load = time.time()
            model, cached, dev, comp = get_whisper(opt["model"], device, compute)
            upd(cached=cached, device=dev)
            log(f"   모델 {'재사용' if cached else '적재'} {time.time() - t_load:.1f}초 "
                f"· 기기 {dev} · {comp}")
        except Exception as e:
            raise PassError("model", e)

        try:
            if hot:
                try:
                    segments, info = model.transcribe(path, hotwords=hot, **kwargs)
                except TypeError:
                    # 옛 판은 hotwords 를 모른다. 프롬프트로 대신한다.
                    # kwargs 를 그대로 두고 사본을 고친다 — 다시 시작할 때 섞이면 안 된다.
                    kw = dict(kwargs)
                    kw["initial_prompt"] = ((kw.get("initial_prompt", "") + " ").strip()
                                            + f" 등장하는 이름과 용어: {hot}.")
                    segments, info = model.transcribe(path, **kw)
            else:
                segments, info = model.transcribe(path, **kwargs)
        except Exception as e:
            raise PassError("open", e)

        dur = float(getattr(info, "duration", 0.0)) or probe_duration(path)
        upd(state="running", duration=dur,
            phase="받아쓰는 중" + (" · 끝나면 화자를 나눈다" if diarize else ""))

        rows, n, chars, stopped = [], 0, 0, False
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for seg in segments:
                    if CANCEL.is_set():
                        stopped = True
                        break
                    text = (seg.text or "").strip()
                    if not text:
                        continue
                    n += 1
                    chars += len(text)
                    rows.append({
                        "start": float(seg.start), "end": float(seg.end), "text": text,
                        "words": [{"start": float(w.start), "end": float(w.end),
                                   "text": w.word} for w in (seg.words or [])]
                        if getattr(seg, "words", None) else [],
                    })
                    f.write(text + "\n")
                    f.flush()

                    el = time.time() - JOB["started"]
                    sp = seg.end / el if el > 0 else 0
                    with LOCK:
                        JOB.update(processed=seg.end, segments=n, chars=chars, elapsed=el,
                                   speed=sp,
                                   eta=max(0.0, (dur - seg.end) / sp)
                                   if (sp > 0 and dur > 0) else 0.0)
                        JOB["tail"] = (JOB["tail"]
                                       + [{"t": hms(seg.start), "s": 0, "x": text}])[-14:]
        except Exception as e:
            raise PassError("loop", e)
        return rows, dur, stopped

    # ── 판 돌리기. CUDA 가 안 되면 CPU 로 처음부터 다시 한 번 ──
    #
    # 규칙 넷을 못 박는다.
    #   1회만    재시작은 한 번. 또 실패하면 오류
    #   auto만   cuda 를 명시로 고른 경우에는 내려가지 않는다
    #   혼합금지  GPU 절반 + CPU 절반 전사문이 나오면 안 된다. 처음부터 다시
    #   부분폐기  다시 시작하기 전에 임시 파일을 비운다. 이것은 "중단" 이 아니라
    #            "다시 하는 것" 이므로 _부분 파일을 만들지 않는다 (§6-3 과 구분)
    want = (opt.get("device") or "auto").strip()
    dev, comp = want, COMPUTE          # 화면에서 고르지 않는다. 옛 값은 무시한다
    rows, dur, stopped = [], 0.0, False
    for attempt in (1, 2):
        try:
            rows, dur, stopped = one_pass(dev, comp)
            with LOCK:
                if JOB.get("device") == "cuda":
                    note_gpu(True)                # 실제로 끝냈으니 쓸 수 있는 것이 확실하다
            break
        except PassError as pe:
            cuda = is_cuda_error(pe.err)
            if attempt == 1 and want == "auto" and cuda:
                note = f"GPU를 쓸 수 없어 CPU로 다시 시작했다. {str(pe.err)[:110]}"
                note_gpu(False, str(pe.err))      # 배지가 "GPU 있으나 사용 불가" 로 바뀐다
                log("   " + note)
                try:
                    with open(tmp, "w", encoding="utf-8"):
                        pass                    # 반쪽 전사문이 남지 않게 비운다
                except OSError:
                    pass
                upd(processed=0.0, segments=0, chars=0, tail=[], duration=0.0,
                    speed=0.0, eta=0.0, elapsed=0.0, started=time.time(),
                    device_note=note, phase="CPU로 다시 시작한다")
                dev, comp = "cpu", COMPUTE
                continue
            if cuda:
                note_gpu(False, str(pe.err))
            gpu_hint = "GPU를 쓸 수 없다. 설정에서 기기를 cpu 로 바꾸거나 진단 화면을 확인해달라."
            msg, hint = {
                "model": (f"모델을 불러오지 못했다. {pe.err}",
                          gpu_hint if cuda else
                          "모델을 처음 받는 중이라면 인터넷 연결을 확인해달라."),
                "open": (f"음원을 읽지 못했다. {pe.err}",
                         "파일이 손상됐거나 지원하지 않는 형식일 수 있다. 다른 파일로 확인해달라."),
                "loop": (f"전사 중 멈췄다. {pe.err}", gpu_hint if cuda else ""),
            }[pe.stage]
            fail(msg, hint=hint)
            return

    if not rows:
        upd(phase="")
        fail("받아쓴 내용이 없다.", "cancelled" if stopped else "error",
             hint=("" if stopped else
                   "음원에 말소리가 없거나 무음 기준이 너무 높다. 자세히에서 무음 기준을 낮춰본다."))
        return

    # ── 2단계 · 화자 분리 ──
    turns = []
    if diarize and not stopped:
        turns = run_diarization(path, int(opt.get("nspk", 0)), upd)

    # ── 3단계 · 재작성 ──
    upd(state="merging", phase="파일을 정리하고 있다")
    final = apply_speakers(rows, turns, opt.get("sens", "high")) if turns else \
        [{"start": r["start"], "end": r["end"], "spk": 0, "text": r["text"]} for r in rows]

    fixlog = []                              # log() 를 가리지 않도록 이름을 달리한다
    if opt.get("fixterms") and hot:
        fixlog = fix_terms(final, hot.split(","))
    made = write_outputs(final, stem, opt["formats"], bool(turns), outdir)
    if fixlog:
        off = final[0]["start"] if final else 0.0
        lp = write_ledger(fixlog, stem, off, outdir)
        made.append({"name": os.path.basename(lp), "path": lp})

    with LOCK:
        JOB["tail"] = [{"t": hms(r["start"]), "s": r.get("spk", 0), "x": r["text"]}
                       for r in final[-14:]]
    n_fix = sum(1 for e in fixlog if not e.get("hold"))
    n_hold = len(fixlog) - n_fix
    upd(corrections=n_fix, holds=n_hold)
    if fixlog:
        upd(message=(JOB["message"] + "  " if JOB["message"] else "")
            + (f"이름·용어 {n_fix}건 반영, {n_hold}건 보류. "
               f"{stem}_corrections.md 에서 확인해달라."))
    upd(outputs=made)
    with LOCK:
        note = JOB["message"]
    finish_job("cancelled" if stopped else "done",
               note or ("중단했다. 여기까지는 저장돼 있다." if stopped
                        else ("끝났다." if turns or not diarize
                              else "끝났다. 화자 분리는 하지 못했다.")))

    with LOCK:
        el, sp = JOB["elapsed"], JOB["speed"]
        st, msg = JOB["state"], JOB["message"]
    log(f"■ {'중단' if stopped else '완료'}  {stem}  ·  상태 {st}")
    log(f"   길이 {hms(dur)} · 소요 {hms(el)} · 배속 {sp:.2f}× · "
        f"화자 {len(set(r.get('spk', 0) for r in final)) if turns else 0}명 · "
        f"발화 {len(final)}건 · 글자 {sum(len(r['text']) for r in final)}자 · "
        f"교정 {n_fix}건 반영 · {n_hold}건 보류")
    for m in made:
        log(f"   저장  {m['path']}")
    if msg:
        log(f"   알림  {msg}")


# ─────────────────────────────────────────────────────────────
# 대기열 실행기
#
# 실행기는 하나다. 동시 실행 금지가 플래그 검사가 아니라 구조로 보장된다.
# large-v3 와 pyannote 가 함께 뜨면 수 GB 를 쓴다.
#
# 상태 전이는 언제나 "파일 먼저, 실행 나중" 이다.
#   저장 전에 죽으면 → waiting 으로 남아 다음에 다시 시도된다
#   저장 후 실행 전에 죽으면 → running 으로 남고 다음 기동에서 interrupted 가 된다
# 순서를 뒤집으면 같은 항목이 두 번 도는 길이 열린다.
# ─────────────────────────────────────────────────────────────

STOP_ALL = threading.Event()               # 전체 중지. 현재 항목은 끝낸다


def take_next() -> dict:
    """대기 중인 첫 항목을 running 으로 바꾸고 저장한 뒤 돌려준다."""
    with STATE_LOCK:
        for it in QUEUE["items"]:
            if it.get("state") == "waiting":
                it["state"] = "running"
                it["started"] = now_iso()
                save_queue()               # 파일 먼저
                return dict(it)            # 실행 나중
    return None


def finish_item(item: dict) -> None:
    """끝난 항목을 큐에서 빼고 이력으로 옮긴다. 대기열 화면이 곧 남은 일이 된다."""
    with LOCK:
        j = dict(JOB)
    rec = {
        "id": new_id("h"), "qid": item["id"],
        "name": os.path.basename(item["path"]), "path": item["path"],
        "outdir": item.get("outdir", ""),
        "duration": round(j.get("duration", 0.0), 2),
        "started": item.get("started", ""), "finished": now_iso(),
        "elapsed": round(j.get("elapsed", 0.0), 2),
        "speed": round(j.get("speed", 0.0), 3),
        "speakers": j.get("speakers", 0), "segments": j.get("segments", 0),
        "chars": j.get("chars", 0),
        "corrections": j.get("corrections", 0), "holds": j.get("holds", 0),
        "cached": bool(j.get("cached")),
        # 나중에 배속을 견줄 때 근거가 된다. GPU 22× 와 CPU 2× 를 같은 표에 놓으면 안 된다.
        "device": j.get("device", ""), "device_note": j.get("device_note", ""),
        "settings": item.get("settings", {}),
        "outputs": [o["path"] for o in j.get("outputs", [])],
        "state": j.get("state", "done"), "message": j.get("message", ""),
    }
    with STATE_LOCK:
        QUEUE["items"] = [x for x in QUEUE["items"] if x["id"] != item["id"]]
        HISTORY["items"] = ([rec] + HISTORY["items"])[:HISTORY_MAX]
        save_queue()
        save_history()
    # 화면이 "다시 시도" 를 이력 경로로 걸 수 있게 방금 만든 기록을 가리켜 둔다.
    with LOCK:
        JOB["hid"] = rec["id"]


def queue_loop() -> None:
    """기동 시 데몬 스레드 하나로 돈다."""
    while True:
        item = None if STOP_ALL.is_set() else take_next()
        if item is None:
            # 여기서 JOB 을 건드리지 않는다. 종료 상태를 푸는 것은 둘뿐이다 —
            # 사람이 [닫기] 를 누르거나, 다음 항목이 transcribe() 로 덮어쓰거나.
            # 실행기가 0.4초마다 지우면 사람이 읽기도 전에 알림이 사라진다.
            if _CACHE_USED[0] and time.time() - _CACHE_USED[0] > CACHE_IDLE_SEC:
                _CACHE_USED[0] = 0.0
                release_cache(f"{CACHE_IDLE_SEC // 60}분 동안 할 일이 없었다.")
            time.sleep(0.4)
            continue

        CANCEL.clear()
        with LOCK:
            JOB["qid"] = item["id"]
        try:
            transcribe(item["path"], item["settings"], item.get("outdir"))
        except Exception as e:
            # 한 항목이 터져도 대기열은 멈추지 않는다.
            import traceback
            log("   실행기가 예외를 받았다 —\n" + traceback.format_exc())
            finish_job("error",
                       f"{os.path.basename(item['path'])} — 예상 못 한 오류. "
                       f"{type(e).__name__}: {e}",
                       "진단 화면의 기록에 자세한 내용이 남는다.")
        finish_item(item)


def enqueue(paths: list, settings: dict, outdir: str) -> dict:
    """여러 건을 한 번에 담는다. 담기는 순간 실행기가 집어간다."""
    settings = norm_opt(settings)      # 옛 화면·옛 이력에서 온 값을 여기서 맞춘다
    chk = check_outdir(outdir)
    if not chk["ok"]:
        return {"error": chk["why"]}

    files = []
    for p in paths:
        p = os.path.abspath((p or "").strip().strip('"'))
        if not os.path.isfile(p):
            return {"error": f"파일을 찾지 못했다: {p}"}
        files.append(p)
    if not files:
        return {"error": "담을 파일을 고르지 않았다."}

    # 같은 이름이 같은 폴더로 가면 나중 것이 앞의 것을 덮어쓴다. 막지는 않고 알린다.
    warn = []
    with STATE_LOCK:
        pending = {(x.get("outdir", ""), os.path.splitext(os.path.basename(x["path"]))[0])
                   for x in QUEUE["items"]}
        seen = set()
        for p in files:
            stem = os.path.splitext(os.path.basename(p))[0]
            key = (chk["path"], stem)
            if key in pending or key in seen:
                warn.append(f"{stem} — 같은 이름이 대기열에 이미 있다")
            elif any(os.path.exists(os.path.join(chk["path"], stem + e))
                     for e in (".txt", "_timed.txt", ".srt", "_canon.md")):
                warn.append(f"{stem} — 같은 이름의 결과가 이미 있다")
            seen.add(key)

        added = []
        for p in files:
            it = {"id": new_id("q"), "path": p, "name": os.path.basename(p),
                  "outdir": chk["path"], "state": "waiting",
                  "settings": settings, "added": now_iso(), "message": ""}
            QUEUE["items"].append(it)
            added.append(it["id"])
        SETTINGS["last"] = settings
        save_queue()
        save_settings()

    remember_outdir(chk["path"])
    log(f"▷ 대기열에 {len(added)}건을 담았다 → {chk['path']}")
    return {"ok": True, "ids": added, "warn": warn}


def move_item(qid: str, delta: int) -> dict:
    """대기 중인 항목만 옮긴다. 도는 항목은 자리를 지킨다."""
    with STATE_LOCK:
        items = QUEUE["items"]
        idx = next((i for i, x in enumerate(items) if x["id"] == qid), -1)
        if idx < 0:
            return {"error": "항목을 찾지 못했다."}
        if items[idx].get("state") == "running":
            return {"error": "지금 도는 항목은 옮길 수 없다."}
        tgt = idx + delta
        if tgt < 0 or tgt >= len(items) or items[tgt].get("state") == "running":
            return {"ok": True}
        items[idx], items[tgt] = items[tgt], items[idx]
        save_queue()
    return {"ok": True}


def remove_item(qid: str) -> dict:
    with STATE_LOCK:
        it = next((x for x in QUEUE["items"] if x["id"] == qid), None)
        if it is None:
            return {"error": "항목을 찾지 못했다."}
        if it.get("state") == "running":
            return {"error": "지금 도는 항목이다. 먼저 중단해달라."}
        QUEUE["items"] = [x for x in QUEUE["items"] if x["id"] != qid]
        save_queue()
    return {"ok": True}


def resume_item(qid: str, mode: str) -> dict:
    """중단된 항목을 다시 돌린다. 남아 있는 부분 산출을 보존할지 사람이 고른다."""
    with STATE_LOCK:
        it = next((x for x in QUEUE["items"] if x["id"] == qid), None)
        if it is None:
            return {"error": "항목을 찾지 못했다."}
        if it.get("state") != "interrupted":
            return {"error": "중단된 항목이 아니다."}
        stem = os.path.splitext(os.path.basename(it["path"]))[0]
        d = it.get("outdir") or OUTDIR
        kept = []
        if mode == "keep":
            for ext in (".txt", "_timed.txt", ".srt", "_canon.md", "_corrections.md"):
                src = os.path.join(d, stem + ext)
                if os.path.exists(src):
                    dst = os.path.join(d, stem + "_부분" + ext)
                    try:
                        os.replace(src, dst)
                        kept.append(os.path.basename(dst))
                    except OSError as e:
                        return {"error": f"부분 산출을 옮기지 못했다. {e}"}
        it["state"] = "waiting"
        it["message"] = ""
        save_queue()
    if kept:
        log(f"   부분 산출을 보존했다 — {', '.join(kept)}")
    return {"ok": True, "kept": kept}


# ─────────────────────────────────────────────────────────────
# 화면
# ─────────────────────────────────────────────────────────────

PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>받아쓰기 v__VER__</title>
<style>
:root{
  --ground:#EDF1F0; --surface:#FFFFFF; --ink:#16211F; --muted:#5E6E6B;
  --rule:#C9D4D1; --signal:#0E7C86; --signal-soft:#D3E6E7; --warn:#B4531F;
  --mono:"Consolas","D2Coding",ui-monospace,"Menlo",monospace;
  --sans:"Segoe UI","Pretendard","Apple SD Gothic Neo",system-ui,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
     font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:880px;margin:0 auto;padding:32px 20px 64px}

header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
       padding-bottom:14px;border-bottom:2px solid var(--ink)}
h1{margin:0;font-size:26px;font-weight:700;letter-spacing:-.02em}
.badge{font-family:var(--mono);font-size:11px;letter-spacing:.06em;
       color:var(--signal);border:1px solid var(--signal);border-radius:2px;
       padding:2px 7px;text-transform:uppercase}
.ver{margin-left:auto;font-family:var(--mono);font-size:12px;color:#fff;
     background:var(--ink);padding:3px 9px;border-radius:2px;letter-spacing:.02em}
button.quit{padding:4px 12px;font-size:12px;font-weight:600;background:transparent;
            color:var(--warn);border:1px solid var(--warn);align-self:center}
button.quit:hover{background:#F7ECE6}
.sub{color:var(--muted);font-size:13px;margin:10px 0 26px}

section{background:var(--surface);border:1px solid var(--rule);border-radius:3px;
        padding:18px 20px;margin-bottom:16px}
.lab{font-family:var(--mono);font-size:11px;letter-spacing:.1em;color:var(--muted);
     text-transform:uppercase;margin:0 0 12px}
.head{display:flex;align-items:center;justify-content:space-between;gap:12px}
.head .lab{margin-bottom:0}

ul.files{list-style:none;margin:12px 0 0;padding:0;max-height:220px;overflow:auto}
ul.files li{display:flex;align-items:center;gap:10px;padding:6px 8px;
            border-radius:2px;border:1px solid transparent}
ul.files li:hover{background:var(--signal-soft)}
ul.files li.on{border-color:var(--signal);background:var(--signal-soft)}
ul.files .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
ul.files .dur,ul.files .sz{font-family:var(--mono);font-size:13px;color:var(--muted)}
.empty{color:var(--muted);font-size:13px;padding:10px 0;line-height:1.7}
.empty code{font-family:var(--mono);background:var(--ground);padding:2px 5px;border-radius:2px}
.dir{font-family:var(--mono);font-size:12px;color:var(--signal);margin:8px 0 0;
     word-break:break-all}
button.mini{padding:4px 11px;font-size:12px;font-weight:500;background:transparent;
            color:var(--muted);border:1px solid var(--rule);border-radius:2px}
button.mini:hover{background:var(--ground);color:var(--ink);border-color:var(--muted)}
.tag{font-family:var(--mono);font-size:10px;letter-spacing:.05em;color:var(--muted);
     border:1px solid var(--rule);border-radius:2px;padding:1px 5px;flex:none}

input[type=text],select{font:inherit;color:var(--ink);background:var(--surface);
  border:1px solid var(--rule);border-radius:2px;padding:7px 9px}
input[type=text]{width:100%;font-family:var(--mono);font-size:13px;margin-top:10px}
input[type=text]:focus,select:focus,button:focus-visible{outline:2px solid var(--signal);outline-offset:1px}

.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:16px}
.fld{display:flex;flex-direction:column;gap:5px}
.fld span{font-size:12px;color:var(--muted)}
.fld.hot{margin-top:16px}
.fld.hot i{font-style:normal;opacity:.75}
.fld.hot input{margin-top:4px}
.checks{display:flex;flex-wrap:wrap;gap:16px;margin-top:16px;font-size:13px}
.checks label{display:flex;align-items:center;gap:6px;cursor:pointer}

details{margin-top:16px;border-top:1px dashed var(--rule);padding-top:12px}
summary{cursor:pointer;font-size:13px;color:var(--signal);font-weight:600;
        list-style:none;user-select:none}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸ ";font-family:var(--mono)}
details[open] summary::before{content:"▾ "}

.dia{margin-top:16px;padding:13px 15px;background:var(--ground);
     border:1px solid var(--rule);border-radius:2px;
     display:flex;align-items:center;gap:20px;flex-wrap:wrap}
.dia .sw{display:flex;align-items:center;gap:7px;cursor:pointer;font-size:14px}
.dia .fld.inline{flex-direction:row;align-items:center;gap:8px}
.dia .hint{flex-basis:100%;margin:0;font-size:12px;color:var(--muted);line-height:1.6}
.dia .hint.bad{color:var(--warn);font-weight:600}
.est{margin-top:14px;padding-top:12px;border-top:1px dashed var(--rule);
     font-size:13px;color:var(--muted)}
.est b{font-family:var(--mono);color:var(--ink);font-weight:600}

button{font:inherit;font-weight:600;border-radius:2px;cursor:pointer;
       padding:11px 26px;border:1px solid var(--ink);background:var(--ink);color:#fff}
button:hover{background:#0b1614}
button[disabled]{opacity:.4;cursor:not-allowed}
button.ghost{background:transparent;color:var(--warn);border-color:var(--warn);
             padding:8px 18px;font-size:13px}
button.ghost:hover{background:#F7ECE6}
button.ghost.on{background:var(--warn);color:#fff}

/* 신호판 — 이 앱의 주인공 */
.meter{display:flex;align-items:flex-end;gap:22px;flex-wrap:wrap;margin-bottom:14px}
.rate{font-family:var(--mono);font-size:52px;font-weight:700;line-height:.95;
      letter-spacing:-.03em;color:var(--signal)}
.rate small{font-size:16px;font-weight:400;color:var(--muted);margin-left:6px;
            letter-spacing:0}
.stats{display:flex;gap:22px;flex-wrap:wrap;margin-left:auto}
.stat{text-align:right}
.stat b{display:block;font-family:var(--mono);font-size:17px;font-weight:600}
.stat span{font-size:11px;color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
.nowfile{margin:0 0 8px;font-size:14px;font-weight:600;overflow:hidden;
         text-overflow:ellipsis;white-space:nowrap}

/* 시간 눈금이 있는 진행 막대 */
.track{position:relative;height:34px;background:var(--ground);
       border:1px solid var(--rule);border-radius:2px;overflow:hidden}
.fill{position:absolute;inset:0 auto 0 0;width:0;background:var(--signal);
      transition:width .4s linear}
.ticks{position:absolute;inset:0;pointer-events:none}
.ticks i{position:absolute;top:0;bottom:0;width:1px;background:var(--rule);opacity:.9}
.ticks i.hour{background:var(--ink);opacity:.35}
.ticks b{position:absolute;bottom:3px;font-family:var(--mono);font-size:10px;
         color:var(--muted);transform:translateX(4px);font-weight:400}
.clock{display:flex;justify-content:space-between;margin-top:7px;
       font-family:var(--mono);font-size:13px;color:var(--muted)}
.clock em{font-style:normal;color:var(--ink);font-weight:600}

.phase{margin:0 0 9px;font-size:13px;color:var(--signal);font-weight:600}
.tail .spk{font-family:var(--mono);font-size:11px;color:#fff;background:var(--signal);
           border-radius:2px;padding:1px 5px;flex:none;align-self:flex-start;margin-top:2px}
.tail .spk.s2{background:#7A5C2E} .tail .spk.s3{background:#4A5F8A}
.tail{margin-top:18px;border-top:1px solid var(--rule);padding-top:14px;
      max-height:220px;overflow:auto}
.tail p{margin:0 0 5px;display:flex;gap:11px;font-size:14px}
.tail time{font-family:var(--mono);font-size:12px;color:var(--signal);
           flex:none;padding-top:2px}
.acts{margin:16px 0 0;display:flex;gap:8px}

/* 대기열·기록 목록 */
ul.list{list-style:none;margin:0;padding:0}
ul.list li{display:flex;align-items:center;gap:10px;padding:9px 8px;
           border-bottom:1px solid var(--rule);font-size:14px}
ul.list li:last-child{border-bottom:none}
ul.list .no{font-family:var(--mono);font-size:12px;color:var(--muted);
            min-width:18px;flex:none}
ul.list .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
ul.list .meta{font-family:var(--mono);font-size:12px;color:var(--muted);flex:none}
ul.list .btns{display:flex;gap:4px;flex:none}
ul.list .btns button{padding:3px 8px;font-size:12px;font-weight:500;
                     background:transparent;color:var(--muted);border-color:var(--rule)}
ul.list .btns button:hover{background:var(--ground);color:var(--ink)}
li.broken{background:#F9EFE9}
li.broken .ask{flex-basis:100%;font-size:13px;color:var(--warn);margin:0}

.note{font-size:13px;color:var(--muted);margin-top:12px}
.err{color:var(--warn);font-size:14px;margin-top:12px;padding:9px 12px;
     background:#F9EFE9;border-left:3px solid var(--warn);border-radius:2px;
     white-space:pre-line}
.nobar{margin:0;font-family:var(--mono);font-size:13px;color:var(--muted);
       padding:9px 0}
/* 기기 배지 — 기존 변수 안에서만 쓴다. 새 색을 만들지 않는다 */
button.dev{font-family:var(--mono);font-size:11px;letter-spacing:.04em;padding:2px 8px;
           border-radius:2px;background:transparent;font-weight:600;cursor:pointer;
           color:var(--muted);border:1px solid var(--rule);align-self:center}
button.dev.gpu{color:var(--signal);border-color:var(--signal)}
button.dev.broken{color:var(--warn);border-color:var(--warn);background:#F9EFE9}
button.dev:hover{background:var(--ground)}
.chip{font-family:var(--mono);font-size:12px;font-weight:600;letter-spacing:.04em;
      padding:2px 7px;border-radius:2px;margin-left:10px;vertical-align:6px;
      color:var(--signal);border:1px solid var(--signal)}
.chip.cpu{color:var(--muted);border-color:var(--rule)}
.chip.broken{color:var(--warn);border-color:var(--warn)}
.devnote{margin:6px 0 0;font-size:12px;color:var(--warn);font-family:var(--mono)}
ul.list li .bad{color:var(--warn);font-weight:600}
.err.ok{color:var(--ink);background:var(--signal-soft);border-left-color:var(--signal)}
.out{margin-top:14px;font-family:var(--mono);font-size:13px}
.out a{color:var(--signal)}
.hide{display:none}

/* 진단 */
.dg{font-size:13px}
.dg h3{font-size:13px;margin:16px 0 6px;font-family:var(--mono);
       letter-spacing:.08em;color:var(--muted);text-transform:uppercase}
.dg table{border-collapse:collapse;width:100%}
.dg td{padding:4px 8px 4px 0;border-bottom:1px solid var(--rule);vertical-align:top}
.dg td.k{color:var(--muted);width:150px;white-space:nowrap}
.dg td.v{font-family:var(--mono);word-break:break-all}
.dg .yes{color:var(--signal);font-weight:600}
.dg .no{color:var(--warn);font-weight:600}
.dg pre{background:var(--ground);border:1px solid var(--rule);border-radius:2px;
        padding:10px;max-height:280px;overflow:auto;font-family:var(--mono);
        font-size:11px;line-height:1.5;margin:0;white-space:pre-wrap;word-break:break-all}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style></head><body><div class="wrap">

<header>
  <h1>받아쓰기</h1>
  <span class="badge">이 PC에서만 처리 · 비용 없음</span>
  <button class="dev" id="devbadge" type="button" title="눌러서 진단 화면을 연다">확인 중</button>
  <span class="ver">v__VER__</span>
  <button class="quit" id="quit" type="button">종료</button>
</header>
<p class="sub">음성이 인터넷으로 나가지 않는다. 여러 파일을 걸어두고 자리를 떠도 된다.</p>

<section id="live" class="hide">
  <p class="lab">지금 하는 일</p>
  <p class="nowfile" id="nowfile"></p>
  <div class="meter">
    <div><div class="rate" id="rate">—<small>× 실시간</small></div></div>
    <span class="chip hide" id="jobdev"></span>
    <div class="stats">
      <div class="stat" id="s_left_box"><b id="s_left">—</b><span>남은 시간</span></div>
      <div class="stat"><b id="s_el">—</b><span id="s_el_lab">경과</span></div>
      <div class="stat"><b id="s_ch">0</b><span>글자</span></div>
    </div>
  </div>
  <p class="phase" id="phase"></p>
  <p class="devnote hide" id="devnote"></p>
  <div id="bar">
    <div class="track"><div class="fill" id="fill"></div><div class="ticks" id="ticks"></div></div>
    <div class="clock"><em id="c_now">0:00</em><span id="c_pct">0%</span><span id="c_end">—</span></div>
  </div>
  <p class="nobar hide" id="nobar">길이를 읽지 못했다. 진행 중이다.</p>
  <div class="tail" id="tail"></div>
  <p class="err hide" id="err"></p>
  <div class="out hide" id="out"></div>
  <p class="acts">
    <button class="ghost" id="cancel" type="button">현재 중단</button>
    <button class="ghost" id="stopall" type="button">전체 중지</button>
    <button class="ghost hide" id="retry" type="button">다시 시도</button>
    <button class="mini hide" id="dismiss" type="button">닫기</button>
  </p>
  <p class="note hide" id="stopnote"></p>
</section>

<section id="qsec" class="hide">
  <div class="head">
    <p class="lab">대기열 (<span id="qn">0</span>)</p>
    <button class="mini" id="qclear" type="button">대기열 비우기</button>
  </div>
  <ul class="list" id="qlist"></ul>
</section>

<section>
  <div class="head">
    <p class="lab">담기</p>
    <button class="mini" id="refresh" type="button">새로 고침</button>
  </div>
  <p class="dir" id="dir">—</p>
  <ul class="files" id="files"></ul>
  <input type="text" id="path" placeholder="목록에 없으면 파일 경로를 붙여넣기">

  <div class="row">
    <label class="fld"><span>설정</span><select id="preset"></select></label>
    <label class="fld"><span>출력 폴더</span><select id="outsel"></select></label>
  </div>
  <input type="text" id="outdir" placeholder="출력 폴더 경로">
  <p class="note" id="outnote"></p>

  <details id="more">
    <summary>자세히</summary>
    <div class="grid" style="margin-top:14px">
      <label class="fld"><span>모델</span><select id="model"></select></label>
      <label class="fld"><span>기기</span><select id="device"></select></label>
      <label class="fld"><span>언어</span>
        <select id="lang"><option value="ko">한국어</option><option value="auto">자동 감지</option></select>
      </label>
      <label class="fld"><span>탐색 폭 (beam)</span>
        <select id="beam"><option value="5">5 · 정확</option><option value="1">1 · 빠름</option></select>
      </label>
      <label class="fld"><span>무음 기준 (ms)</span>
        <select id="silence"><option>500</option><option>300</option><option>1000</option></select>
      </label>
    </div>
    <label class="fld hot"><span>이름·전문용어 &nbsp;<i>쉼표로 구분. 사람 이름을 넣으면 오인식이 크게 줄어든다</i></span>
      <input type="text" id="hotwords" placeholder="이승은, 황미혜, 스쿼트, 에스컬레이터"></label>
    <div class="checks">
      <label><input type="checkbox" id="vad" checked> 무음 건너뛰기</label>
      <label><input type="checkbox" id="fallback" checked> 인식 실패 시 재시도</label>
      <label><input type="checkbox" id="prompt"> 말버릇 살리기</label>
      <label><input type="checkbox" id="fixterms" checked> 이름·용어 교정</label>
    </div>
    <div class="dia">
      <label class="sw"><input type="checkbox" id="diarize" checked> <b>화자 분리</b></label>
      <label class="fld inline" id="sens_wrap" hidden><span>전환 민감도</span>
        <select id="sens">
          <option value="high" selected>민감 · 맞장구 보존</option>
          <option value="normal">보통</option>
          <option value="low">둔감 · 과분리 억제</option>
        </select></label>
      <label class="fld inline" id="nspk_wrap" hidden><span>화자 수</span>
        <select id="nspk">
          <option value="0" selected>자동 추정</option><option value="2">2명 · 코칭·인터뷰</option>
          <option value="1">1명</option><option value="3">3명</option>
          <option value="4">4명</option><option value="5">5명</option>
          <option value="6">6명</option><option value="7">7명</option>
          <option value="8">8명</option><option value="9">9명</option>
          <option value="10">10명</option>
        </select></label>
      <p class="hint" id="dia_note" hidden>받아쓰기가 끝난 뒤 한 번 더 돌린다.
        음원 길이의 10~30%가 더 걸린다. 아는 인원을 지정하면 정확도가 오른다.</p>
    </div>
    <div class="checks">
      <label><input type="checkbox" id="f_plain"> 평문 txt</label>
      <label><input type="checkbox" id="f_timed" checked> 시각 포함 txt</label>
      <label><input type="checkbox" id="f_srt"> 자막 srt</label>
      <label><input type="checkbox" id="f_canon" checked> 정본화 초안 md</label>
    </div>
  </details>

  <p class="est" id="est">음원을 고르면 예상 소요 시간을 계산한다.</p>
  <p style="margin:16px 0 0"><button id="add" disabled>대기열에 담기</button></p>
</section>

<section>
  <div class="head">
    <p class="lab">최근 기록</p>
    <button class="mini" id="diagbtn" type="button">진단</button>
  </div>
  <ul class="list" id="hlist"></ul>
</section>

<section id="diagsec" class="hide">
  <div class="head">
    <p class="lab">진단</p>
    <button class="mini" id="diagclose" type="button">닫기</button>
  </div>
  <div class="dg" id="dg">확인하는 중…</div>
</section>

<script>
const $ = s => document.querySelector(s);
const MODELS = __MODELS__, DEVICES = __DEVICES__, COMPUTE = __COMPUTE__;
const SPEED = __SPEED__;
let files = [], picked = new Set(), lastSeg = 0, settings = null, drewFor = 0;

const hms = s => { s=Math.max(0,Math.round(s)); const h=(s/3600)|0,m=((s%3600)/60)|0,x=s%60;
  return h ? `${h}:${String(m).padStart(2,"0")}:${String(x).padStart(2,"0")}`
           : `${m}:${String(x).padStart(2,"0")}`; };
const esc = t => String(t).replace(/[<>&"]/g,c=>({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;"}[c]));
const base = p => String(p).split(/[\\/]/).pop();
const stamp = s => (s||"").replace("T"," ").slice(5,16);

$("#model").innerHTML = MODELS.map(v => `<option>${esc(v)}</option>`).join("");
/* 기기만 값과 보이는 글이 다르다 — auto 가 무엇을 고르는지 옆에 적어 준다 */
$("#device").innerHTML = DEVICES.map(([v,t]) =>
  `<option value="${esc(v)}">${esc(t)}</option>`).join("");

/* ── 설정 폼 ── */
function readOpt(){
  return {
    model:$("#model").value, device:$("#device").value, compute:COMPUTE,
    lang:$("#lang").value, beam:+$("#beam").value, silence:+$("#silence").value,
    vad:$("#vad").checked, fallback:$("#fallback").checked, prompt:$("#prompt").checked,
    fixterms:$("#fixterms").checked, hotwords:$("#hotwords").value,
    diarize:$("#diarize").checked, nspk:+$("#nspk").value, sens:$("#sens").value,
    formats:{ plain:$("#f_plain").checked, timed:$("#f_timed").checked,
              srt:$("#f_srt").checked, canon:$("#f_canon").checked }
  };
}
function writeOpt(o){
  if(!o) return;
  const set=(id,v)=>{ const e=$(id); if(e!=null&&v!=null) e.value=v; };
  const chk=(id,v)=>{ const e=$(id); if(e!=null&&v!=null) e.checked=!!v; };
  set("#model",o.model); set("#device",o.device);
  set("#lang",o.lang); set("#beam",o.beam); set("#silence",o.silence);
  chk("#vad",o.vad); chk("#fallback",o.fallback); chk("#prompt",o.prompt);
  chk("#fixterms",o.fixterms); set("#hotwords",o.hotwords);
  chk("#diarize",o.diarize); set("#nspk",o.nspk); set("#sens",o.sens);
  const f=o.formats||{};
  chk("#f_plain",f.plain); chk("#f_timed",f.timed); chk("#f_srt",f.srt); chk("#f_canon",f.canon);
  syncDia();
}
function syncDia(){
  const on=$("#diarize").checked;
  $("#nspk_wrap").hidden=!on; $("#sens_wrap").hidden=!on; $("#dia_note").hidden=!on;
}

/* ── 담기 ── */
async function loadFiles(){
  const r = await (await fetch("/files")).json();
  files = r.files; picked.clear();
  $("#dir").textContent = r.dir;
  $("#files").innerHTML = files.length
    ? files.map((f,i)=>`<li data-i="${i}">
        <input type="checkbox" data-i="${i}">
        <span class="nm">${esc(f.name)}</span>
        ${f.done?'<span class="tag">변환됨</span>':''}
        <span class="dur">${f.duration?hms(f.duration):"—"}</span></li>`).join("")
    : (r.exists
        ? `<li class="empty">폴더가 비어 있다. 음원을 넣고 <b>새로 고침</b>을 눌러달라.</li>`
        : `<li class="empty">폴더가 없다. <code>${esc(r.dir)}</code> 를 만들고 음원을 넣어달라.<br>
             그때까지는 아래에 파일 경로를 직접 붙여넣으면 된다.</li>`);
  document.querySelectorAll("#files li[data-i]").forEach(li=>{
    const box = li.querySelector("input");
    const toggle = on => { box.checked=on; on?picked.add(+li.dataset.i):picked.delete(+li.dataset.i);
      li.classList.toggle("on",on); estimate(); };
    li.onclick = e => { if(e.target!==box) toggle(!box.checked); };
    box.onchange = () => toggle(box.checked);
  });
  estimate();
}
function chosen(){
  const typed = $("#path").value.trim();
  const list = [...picked].map(i=>files[i]);
  if (typed) list.push({path:typed, duration:0});
  return list;
}
function estimate(){
  const list = chosen();
  $("#add").disabled = list.length===0;
  const total = list.reduce((a,f)=>a+(f.duration||0),0);
  const k = SPEED[$("#model").value + "|" + ($("#device").value==="cuda"?"cuda":"cpu")] || 1;
  if(!list.length){ $("#est").textContent="음원을 고르면 예상 소요 시간을 계산한다."; return; }
  if(!total){ $("#est").innerHTML=`<b>${list.length}개</b> 선택. 길이를 읽지 못했다. 예상 시간은 시작 후 실측으로 나온다.`; return; }
  $("#est").innerHTML = `<b>${list.length}개</b> · 합계 <b>${hms(total)}</b> · 예상 소요 <b>약 ${hms(total/k)}</b>
    <span style="opacity:.7">(${$("#device").value==="cuda"?"GPU":"CPU"} 기준 어림값)</span>`;
}
["#path","#model","#device"].forEach(id=>$(id).addEventListener("input",estimate));

/* ── 출력 폴더 ── */
const SAME = "__same__";
function fillOutdirs(){
  const rec = (settings.recent_outdirs||[]);
  $("#outsel").innerHTML = rec.map(p=>`<option value="${esc(p)}">${esc(p)}</option>`).join("")
    + `<option value="${SAME}">음원과 같은 폴더</option>`;
  if(!$("#outdir").value) $("#outdir").value = rec[0] || "";
}
$("#outsel").onchange = () => {
  const v = $("#outsel").value;
  if(v===SAME){ $("#outdir").value=""; $("#outnote").textContent="음원이 있는 폴더에 저장한다."; }
  else { $("#outdir").value=v; $("#outnote").textContent=""; }
};

/* ── 대기열 ── */
function renderQueue(q){
  $("#qsec").classList.toggle("hide", q.length===0);
  $("#qn").textContent = q.length;
  $("#qlist").innerHTML = q.map((it,i)=>{
    if(it.state==="interrupted") return `<li class="broken" data-id="${it.id}">
      <span class="no">${i+1}</span><span class="nm">${esc(it.name)}</span>
      <span class="meta">중단됨</span>
      <p class="ask">중단된 작업이다. 다시 할까? 이미 만들어진 파일을 어떻게 할지 고른다.
        <button class="mini" data-act="keep">보존하고 다시</button>
        <button class="mini" data-act="over">덮어쓰고 다시</button>
        <button class="mini" data-act="del">빼기</button></p></li>`;
    const s = it.settings||{};
    const tag = (s.diarize?`화자 ${s.nspk||"자동"}`:"화자분리 끔");
    return `<li data-id="${it.id}">
      <span class="no">${i+1}</span><span class="nm">${esc(it.name)}</span>
      <span class="meta">${it.state==="running"?"진행 중":"대기"} · ${esc(tag)}</span>
      <span class="btns">
        <button data-act="up" ${it.state==="running"?"disabled":""}>▲</button>
        <button data-act="down" ${it.state==="running"?"disabled":""}>▼</button>
        <button data-act="del" ${it.state==="running"?"disabled":""}>✕</button>
      </span></li>`;
  }).join("");
}
$("#qlist").onclick = async e => {
  const b = e.target.closest("button"); if(!b) return;
  const id = e.target.closest("li").dataset.id, act = b.dataset.act;
  if(act==="up") await post("/queue/move",{id,dir:-1});
  else if(act==="down") await post("/queue/move",{id,dir:1});
  else if(act==="del") await post("/queue/remove",{id});
  else if(act==="keep") await post("/queue/resume",{id,mode:"keep"});
  else if(act==="over") await post("/queue/resume",{id,mode:"overwrite"});
  poll();
};

/* ── 기록 ── */
function renderHistory(h){
  // 대기열에 다음 항목이 있으면 실패 사유가 화면에서 수 ms 만에 덮인다.
  // 폴링이 900ms 이므로 사실상 못 본다. 기록에 사유가 남아야 하는 이유다.
  $("#hlist").innerHTML = h.length ? h.map(r=>`<li data-id="${r.id}" title="${esc(r.message||"")}">
      <span class="nm">${esc(r.name)}</span>
      <span class="meta">${stamp(r.finished)} · ${hms(r.duration)} · ${(r.speed||0).toFixed(1)}×
        ${r.device?` · ${esc(r.device.toUpperCase())}`:""}${r.speakers?` · 화자 ${r.speakers}명`:""}${r.state==="error"?` · <span class="bad">실패</span>`
        :r.state==="cancelled"?` · <span class="bad">중단</span>`:""}</span>
      <span class="btns">
        <button data-act="open" ${r.outputs&&r.outputs.length?"":"disabled"}>열기</button>
        <button data-act="again">같은 설정으로</button>
        <button data-act="del">삭제</button>
      </span></li>`).join("")
    : `<li class="empty">아직 기록이 없다.</li>`;
}
$("#hlist").onclick = async e => {
  const b = e.target.closest("button"); if(!b) return;
  const id = e.target.closest("li").dataset.id, act = b.dataset.act;
  const rec = (window._hist||[]).find(x=>x.id===id); if(!rec) return;
  if(act==="open"){ rec.outputs.forEach(p=>window.open("/open?p="+encodeURIComponent(p),"_blank")); return; }
  if(act==="del"){ await post("/history/remove",{id}); poll(); return; }
  if(act==="again"){
    writeOpt(rec.settings);
    const r = await post("/history/again",{id});
    if(r.error) alert(r.error); else poll();
  }
};

/* ── 진행 ── */
function drawTicks(dur){
  if(!dur || drewFor===dur) return;
  drewFor = dur;
  const step = dur > 5400 ? 1800 : dur > 1200 ? 600 : 60;
  let h = "";
  for (let t = step; t < dur; t += step){
    const pct = t/dur*100, isHour = t % 3600 === 0;
    h += `<i class="${isHour?"hour":""}" style="left:${pct}%"></i>`;
    if (isHour) h += `<b style="left:${pct}%">${t/3600}시간</b>`;
  }
  $("#ticks").innerHTML = h;
}
function renderJob(j, hasQueue){
  const busy = ["loading","running","diarizing","merging"].includes(j.state);
  const term = ["done","error","cancelled"].includes(j.state);
  $("#live").classList.toggle("hide", !busy && !term && !hasQueue);
  if(!busy && !term && !hasQueue) return;
  window._job = j;
  const known = j.duration > 0;
  $("#bar").classList.toggle("hide", !known);
  $("#nobar").classList.toggle("hide", known || !busy || j.state === "loading");
  const pct = known ? Math.min(100, j.processed/j.duration*100) : 0;
  $("#nowfile").textContent = j.stem ? base(j.file) : "";
  $("#rate").innerHTML = (j.speed ? j.speed.toFixed(1) : "—") + "<small>× 실시간</small>";
  $("#s_left").textContent = j.state==="running" && j.eta > 0 ? hms(j.eta) : "—";
  $("#s_el").textContent = hms(j.elapsed);
  $("#s_ch").textContent = (j.chars||0).toLocaleString();
  // 끝난 뒤에는 남은 시간이 뜻이 없다. 경과는 총 소요로 이름을 바꾼다.
  $("#s_left_box").classList.toggle("hide", term);
  $("#s_el_lab").textContent = term ? "총 소요" : "경과";
  $("#fill").style.width = pct + "%";
  $("#c_now").textContent = hms(j.processed);
  $("#c_pct").textContent = pct.toFixed(1) + "%";
  $("#c_end").textContent = j.duration ? hms(j.duration) : "길이 미상";
  drawTicks(j.duration);

  // 배속 숫자가 이 앱의 주인공이고, 기기가 그 숫자를 설명한다.
  const dv = j.device || "";
  $("#jobdev").classList.toggle("hide", !dv);
  if (dv){
    const fell = !!j.device_note;
    $("#jobdev").textContent = fell ? "CPU · GPU 사용 불가" : dv.toUpperCase();
    $("#jobdev").className = "chip" + (fell ? " broken" : dv === "cpu" ? " cpu" : "");
  }
  $("#devnote").classList.toggle("hide", !j.device_note);
  if (j.device_note) $("#devnote").textContent = j.device_note;

  let ph = j.phase || "";
  if (j.state==="diarizing" && j.dia_pct) ph += " " + j.dia_pct.toFixed(0) + "%";
  if (j.speakers) ph += `  ·  화자 ${j.speakers}명`;
  if (j.state==="done") ph = j.speakers ? `화자 ${j.speakers}명으로 나눴다` : "끝났다";
  $("#phase").textContent = ph;

  if (j.tail && j.tail.length){
    $("#tail").innerHTML = j.tail.map(s=>
      `<p><time>${s.t}</time>${s.s?`<span class="spk s${s.s}">화자${s.s}</span>`:``}
         <span>${esc(s.x)}</span></p>`).join("");
    if (j.segments !== lastSeg){ $("#tail").scrollTop = $("#tail").scrollHeight; lastSeg = j.segments; }
  } else { $("#tail").innerHTML = ""; lastSeg = -1; }
  $("#err").classList.toggle("hide", !j.message);
  if (j.message){ $("#err").textContent = j.message;
    $("#err").classList.toggle("ok", j.state==="done"); }
  const outs = j.outputs || [];
  $("#out").classList.toggle("hide", !(j.state==="done" && outs.length));
  if (j.state==="done" && outs.length)
    $("#out").innerHTML = "저장 위치<br>" + outs.map(o=>
      `<a href="/open?p=${encodeURIComponent(o.path)}" target="_blank">${esc(o.path)}</a>`).join("<br>");

  // 버튼을 상태에 묶는다. 실패한 작업에 "현재 중단" 은 뜻이 없다.
  $("#cancel").classList.toggle("hide", !busy);
  $("#stopall").classList.toggle("hide", !busy && !hasQueue);
  $("#retry").classList.toggle("hide", !(j.hid && (j.state==="error" || j.state==="cancelled")));
  $("#dismiss").classList.toggle("hide", !term);
}

/* ── 폴링 ── */
async function post(p, b){
  const r = await fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},
                          body:JSON.stringify(b||{})});
  return r.json();
}
let timer = null;
async function poll(){
  if (timer) { clearTimeout(timer); timer = null; }
  let s;
  try { s = await (await fetch("/state")).json(); }
  catch(e){ $("#phase").textContent = "앱이 종료됐다. 이 창을 닫아도 된다."; return; }
  const first = settings === null;
  settings = s.settings;
  if (first){ writeOpt(settings.last); fillOutdirs(); loadFiles(); }
  fillPresets();
  window._hist = s.history;
  renderJob(s.job, s.queue.length>0);
  renderQueue(s.queue);
  renderHistory(s.history);
  $("#stopall").textContent = s.stopall ? "대기열 재개" : "전체 중지";
  $("#stopall").classList.toggle("on", s.stopall);
  // 눌렀을 때 무슨 일이 일어나는지 말로 알린다. 안 듣는 것처럼 보이면 안 된다.
  const left = s.queue.filter(x=>x.state==="waiting").length;
  $("#stopnote").classList.toggle("hide", !s.stopall);
  if (s.stopall) $("#stopnote").textContent =
    `지금 것을 끝내면 멈춘다. 남은 ${left}건은 그대로 있다.`
    + (busyNow(s.job) ? "  즉시 멈추려면 [현재 중단] 을 함께 누른다." : "");
  // 전체 중지를 켠 채 대기열이 비면 #live 가 숨어 재개 단추까지 사라진다.
  // 서버는 멈춘 상태 그대로인데 그것을 푸는 유일한 단추가 안 보이면 갇힌다.
  if (s.stopall) $("#live").classList.remove("hide");

  // 머리말 배지. "이 PC에 GPU가 있는가" 와 "지금 무엇으로 도는가" 는 다른 질문이다.
  const g = s.gpu || {};
  $("#devbadge").textContent = g.label || "확인 중";
  $("#devbadge").className = "dev" + (g.state === "gpu" ? " gpu"
                                    : g.state === "broken" ? " broken" : "");
  $("#devbadge").title = (g.why || "눌러서 진단 화면을 연다");

  const busy = ["loading","running","diarizing","merging"].includes(s.job.state);
  timer = setTimeout(poll, busy ? 900 : 3000);
}
let presetNames = "";
function fillPresets(){
  const names = (settings.presets||[]).map(p=>p.name).join("|");
  if (names === presetNames) return;
  presetNames = names;
  $("#preset").innerHTML = `<option value="">직접 정한다</option>` +
    (settings.presets||[]).map((p,i)=>`<option value="${i}">${esc(p.name)}</option>`).join("");
}
$("#preset").onchange = () => {
  const i = $("#preset").value;
  if(i==="") return;
  writeOpt((settings.presets||[])[+i].settings);
  estimate();
};

/* ── 단추 ── */
$("#refresh").onclick = () => { $("#path").value=""; loadFiles(); };
$("#diarize").onchange = async () => {
  syncDia();
  if(!$("#diarize").checked) return;
  $("#f_canon").checked = true;
  $("#dia_note").textContent = "확인하는 중…";
  const r = await (await fetch("/diacheck")).json();
  $("#dia_note").textContent = r.why;
  $("#dia_note").classList.toggle("bad", !r.ok);
};
const busyNow = j => ["loading","running","diarizing","merging"].includes(j.state);
$("#cancel").onclick = () => post("/cancel",{});
$("#stopall").onclick = async () => {
  const s = await (await fetch("/state")).json();
  await post("/queue/stopall",{on:!s.stopall}); poll();
};
$("#dismiss").onclick = async () => { await post("/job/dismiss",{}); poll(); };
$("#qclear").onclick = async () => {
  const s = await (await fetch("/state")).json();
  const n = s.queue.filter(x=>x.state!=="running").length;
  if(!n){ alert("뺄 항목이 없다."); return; }
  if(!confirm(`대기 중인 ${n}건을 대기열에서 뺀다. 돌고 있는 것은 그대로 둔다.\n\n`
            + `이미 만들어진 파일은 지우지 않는다.`)) return;
  await post("/queue/clear",{}); poll();
};
$("#retry").onclick = async () => {
  const j = window._job || {};
  if(!j.hid) return;
  const r = await post("/history/again",{id:j.hid});
  if(r.error){ alert(r.error); return; }
  await post("/job/dismiss",{});
  poll();
};
$("#add").onclick = async () => {
  const list = chosen();
  if(!list.length){ alert("음원을 고르지 않았다."); return; }
  const opt = readOpt();
  if(!Object.values(opt.formats).some(Boolean)){ alert("저장 형식을 하나 이상 골라달라."); return; }
  const same = $("#outsel").value===SAME;
  const paths = list.map(f=>f.path);
  let r;
  if(same){
    r = {ok:true, warn:[]};
    for(const p of paths){
      const dir = p.replace(/[\\/][^\\/]*$/,"");
      const one = await post("/queue/add",{paths:[p],settings:opt,outdir:dir});
      if(one.error){ r = one; break; }
      r.warn = r.warn.concat(one.warn||[]);
    }
  } else {
    r = await post("/queue/add",{paths, settings:opt, outdir:$("#outdir").value.trim()});
  }
  if(r.error){ alert(r.error); return; }
  if(r.warn && r.warn.length) alert("같은 이름이 있다. 나중 것이 덮어쓴다.\n\n" + r.warn.join("\n"));
  $("#path").value=""; picked.clear();
  document.querySelectorAll("#files li input").forEach(b=>{b.checked=false;b.closest("li").classList.remove("on");});
  estimate(); poll(); loadFiles();
};
$("#quit").onclick = async () => {
  const s = await (await fetch("/state")).json();
  const busy = ["loading","running","diarizing","merging"].includes(s.job.state);
  const left = s.queue.filter(x=>x.state==="waiting"||x.state==="running").length;
  if((busy||left) && !confirm(`진행 중인 작업이 있습니다. 종료할까요?\n\n남은 항목 ${left}건. 여기까지 받아쓴 내용은 저장돼 있습니다.`)) return;
  await post("/quit",{});
  document.body.innerHTML = '<div class="wrap"><header><h1>받아쓰기</h1></header>'
    + '<p class="sub">종료했다. 이 창을 닫아도 된다.</p></div>';
};

/* ── 진단 ── */
$("#diagbtn").onclick = async () => {
  $("#diagsec").classList.remove("hide");
  $("#diagsec").scrollIntoView({behavior:"smooth"});
  const d = await (await fetch("/diag")).json();
  const yn = b => b ? '<span class="yes">있음</span>' : '<span class="no">없음</span>';
  const rows = [];
  rows.push(["버전", `v${esc(d.version)} · PID ${d.pid}`]);
  rows.push(["실행 파일", esc(d.file)]);
  rows.push(["파이썬", `${esc((d.python||{}).version)} ${(d.python||{}).ok?'<span class="yes">3.10 이상</span>':'<span class="no">3.10 미만</span>'}`]);
  rows.push(["기기", `${esc((d.device||{}).name)} ${(d.device||{}).cuda?'<span class="yes">GPU 사용</span>':""}`]);
  let h = `<h3>기본</h3><table>${rows.map(r=>`<tr><td class="k">${r[0]}</td><td class="v">${r[1]}</td></tr>`).join("")}</table>`;

  const cu = d.cuda||{}, bd = cu.badge||{};
  h += `<h3>그래픽카드</h3><table>
    <tr><td class="k">지금 판정</td><td class="v">${
      bd.state==="gpu"?`<span class="yes">${esc(bd.label)}</span>`
      :bd.state==="broken"?`<span class="no">${esc(bd.label)}</span>`
      :esc(bd.label||"")}</td></tr>
    <tr><td class="k">물리 GPU</td><td class="v">${(cu.count||0)}개 · ${esc((d.device||{}).name||"")}</td></tr>
    <tr><td class="k">실제 사용</td><td class="v">${
      cu.usable===true?'<span class="yes">확인됐다</span>'
      :cu.usable===false?'<span class="no">못 쓴다</span>'
      :"아직 확인하지 않았다. 아래 탐침을 눌러본다"}</td></tr>`
    + (cu.why?`<tr><td class="k">사유</td><td class="v">${esc(cu.why)}</td></tr>`:"")
    + `<tr><td class="k">DLL 경로</td><td class="v">${
      (cu.dll_dirs||[]).map(esc).join("<br>") || "등록된 곳이 없다"}</td></tr>
    <tr><td class="k">탐침 음원</td><td class="v">${esc(cu.probe||"")} ${yn(cu.probe_exists)}</td></tr>
    </table>
    <p style="margin:10px 0 0">
      <button class="mini" id="probe" type="button">탐침 실행</button>
      <span id="proberes" style="margin-left:10px;font-family:var(--mono);font-size:12px"></span></p>
    <p class="note" style="margin-top:6px">2.8초짜리 실제 음성을 GPU로 받아써 본다.
      <b>모델을 만드는 것만으로는 알 수 없다</b> — CUDA는 실제로 인코딩할 때 비로소 불린다.</p>`;

  h += `<h3>패키지</h3><table>` + (d.packages||[]).map(p=>
    `<tr><td class="k">${esc(p.name)}${p.need?" (필수)":""}</td><td class="v">${
      p.ok?`<span class="yes">${esc(p.version)}</span>`:'<span class="no">없다</span>'}</td></tr>`).join("") + `</table>`;

  const t = d.token||{};
  h += `<h3>화자 분리 토큰</h3><table>
    <tr><td class="k">HF_TOKEN</td><td class="v">${t.ok?`<span class="yes">${esc(t.shown)}</span>`:'<span class="no">없다</span>'}</td></tr>
    <tr><td class="k">설정 파일</td><td class="v">${esc(t.file||"없다")}</td></tr>
    <tr><td class="k">읽은 항목</td><td class="v">${esc((t.keys||[]).join(", ")||"없다")}</td></tr></table>`;

  const m = d.models||{};
  h += `<h3>내려받은 모델</h3><table><tr><td class="k">캐시 폴더</td><td class="v">${esc(m.dir||"")}</td></tr>`
    + ((m.items||[]).length ? (m.items||[]).map(x=>
        `<tr><td class="k">${esc(x.name)}</td><td class="v">${esc(x.size)}</td></tr>`).join("")
      : `<tr><td class="k">—</td><td class="v">아직 없다. 처음 돌릴 때 내려받는다.</td></tr>`) + `</table>`;

  h += `<h3>폴더</h3><table>` + (d.dirs||[]).map(x=>
    `<tr><td class="k">${esc(x.label)}</td><td class="v">${esc(x.path)} ${yn(x.exists)}</td></tr>`).join("")
    + `<tr><td class="k">기록</td><td class="v">${esc((d.log||{}).path)} ${yn((d.log||{}).exists)}</td></tr></table>`;

  h += `<h3>최근 기록 200줄</h3><pre>${esc((d.log_lines||[]).join("\n") || "아직 없다.")}</pre>`;
  $("#dg").innerHTML = h;

  const pb = $("#probe");
  if (pb) pb.onclick = async () => {
    pb.disabled = true;
    $("#proberes").textContent = "돌리는 중… 모델을 처음 올리면 시간이 걸린다";
    const r = await post("/gpu/probe", {});
    pb.disabled = false;
    $("#proberes").innerHTML = r.ok
      ? `<span class="yes">통과 · ${r.sec}초 · ${esc(r.device||"")}</span>`
      : `<span class="no">실패 · ${esc(r.why||"")}</span>`;
    poll();
  };
};
$("#devbadge").onclick = () => $("#diagbtn").click();
$("#diagclose").onclick = () => $("#diagsec").classList.add("hide");

poll();
</script></div></body></html>
"""


# ─────────────────────────────────────────────────────────────
# 서버
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# 진단
#
# 설치·환경 문제를 사용자가 스스로 확인한다. 항목마다 try 로 감싼다.
# 하나가 실패해도 나머지가 보여야 한다.
# 토큰은 있음·없음과 길이까지다. 값은 어디에도 찍지 않는다.
# ─────────────────────────────────────────────────────────────

_DIAG = {"t": 0.0, "data": None}


def hf_cache_dir() -> str:
    for k in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        v = os.environ.get(k)
        if v:
            return v
    home = os.environ.get("HF_HOME")
    if home:
        return os.path.join(home, "hub")
    return os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")


def dir_size(path: str) -> int:
    n = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def diagnose(force: bool = False) -> dict:
    """torch import 가 몇 초 걸린다. 부를 때만 하고 결과를 잠시 쥐고 있는다."""
    if not force and _DIAG["data"] and time.time() - _DIAG["t"] < 60:
        return _DIAG["data"]

    d = {"version": APP_VERSION, "pid": os.getpid(), "file": os.path.abspath(__file__)}

    try:
        d["python"] = {"version": sys.version.split()[0], "exe": sys.executable,
                       "ok": sys.version_info >= (3, 10)}
    except Exception as e:
        d["python"] = {"error": str(e)}

    pkgs = []
    for name, mod, need in (("faster-whisper", "faster_whisper", True),
                            ("pyannote.audio", "pyannote.audio", False),
                            ("torch", "torch", False),
                            ("av", "av", False)):
        try:
            m = __import__(mod)
            for part in mod.split(".")[1:]:
                m = getattr(m, part)
            pkgs.append({"name": name, "ok": True,
                         "version": getattr(m, "__version__", "?"), "need": need})
        except Exception:
            pkgs.append({"name": name, "ok": False, "version": "", "need": need})
    d["packages"] = pkgs

    try:
        tok = hf_token()
        d["token"] = {"ok": bool(tok), "shown": mask(tok),
                      "file": ENV_INFO["file"], "keys": ENV_INFO["keys"]}
    except Exception as e:
        d["token"] = {"error": str(e)}

    try:
        import torch
        cuda = bool(torch.cuda.is_available())
        d["device"] = {"cuda": cuda,
                       "name": torch.cuda.get_device_name(0) if cuda else "CPU"}
    except Exception:
        d["device"] = {"cuda": False, "name": "CPU (torch 없음)"}

    try:
        root = hf_cache_dir()
        models = []
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                if not name.startswith("models--"):
                    continue
                size = dir_size(os.path.join(root, name))
                models.append({"name": name.replace("models--", "").replace("--", "/"),
                               "size": human(size)})
        d["models"] = {"dir": root, "items": models}
    except Exception as e:
        d["models"] = {"dir": "", "items": [], "error": str(e)}

    dirs = []
    for label, p in (("음원", AUDIODIR), ("기본 출력", OUTDIR), ("설정·기록", DATA)):
        try:
            dirs.append({"label": label, "path": p, "exists": os.path.isdir(p)})
        except Exception as e:
            dirs.append({"label": label, "path": p, "exists": False, "why": str(e)})
    d["dirs"] = dirs
    d["log"] = {"path": LOG_PATH, "exists": os.path.isfile(LOG_PATH)}

    try:
        # 물리적으로 GPU 가 있는지와 실제로 쓸 수 있는지는 다른 질문이다.
        # get_cuda_device_count() 는 즉답이다 — 모델을 만들지 않으므로 460MB 를 받지 않는다.
        # 다만 이것이 1 이어도 전사가 된다는 보장은 아니다. cublas 는 실제 추론에서 걸린다.
        register_cuda_dlls()
        import ctranslate2
        d["cuda"] = {"count": ctranslate2.get_cuda_device_count(),
                     "dll_dirs": list(CUDA_DLL_DIRS),
                     "badge": gpu_badge(),
                     "usable": GPU["usable"], "why": GPU["why"],
                     "probe": PROBE_WAV, "probe_exists": os.path.isfile(PROBE_WAV)}
    except Exception as e:
        d["cuda"] = {"count": 0, "dll_dirs": list(CUDA_DLL_DIRS),
                     "error": f"{type(e).__name__}: {e}"[:200]}

    _DIAG.update(t=time.time(), data=d)
    return d


# ─────────────────────────────────────────────────────────────
# 무창 실행과 바로가기
#
# .vbs 규칙 — ANSI 또는 UTF-16LE. 한글을 넣지 않는다. 경로를 박지 않는다.
# start.vbs 는 순수 ASCII 로 쓰고 자기 위치에서 폴더를 얻는다.
# 바로가기 이름에는 한글이 불가피하므로 그 도우미만 UTF-16LE 로 쓰고 지운다.
# ─────────────────────────────────────────────────────────────

START_VBS = (
    "' Batasseugi launcher. Runs the app with no console window.\n"
    "' ASCII only, no Korean, no hard-coded paths. See CLAUDE.md for why.\n"
    "Option Explicit\n"
    "Dim sh, fso, here\n"
    'Set sh = CreateObject("WScript.Shell")\n'
    'Set fso = CreateObject("Scripting.FileSystemObject")\n'
    "here = fso.GetParentFolderName(WScript.ScriptFullName)\n"
    "sh.CurrentDirectory = here\n"
    "On Error Resume Next\n"
    'sh.Run "pythonw.exe """ & here & "\\app.py""", 0, False\n'
    "If Err.Number <> 0 Then\n"
    '  MsgBox "Python was not found. Install Python 3.10 or newer and be sure to '
    'check Add Python to PATH.", 48, "Batasseugi"\n'
    "End If\n"
)


def write_start_vbs() -> str:
    path = os.path.join(BASE, "start.vbs")
    with open(path, "w", encoding="ascii", newline="\r\n") as f:
        f.write(START_VBS)
    return path


def desktop_dir() -> str:
    """OneDrive 로 옮겨진 바탕화면이 흔하다. 레지스트리를 먼저 본다."""
    try:
        import winreg
        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            v = winreg.QueryValueEx(k, "Desktop")[0]
            if v and os.path.isdir(v):
                return v
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def make_shortcut(name: str = "받아쓰기") -> dict:
    """바탕화면 바로가기를 만든다. 실패해도 start.vbs 위치를 알린다."""
    if os.name != "nt":
        return {"ok": False, "why": "윈도우에서만 만든다.", "start": ""}
    start = write_start_vbs()
    desk = desktop_dir()
    lnk = os.path.join(desk, name + ".lnk")
    helper = os.path.join(DATA, "_shortcut.vbs")
    body = ('Set sh = CreateObject("WScript.Shell")\r\n'
            f'Set lnk = sh.CreateShortcut("{lnk}")\r\n'
            f'lnk.TargetPath = "{start}"\r\n'
            f'lnk.WorkingDirectory = "{BASE}"\r\n'
            'lnk.Description = "Batasseugi"\r\n'
            'lnk.Save\r\n')
    try:
        os.makedirs(DATA, exist_ok=True)
        # 한글 경로가 섞이므로 UTF-16LE 로 쓴다. UTF-8 로 쓰면 cscript 가 깨뜨린다.
        with open(helper, "w", encoding="utf-16") as f:
            f.write(body)
        import subprocess
        r = subprocess.run(["cscript", "//nologo", helper],
                           capture_output=True, timeout=30)
        if r.returncode != 0:
            return {"ok": False, "start": start,
                    "why": r.stderr.decode("utf-8", "replace")[:200] or "cscript 실패"}
        return {"ok": os.path.exists(lnk), "path": lnk, "start": start,
                "why": "" if os.path.exists(lnk) else "바로가기가 만들어지지 않았다."}
    except Exception as e:
        return {"ok": False, "start": start, "why": f"{type(e).__name__}: {e}"}
    finally:
        try:
            os.remove(helper)
        except OSError:
            pass


SRV = None                                 # main() 에서 채운다


def shutdown_later() -> None:
    """응답을 보낸 뒤에 멈춘다. 서비스 스레드에서 shutdown() 을 부르면 잠긴다."""
    time.sleep(0.3)
    CANCEL.set()
    STOP_ALL.set()
    log("  화면에서 종료를 눌렀다.")
    try:
        if SRV is not None:
            SRV.shutdown()
    except Exception:
        pass


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                                     # 콘솔을 조용히 둔다

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 브라우저가 이전 화면을 붙들고 있으면 갱신한 기능이 안 보인다.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)

        if u.path == "/":
            page = (PAGE
                    .replace("__VER__", APP_VERSION)
                    .replace("__MODELS__", json.dumps(MODELS))
                    # 라벨에 한글이 있다. 페이지가 UTF-8 이므로 그대로 싣는다
                    .replace("__DEVICES__", json.dumps(device_options(),
                                                       ensure_ascii=False))
                    .replace("__COMPUTE__", json.dumps(COMPUTE))
                    .replace("__SPEED__", json.dumps(
                        {f"{m}|{d}": v for (m, d), v in ROUGH_SPEED.items()})))
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

        if u.path == "/files":
            return self._json(list_audio())

        if u.path == "/diacheck":
            return self._json(dia_check())

        if u.path == "/version":
            return self._json({"version": APP_VERSION, "pid": os.getpid(),
                               "file": os.path.abspath(__file__)})

        if u.path == "/status":
            with LOCK:
                return self._json(dict(JOB))

        if u.path == "/log":
            n = int(urllib.parse.parse_qs(u.query).get("n", ["200"])[0] or 200)
            return self._json({"path": LOG_PATH, "lines": log_tail(min(n, 2000))})

        if u.path == "/diag":
            q = urllib.parse.parse_qs(u.query)
            d = diagnose(force=q.get("force", ["0"])[0] == "1")
            return self._json(dict(d, log_lines=log_tail(200)))

        if u.path == "/open":
            p = urllib.parse.parse_qs(u.query).get("p", [""])[0]
            # startswith 를 쓰지 않는다. out_text 가 out_text2 를 통과시킨다.
            # 허용 뿌리는 설정에 등록된 출력 폴더뿐이다. 임의 경로를 받지 않는다.
            if os.path.isfile(p) and any(under(p, r) for r in allowed_roots()):
                with open(p, "rb") as f:
                    return self._send(200, f.read(), "text/plain; charset=utf-8")
            return self._send(404, b"not found", "text/plain")

        if u.path == "/queue":
            with STATE_LOCK:
                return self._json({"items": QUEUE["items"],
                                   "stopall": STOP_ALL.is_set()})

        if u.path == "/history":
            n = int(urllib.parse.parse_qs(u.query).get("n", ["50"])[0] or 50)
            with STATE_LOCK:
                return self._json({"items": HISTORY["items"][:min(n, HISTORY_MAX)]})

        if u.path == "/settings":
            with STATE_LOCK:
                return self._json(SETTINGS)

        if u.path == "/state":
            with LOCK:
                job = dict(JOB)
            with STATE_LOCK:
                return self._json({
                    "job": job, "queue": QUEUE["items"],
                    "stopall": STOP_ALL.is_set(),
                    "history": HISTORY["items"][:20],
                    "settings": SETTINGS, "version": APP_VERSION,
                    "gpu": gpu_badge()})

        return self._send(404, b"not found", "text/plain")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)

        if u.path == "/cancel":                 # 현재 항목만 중단하고 다음으로 간다
            CANCEL.set()
            return self._json({"ok": True})

        if u.path == "/start":
            # 없애지 않는다. 큐에 1건 넣고 곧바로 도는 것으로 속만 바꿨다.
            # 기존 화면의 시작 버튼·/status 폴링·중단 버튼이 그대로 동작한다.
            opt = self._body()
            path = (opt.get("path") or "").strip().strip('"')
            outdir = opt.get("outdir") or OUTDIR
            opt.pop("path", None)
            opt.pop("outdir", None)
            return self._json(enqueue([path], dict(DEFAULT_OPT, **opt), outdir))

        if u.path == "/queue/add":
            b = self._body()
            settings = dict(DEFAULT_OPT, **(b.get("settings") or {}))
            return self._json(enqueue(b.get("paths") or [], settings,
                                      b.get("outdir") or OUTDIR))

        if u.path == "/queue/move":
            b = self._body()
            return self._json(move_item(b.get("id", ""), int(b.get("dir", 0))))

        if u.path == "/queue/remove":
            return self._json(remove_item(self._body().get("id", "")))

        if u.path == "/queue/resume":
            b = self._body()
            return self._json(resume_item(b.get("id", ""), b.get("mode", "overwrite")))

        if u.path == "/queue/clear":
            # 대기 중인 것만 뺀다. 도는 것은 건드리지 않고 **산출물도 지우지 않는다.**
            # 이 프로젝트의 안전장치 전부가 "중단해도 여기까지는 남는다" 를 향해 있다.
            with STATE_LOCK:
                keep = [x for x in QUEUE["items"] if x.get("state") == "running"]
                n = len(QUEUE["items"]) - len(keep)
                QUEUE["items"] = keep
                save_queue()
            if n:
                log(f"   대기열을 비웠다 — {n}건. 만들어진 파일은 그대로 둔다.")
            return self._json({"ok": True, "removed": n})

        if u.path == "/queue/stopall":
            on = bool(self._body().get("on", True))
            STOP_ALL.set() if on else STOP_ALL.clear()
            log(f"   전체 {'중지' if on else '재개'}")
            return self._json({"ok": True, "stopall": on})

        if u.path == "/history/remove":
            hid = self._body().get("id", "")
            with STATE_LOCK:
                HISTORY["items"] = [x for x in HISTORY["items"] if x["id"] != hid]
                save_history()
            return self._json({"ok": True})

        if u.path == "/history/again":
            # 본문은 한 번만 읽는다. 두 번 부르면 rfile 이 비어 영원히 기다린다.
            b = self._body()
            with STATE_LOCK:
                rec = next((x for x in HISTORY["items"]
                            if x["id"] == b.get("id", "")), None)
            if rec is None:
                return self._json({"error": "기록을 찾지 못했다."})
            settings = dict(rec.get("settings") or DEFAULT_OPT)
            settings.update(b.get("settings") or {})     # 이름·용어를 보태 다시 돌린다
            return self._json(enqueue([rec["path"]], settings,
                                      b.get("outdir") or rec.get("outdir") or OUTDIR))

        if u.path == "/settings":
            b = self._body()
            with STATE_LOCK:
                if "last" in b:
                    SETTINGS["last"] = norm_opt(b["last"])
                if "presets" in b:
                    SETTINGS["presets"] = b["presets"]
                save_settings()
                return self._json(SETTINGS)

        if u.path == "/gpu/probe":
            # 누를 때만 돈다. 기동 시에는 절대 부르지 않는다.
            return self._json(gpu_probe())

        if u.path == "/job/dismiss":
            # 끝난 알림을 화면에서 내린다. 도는 중이면 건드리지 않는다.
            with LOCK:
                if JOB["state"] not in TERMINAL:
                    return self._json({"error": "아직 도는 중이다."})
                JOB.update(state="idle", phase="", message="", eta=0.0,
                           file="", stem="", outputs=[], tail=[],
                           processed=0.0, duration=0.0, speed=0.0, elapsed=0.0,
                           segments=0, chars=0, speakers=0, dia_pct=0.0,
                           qid="", hid="")
            return self._json({"ok": True})

        if u.path == "/outdir/check":
            return self._json(check_outdir(self._body().get("path", "")))

        if u.path == "/shortcut":
            return self._json(make_shortcut())

        if u.path == "/quit":
            # 먼저 응답하고 다른 스레드에서 멈춘다. 여기서 shutdown() 을 부르면 잠긴다.
            self._json({"ok": True})
            threading.Thread(target=shutdown_later, daemon=True).start()
            return

        return self._send(404, b"not found", "text/plain")


class Server(socketserver.ThreadingTCPServer):
    # 윈도우에서 allow_reuse_address 를 켜면 이전 프로세스가 살아 있어도
    # 새 프로세스가 오류 없이 뜬다. 그러면 콘솔은 새 버전, 화면은 옛 버전이 된다.
    allow_reuse_address = (os.name != "nt")
    daemon_threads = True


def port_busy(port: int) -> str:
    """이미 누가 이 포트를 쓰고 있는지 본다. 쓰고 있으면 그쪽 버전을 알려준다."""
    import socket
    with socket.socket() as sk:
        sk.settimeout(0.4)
        if sk.connect_ex(("127.0.0.1", port)) != 0:
            return ""
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=1) as r:
            d = json.loads(r.read())
            return f"v{d.get('version', '?')} (PID {d.get('pid', '?')})"
    except Exception:
        return "정체를 알 수 없는 프로그램"


# ─────────────────────────────────────────────────────────────
# 자가 시험 — python app.py --selftest
#
# fix_terms 의 규칙은 두 번 뒤집혔다. 어절 가운데를 훑다가 "상황이라서" 를
# "상황미혜서" 로 깨뜨렸고, 머리를 먼저 자르다가 "이승입니다" 를 보류로 흘렸다.
# 사례를 코드에 박아두지 않으면 세 번째가 온다.
# ─────────────────────────────────────────────────────────────

SELFTEST_TERMS = ["이승은", "황미혜", "이대표님", "스쿼트",
                  "에스컬레이터", "헬스클럽", "대중교통"]

SELFTEST_CASES = [
    # (입력, 기대 출력, 분류)
    ("이승입니다",      "이승은입니다",     "정상 교정"),
    ("이승님입니다",     "이승은입니다",     "정상 교정"),
    ("황미애입니다",     "황미혜입니다",     "정상 교정"),
    ("이대포님이라고",    "이대표님이라고",    "정상 교정"),
    ("헬스컬러에서",     "헬스클럽에서",     "정상 교정"),
    ("대중대통을",      "대중교통을",      "정상 교정"),
    ("에스칼레이터를",    "에스컬레이터를",    "정상 교정"),

    ("상황이라서",      "상황이라서",      "파괴 차단"),   # 황이라 → 황미혜
    ("상황에서",       "상황에서",       "파괴 차단"),   # 황에서 → 황미혜
    ("사이클을",       "사이클을",       "파괴 차단"),   # 이클을 → 이승은

    ("이승은이",       "이승은이",       "이미 맞음"),
    ("대중교통을",      "대중교통을",      "이미 맞음"),
    ("황미혜코치님",     "황미혜코치님",     "이미 맞음"),
    ("이대표님이라고",    "이대표님이라고",    "이미 맞음"),

    ("이상하다",       "이상하다",       "무관"),
    ("헬스장에서",      "헬스장에서",      "무관"),
]


def selftest() -> int:
    """16개 사례를 돌린다. 하나라도 어긋나면 1을 돌려준다."""
    print(f"\n  fix_terms 자가 시험 — v{APP_VERSION}\n")
    bad, holds = 0, 0
    for src, want, kind in SELFTEST_CASES:
        rows = [{"start": 0.0, "text": src}]
        hits = fix_terms(rows, list(SELFTEST_TERMS))
        got = rows[0]["text"]
        holds += sum(1 for e in hits if e.get("hold"))
        mark = "OK  " if got == want else "실패"
        if got != want:
            bad += 1
        note = ""
        if hits:
            e = hits[0]
            note = f"{'보류' if e.get('hold') else '반영'} {e['was']}→{e['now']} {e['dist']}/{e['of']}"
        print(f"  {mark} [{kind}] {src:<12} → {got:<12} {note}")
        if got != want:
            print(f"       기대: {want}")
    print(f"\n  {len(SELFTEST_CASES)}건 중 {len(SELFTEST_CASES) - bad}건 통과 · 보류 {holds}건\n")
    return 1 if bad else 0


def main() -> None:
    setup_log()

    url = f"http://127.0.0.1:{PORT}"
    busy = port_busy(PORT)
    if busy:
        # 이미 떠 있으면 새로 띄우지 않고 기존 화면을 연다.
        # 무창 실행에서는 안내문을 볼 수 없으므로 브라우저가 대신 답한다.
        log(f"\n  !! {PORT} 포트를 이미 {busy} 가 쓰고 있다. 기존 화면을 연다.")
        log(f"     바꾼 것이 안 보이면 그 화면의 종료 버튼을 누르고 다시 실행해달라.")
        log(f"     다른 포트로 띄우려면 — set PORT=8766 && python app.py\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return

    load_env()
    os.makedirs(OUTDIR, exist_ok=True)
    load_state()
    threading.Thread(target=queue_loop, daemon=True).start()
    log(f"\n  받아쓰기 v{APP_VERSION} — {url}")
    log(f"  음원 폴더 — {AUDIODIR}"
        + ("" if os.path.isdir(AUDIODIR) else "   (없다. 폴더를 만들어 음원을 넣어달라)"))
    log(f"  저장 위치 — {OUTDIR}")
    if ENV_INFO["file"]:
        log(f"  설정 파일 — {ENV_INFO['file']}  ({len(ENV_INFO['keys'])}개 항목: "
            f"{', '.join(ENV_INFO['keys'])})")
    else:
        log(f"  설정 파일 — 없음. HF_TOKEN이 필요하면 {os.path.join(BASE, 'env.local')} 에 넣어달라")
    tok = hf_token()
    # 토큰은 값을 남기지 않는다. 앞 4자와 길이까지다.
    log(f"  화자 분리 — " + (f"HF_TOKEN 확인 {mask(tok)}" if tok
                            else "HF_TOKEN 없음. 화자 분리를 쓸 수 없다"))
    log(f"  기록 파일 — {LOG_PATH}")
    log("  바탕화면 바로가기를 만들려면 — python app.py --shortcut")
    with STATE_LOCK:
        n_wait = sum(1 for x in QUEUE["items"] if x.get("state") == "waiting")
        n_int = sum(1 for x in QUEUE["items"] if x.get("state") == "interrupted")
        n_hist = len(HISTORY["items"])
    log(f"  대기열 — 대기 {n_wait}건"
        + (f" · 중단됨 {n_int}건" if n_int else "") + f" · 이력 {n_hist}건")
    # 무창 실행에는 누를 창이 없다. 그때는 화면 안 종료 버튼뿐이다.
    log("  종료 — " + ("화면 오른쪽 위 종료 버튼" if not _CONSOLE
                       else "화면의 종료 버튼 또는 이 창에서 Ctrl+C") + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    global SRV
    with Server(("127.0.0.1", PORT), Handler) as srv:
        SRV = srv
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            log("  종료했다.\n")
    SRV = None
    log(f"── 받아쓰기 v{APP_VERSION} 종료")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--shortcut" in sys.argv:
        setup_log()
        r = make_shortcut()
        log(f"  실행 스크립트 — {r.get('start', '')}")
        log("  바탕화면 바로가기 — " + (r["path"] if r.get("ok")
                                        else f"만들지 못했다. {r.get('why', '')}"))
        sys.exit(0 if r.get("ok") else 1)
    main()
