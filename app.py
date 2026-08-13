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

APP_VERSION = "2026-08-13.12"     # 화면 우상단과 콘솔에 찍힌다. 갱신 확인용이다.

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8765"))
# 변환 대상 폴더. 실행 위치와 무관하게 스크립트 옆의 audio 를 본다.
AUDIODIR = os.path.abspath(os.environ.get("AUDIODIR", os.path.join(BASE, "audio")))
OUTDIR = os.path.abspath(os.environ.get("OUTDIR", os.path.join(BASE, "out_text")))
AUDIO_EXT = (".wav", ".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".aac",
             ".wma", ".webm", ".mkv", ".mov", ".amr", ".opus")

MODELS = ["large-v3-turbo", "large-v3", "medium", "small"]
DEVICES = ["auto", "cpu", "cuda"]
COMPUTES = ["int8", "int8_float16", "float16"]

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

DEFAULT_OPT = {
    "model": "large-v3-turbo", "device": "auto", "compute": "int8",
    "lang": "ko", "beam": 5, "silence": 500,
    "vad": True, "fallback": True, "prompt": False, "fixterms": True,
    "hotwords": "",
    "diarize": False, "nspk": 2, "sens": "high",
    "formats": {"plain": True, "timed": True, "srt": False, "canon": False},
}

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


def get_whisper(model: str, device: str, compute: str):
    """(모델, 반환값이 캐시에서 나왔는지)"""
    from faster_whisper import WhisperModel
    key = (model, device, compute)
    _CACHE_USED[0] = time.time()
    if _WHISPER["key"] == key and _WHISPER["obj"] is not None:
        return _WHISPER["obj"], True
    _WHISPER["obj"] = None                 # 새로 만들기 전에 먼저 놓는다. 메모리 때문이다
    _WHISPER["key"] = None
    obj = WhisperModel(model, device=device, compute_type=compute)
    _WHISPER.update(key=key, obj=obj)
    return obj, False


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
    try:
        import torch
        if torch.cuda.is_available():
            pipe.to(torch.device("cuda"))
    except Exception:
        pass
    _DIA.update(key=key, obj=pipe)
    return pipe, False


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
    log(f"   모델을 메모리에서 놓았다.{(' ' + why) if why else ''}")


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
    "qid": "", "outdir": "",           # 지금 도는 대기열 항목과 그 출력 폴더
    "cached": False,                   # 모델을 캐시에서 꺼냈는지. 재적재 회귀를 본다
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

    def fail(msg: str, state: str = "error"):
        """실패 사유는 화면과 기록에 함께 남긴다. 기록이 없으면 무창 실행에서 원인을 못 찾는다."""
        upd(state=state, message=msg)
        log(f"   실패 — {msg}")

    upd(state="loading", file=path, stem=stem, phase="모델을 준비하고 있다", message="",
        processed=0.0, segments=0, chars=0, tail=[], outputs=[], speakers=0, dia_pct=0.0,
        corrections=0, holds=0, outdir=outdir, cached=False,
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
        fail("faster-whisper가 없다. pip install faster-whisper 를 실행해달라.")
        return

    try:
        t_load = time.time()
        model, cached = get_whisper(opt["model"], opt["device"], opt["compute"])
        upd(cached=cached)
        log(f"   모델 {'재사용' if cached else '적재'} {time.time() - t_load:.1f}초")
    except Exception as e:
        fail(f"모델을 불러오지 못했다. {e}")
        return

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

    try:
        if hot:
            try:
                segments, info = model.transcribe(path, hotwords=hot, **kwargs)
            except TypeError:
                kwargs["initial_prompt"] = (
                    (kwargs.get("initial_prompt", "") + " ").strip()
                    + f" 등장하는 이름과 용어: {hot}.")
                segments, info = model.transcribe(path, **kwargs)
        else:
            segments, info = model.transcribe(path, **kwargs)
    except Exception as e:
        fail(f"음원을 읽지 못했다. {e}")
        return

    dur = float(getattr(info, "duration", 0.0)) or probe_duration(path)
    upd(state="running", duration=dur,
        phase="받아쓰는 중" + (" · 끝나면 화자를 나눈다" if diarize else ""))

    # ── 1단계 · 전사. 구간마다 임시 파일에 적어 중단에 대비한다 ──
    tmp = os.path.join(outdir, f"{stem}.txt")
    rows, n, chars = [], 0, 0
    stopped = False
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
                               speed=sp, eta=(dur - seg.end) / sp if sp > 0 else 0)
                    JOB["tail"] = (JOB["tail"] + [{"t": hms(seg.start), "s": 0, "x": text}])[-14:]
    except Exception as e:
        fail(f"전사 중 멈췄다. {e}")
        return

    if not rows:
        upd(phase="")
        fail("받아쓴 내용이 없다.", "cancelled" if stopped else "error")
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
    upd(state="cancelled" if stopped else "done", outputs=made, phase="",
        message=(JOB["message"] or ("중단했다. 여기까지는 저장돼 있다." if stopped
                 else ("끝났다." if turns or not diarize
                       else "끝났다. 화자 분리는 하지 못했다."))))

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
        "settings": item.get("settings", {}),
        "outputs": [o["path"] for o in j.get("outputs", [])],
        "state": j.get("state", "done"), "message": j.get("message", ""),
    }
    with STATE_LOCK:
        QUEUE["items"] = [x for x in QUEUE["items"] if x["id"] != item["id"]]
        HISTORY["items"] = ([rec] + HISTORY["items"])[:HISTORY_MAX]
        save_queue()
        save_history()


def queue_loop() -> None:
    """기동 시 데몬 스레드 하나로 돈다."""
    while True:
        item = None if STOP_ALL.is_set() else take_next()
        if item is None:
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
            with LOCK:
                JOB.update(state="error", message=f"{type(e).__name__}: {e}")
        finish_item(item)


def enqueue(paths: list, settings: dict, outdir: str) -> dict:
    """여러 건을 한 번에 담는다. 담기는 순간 실행기가 집어간다."""
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
.wrap{max-width:860px;margin:0 auto;padding:32px 20px 64px}

header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
       padding-bottom:14px;border-bottom:2px solid var(--ink)}
h1{margin:0;font-size:26px;font-weight:700;letter-spacing:-.02em}
.badge{font-family:var(--mono);font-size:11px;letter-spacing:.06em;
       color:var(--signal);border:1px solid var(--signal);border-radius:2px;
       padding:2px 7px;text-transform:uppercase}
.ver{margin-left:auto;font-family:var(--mono);font-size:12px;color:#fff;
     background:var(--ink);padding:3px 9px;border-radius:2px;letter-spacing:.02em}
.sub{color:var(--muted);font-size:13px;margin:10px 0 26px}

section{background:var(--surface);border:1px solid var(--rule);border-radius:3px;
        padding:18px 20px;margin-bottom:16px}
.lab{font-family:var(--mono);font-size:11px;letter-spacing:.1em;color:var(--muted);
     text-transform:uppercase;margin:0 0 12px}

ul.files{list-style:none;margin:0;padding:0;max-height:210px;overflow:auto}
ul.files li{display:flex;align-items:center;gap:12px;padding:7px 9px;
            border-radius:2px;cursor:pointer;border:1px solid transparent}
ul.files li:hover{background:var(--signal-soft)}
ul.files li[aria-selected="true"]{border-color:var(--signal);background:var(--signal-soft)}
ul.files .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
ul.files .dur{font-family:var(--mono);font-size:13px;color:var(--muted)}
.empty{color:var(--muted);font-size:13px;padding:10px 0;line-height:1.7}
.empty code{font-family:var(--mono);background:var(--ground);padding:2px 5px;border-radius:2px}
.head{display:flex;align-items:center;justify-content:space-between;gap:12px}
.head .lab{margin-bottom:0}
.dir{font-family:var(--mono);font-size:12px;color:var(--signal);margin:8px 0 12px;
     word-break:break-all}
button.mini{padding:4px 11px;font-size:12px;font-weight:500;background:transparent;
            color:var(--muted);border-color:var(--rule)}
button.mini:hover{background:var(--ground);color:var(--ink);border-color:var(--muted)}
.tag{font-family:var(--mono);font-size:10px;letter-spacing:.05em;color:var(--muted);
     border:1px solid var(--rule);border-radius:2px;padding:1px 5px;flex:none}

input[type=text],select{font:inherit;color:var(--ink);background:var(--surface);
  border:1px solid var(--rule);border-radius:2px;padding:7px 9px}
input[type=text]{width:100%;font-family:var(--mono);font-size:13px;margin-top:10px}
input[type=text]:focus,select:focus,button:focus-visible,ul.files li:focus-visible
  {outline:2px solid var(--signal);outline-offset:1px}

.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}
.fld{display:flex;flex-direction:column;gap:5px}
.fld span{font-size:12px;color:var(--muted)}
.fld.hot{margin-top:16px}
.fld.hot i{font-style:normal;opacity:.75}
.fld.hot input{margin-top:4px}
.checks{display:flex;flex-wrap:wrap;gap:16px;margin-top:16px;font-size:13px}
.checks label{display:flex;align-items:center;gap:6px;cursor:pointer}

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
button.ghost{background:transparent;color:var(--warn);border-color:var(--warn)}
button.ghost:hover{background:#F7ECE6}

/* 신호판 — 이 앱의 주인공 */
.meter{display:flex;align-items:flex-end;gap:22px;flex-wrap:wrap;margin-bottom:16px}
.rate{font-family:var(--mono);font-size:52px;font-weight:700;line-height:.95;
      letter-spacing:-.03em;color:var(--signal)}
.rate small{font-size:16px;font-weight:400;color:var(--muted);margin-left:6px;
            letter-spacing:0}
.stats{display:flex;gap:22px;flex-wrap:wrap;margin-left:auto}
.stat{text-align:right}
.stat b{display:block;font-family:var(--mono);font-size:17px;font-weight:600}
.stat span{font-size:11px;color:var(--muted);letter-spacing:.06em;text-transform:uppercase}

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
      max-height:260px;overflow:auto}
.tail p{margin:0 0 5px;display:flex;gap:11px;font-size:14px}
.tail time{font-family:var(--mono);font-size:12px;color:var(--signal);
           flex:none;padding-top:2px}
.tail p.new{animation:in .35s ease-out}
@keyframes in{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}

.note{font-size:13px;color:var(--muted);margin-top:12px}
.err{color:var(--warn);font-size:14px;margin-top:12px;padding:9px 12px;
     background:#F9EFE9;border-left:3px solid var(--warn);border-radius:2px}
.err.ok{color:var(--ink);background:var(--signal-soft);border-left-color:var(--signal)}
.out{margin-top:14px;font-family:var(--mono);font-size:13px}
.out a{color:var(--signal)}
.hide{display:none}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style></head><body><div class="wrap">

<header>
  <h1>받아쓰기</h1>
  <span class="badge">이 PC에서만 처리 · 비용 없음</span>
  <span class="ver">v__VER__</span>
</header>
<p class="sub">음성이 인터넷으로 나가지 않는다. 긴 강의 파일을 끝까지 돌리기 위한 도구다.</p>

<div id="setup">
  <section>
    <div class="head">
      <p class="lab">음원 폴더</p>
      <button class="mini" id="refresh" type="button">새로 고침</button>
    </div>
    <p class="dir" id="dir">—</p>
    <ul class="files" id="files"></ul>
    <input type="text" id="path" placeholder="목록에 없으면 파일 경로를 붙여넣기">
  </section>

  <section>
    <p class="lab">설정</p>
    <div class="grid">
      <label class="fld"><span>모델</span><select id="model"></select></label>
      <label class="fld"><span>기기</span><select id="device"></select></label>
      <label class="fld"><span>연산 정밀도</span><select id="compute"></select></label>
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
      <label class="sw"><input type="checkbox" id="diarize"> <b>화자 분리</b></label>
      <label class="fld inline" id="sens_wrap" hidden><span>전환 민감도</span>
        <select id="sens">
          <option value="high" selected>민감 · 맞장구 보존</option>
          <option value="normal">보통</option>
          <option value="low">둔감 · 과분리 억제</option>
        </select></label>
      <label class="fld inline" id="nspk_wrap" hidden><span>화자 수</span>
        <select id="nspk">
          <option value="0">자동 추정</option><option value="2" selected>2명 · 코칭·인터뷰</option>
          <option value="1">1명</option><option value="3">3명</option>
          <option value="4">4명</option><option value="5">5명</option>
          <option value="6">6명</option><option value="8">8명</option><option value="10">10명</option>
        </select></label>
      <p class="hint" id="dia_note" hidden>받아쓰기가 끝난 뒤 한 번 더 돌린다.
        음원 길이의 10~30%가 더 걸린다. 아는 인원을 지정하면 정확도가 오른다.</p>
    </div>

    <div class="checks">
      <label><input type="checkbox" id="f_plain" checked> 평문 txt</label>
      <label><input type="checkbox" id="f_timed" checked> 시각 포함 txt</label>
      <label><input type="checkbox" id="f_srt"> 자막 srt</label>
      <label><input type="checkbox" id="f_canon"> 정본화 초안 md</label>
    </div>
    <p class="est" id="est">음원을 고르면 예상 소요 시간을 계산한다.</p>
  </section>

  <button id="go" disabled>시작</button>
  <p class="note" id="hint">처음 실행할 때는 모델을 내려받느라 몇 분 걸린다. 한 번만 받는다.</p>
</div>

<div id="live" class="hide">
  <section>
    <div class="meter">
      <div><div class="rate" id="rate">—<small>× 실시간</small></div></div>
      <div class="stats">
        <div class="stat"><b id="s_left">—</b><span>남은 시간</span></div>
        <div class="stat"><b id="s_el">—</b><span>경과</span></div>
        <div class="stat"><b id="s_ch">0</b><span>글자</span></div>
      </div>
    </div>

    <p class="phase" id="phase"></p>
    <div class="track"><div class="fill" id="fill"></div><div class="ticks" id="ticks"></div></div>
    <div class="clock"><em id="c_now">0:00</em><span id="c_pct">0%</span><span id="c_end">—</span></div>

    <div class="tail" id="tail"></div>
    <p class="err hide" id="err"></p>
    <div class="out hide" id="out"></div>
    <p style="margin-top:18px"><button class="ghost" id="stop">중단</button>
       <button id="again" class="hide" style="margin-left:8px">다른 파일 받아쓰기</button></p>
  </section>
</div>

<script>
const $ = s => document.querySelector(s);
const MODELS = __MODELS__, DEVICES = __DEVICES__, COMPUTES = __COMPUTES__;
const SPEED = __SPEED__;
let picked = null, files = [], lastSeg = 0;

for (const [id, arr] of [["#model",MODELS],["#device",DEVICES],["#compute",COMPUTES]])
  $(id).innerHTML = arr.map(v => `<option>${v}</option>`).join("");

const hms = s => { s=Math.max(0,Math.round(s)); const h=(s/3600)|0,m=((s%3600)/60)|0,x=s%60;
  return h ? `${h}:${String(m).padStart(2,"0")}:${String(x).padStart(2,"0")}`
           : `${m}:${String(x).padStart(2,"0")}`; };

const esc = t => t.replace(/[<>&]/g,c=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));

async function loadFiles(){
  const r = await (await fetch("/files")).json();
  files = r.files;
  $("#dir").textContent = r.dir;
  $("#files").innerHTML = files.length
    ? files.map((f,i)=>`<li tabindex="0" data-i="${i}">
        <span class="nm">${esc(f.name)}</span>
        ${f.done?'<span class="tag">변환됨</span>':''}
        <span class="dur">${f.duration?hms(f.duration):"—"}</span></li>`).join("")
    : (r.exists
        ? `<li class="empty">폴더가 비어 있다. 음원을 넣고 <b>새로 고침</b>을 눌러달라.</li>`
        : `<li class="empty">폴더가 없다. <code>${esc(r.dir)}</code> 를 만들고 음원을 넣어달라.<br>
             그때까지는 아래에 파일 경로를 직접 붙여넣으면 된다.</li>`);
  document.querySelectorAll("#files li[data-i]").forEach(li=>{
    const pick = ()=>{ picked = files[+li.dataset.i]; $("#path").value = picked.path;
      document.querySelectorAll("#files li").forEach(x=>x.setAttribute("aria-selected","false"));
      li.setAttribute("aria-selected","true"); estimate(); };
    li.onclick = pick;
    li.onkeydown = e => { if(e.key==="Enter"||e.key===" "){ e.preventDefault(); pick(); } };
  });
}

function estimate(){
  const p = $("#path").value.trim();
  $("#go").disabled = !p;
  const d = picked && picked.path === p ? picked.duration : 0;
  const k = SPEED[$("#model").value + "|" + ($("#device").value==="cuda"?"cuda":"cpu")] || 1;
  if (!d) { $("#est").innerHTML = "길이를 읽지 못했다. 예상 시간은 시작 후에 실측으로 표시된다."; return; }
  $("#est").innerHTML = `길이 <b>${hms(d)}</b> · 예상 소요 <b>약 ${hms(d/k)}</b>
    <span style="opacity:.7">(${$("#device").value==="cuda"?"GPU":"CPU"} 기준 어림값)</span>`;
}
["#path","#model","#device"].forEach(id=>$(id).addEventListener("input",estimate));

function drawTicks(dur){
  if(!dur) return;
  const step = dur > 5400 ? 1800 : dur > 1200 ? 600 : 60;
  let h = "";
  for (let t = step; t < dur; t += step){
    const pct = t/dur*100, isHour = t % 3600 === 0;
    h += `<i class="${isHour?"hour":""}" style="left:${pct}%"></i>`;
    if (isHour) h += `<b style="left:${pct}%">${t/3600}시간</b>`;
  }
  $("#ticks").innerHTML = h;
}

$("#go").onclick = async () => {
  const body = {
    path: $("#path").value.trim(),
    model: $("#model").value, device: $("#device").value, compute: $("#compute").value,
    lang: $("#lang").value, beam: +$("#beam").value, silence: +$("#silence").value,
    vad: $("#vad").checked, fallback: $("#fallback").checked, prompt: $("#prompt").checked,
    hotwords: $("#hotwords").value,
    diarize: $("#diarize").checked, nspk: +$("#nspk").value, sens: $("#sens").value,
    fixterms: $("#fixterms").checked,
    formats: { plain:$("#f_plain").checked, timed:$("#f_timed").checked,
               srt:$("#f_srt").checked, canon:$("#f_canon").checked }
  };
  if (!Object.values(body.formats).some(Boolean)){
    alert("저장 형식을 하나 이상 골라달라."); return; }
  const r = await (await fetch("/start",{method:"POST",body:JSON.stringify(body)})).json();
  if (r.error){ alert(r.error); return; }
  $("#setup").classList.add("hide"); $("#live").classList.remove("hide");
  lastSeg = 0; poll();
};

$("#diarize").onchange = async e => {
  const on = e.target.checked;
  $("#nspk_wrap").hidden = !on; $("#sens_wrap").hidden = !on; $("#dia_note").hidden = !on;
  if (!on){ $("#go").disabled = !$("#path").value.trim(); return; }
  $("#f_canon").checked = true;
  $("#dia_note").textContent = "확인하는 중…";
  const r = await (await fetch("/diacheck")).json();
  $("#dia_note").textContent = r.why;
  $("#dia_note").classList.toggle("bad", !r.ok);
};
$("#refresh").onclick = () => { picked = null; $("#path").value = ""; loadFiles(); estimate(); };
$("#stop").onclick = () => fetch("/cancel",{method:"POST"});
$("#again").onclick = () => { $("#live").classList.add("hide");
  $("#setup").classList.remove("hide"); loadFiles(); };

async function poll(){
  const j = await (await fetch("/status")).json();
  const pct = j.duration ? Math.min(100, j.processed/j.duration*100) : 0;

  $("#rate").innerHTML = (j.speed ? j.speed.toFixed(1) : "—") + "<small>× 실시간</small>";
  $("#s_left").textContent = j.state==="running" && j.eta ? hms(j.eta) : "—";
  $("#s_el").textContent = hms(j.elapsed);
  $("#s_ch").textContent = j.chars.toLocaleString();
  $("#fill").style.width = pct + "%";
  $("#c_now").textContent = hms(j.processed);
  $("#c_pct").textContent = pct.toFixed(1) + "%";
  $("#c_end").textContent = j.duration ? hms(j.duration) : "길이 미상";
  if (j.duration && !$("#ticks").innerHTML) drawTicks(j.duration);

  $("#phase").textContent = j.phase || "";
  if (j.state==="diarizing" && j.dia_pct) $("#phase").textContent = j.phase + " " + j.dia_pct.toFixed(0) + "%";
  if (j.speakers) $("#phase").textContent += `  ·  화자 ${j.speakers}명`;

  if (j.tail.length){
    $("#tail").innerHTML = j.tail.map((s,i)=>
      `<p class="${i>=j.tail.length-(j.segments-lastSeg)?"new":""}">
         <time>${s.t}</time>${s.s?`<span class="spk s${s.s}">화자${s.s}</span>`:``}
         <span>${esc(s.x)}</span></p>`).join("");
    $("#tail").scrollTop = $("#tail").scrollHeight;
    lastSeg = j.segments;
  }

  if (j.message){
    $("#err").textContent = j.message;
    $("#err").classList.remove("hide");
    $("#err").classList.toggle("ok", j.state === "done" && j.speakers > 0);
  }

  if (["done","cancelled","error"].includes(j.state)){
    $("#phase").textContent = j.state==="done"
      ? (j.speakers ? `화자 ${j.speakers}명으로 나눴다` : "완료 · 화자 분리 없음") : "";
    $("#stop").classList.add("hide"); $("#again").classList.remove("hide");
    if (j.outputs.length){
      $("#out").innerHTML = "저장 위치<br>" +
        j.outputs.map(o=>`<a href="/open?p=${encodeURIComponent(o.path)}" target="_blank">${o.path}</a>`).join("<br>");
      $("#out").classList.remove("hide");
    }
    return;
  }
  setTimeout(poll, 900);
}

loadFiles();
</script></div></body></html>
"""


# ─────────────────────────────────────────────────────────────
# 서버
# ─────────────────────────────────────────────────────────────

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
                    .replace("__DEVICES__", json.dumps(DEVICES))
                    .replace("__COMPUTES__", json.dumps(COMPUTES))
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
                    "settings": SETTINGS, "version": APP_VERSION})

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
            hid = self._body().get("id", "")
            with STATE_LOCK:
                rec = next((x for x in HISTORY["items"] if x["id"] == hid), None)
            if rec is None:
                return self._json({"error": "기록을 찾지 못했다."})
            b = self._body()
            settings = dict(rec.get("settings") or DEFAULT_OPT)
            settings.update(b.get("settings") or {})     # 이름·용어를 보태 다시 돌린다
            return self._json(enqueue([rec["path"]], settings,
                                      b.get("outdir") or rec.get("outdir") or OUTDIR))

        if u.path == "/settings":
            b = self._body()
            with STATE_LOCK:
                if "last" in b:
                    SETTINGS["last"] = dict(DEFAULT_OPT, **b["last"])
                if "presets" in b:
                    SETTINGS["presets"] = b["presets"]
                save_settings()
                return self._json(SETTINGS)

        if u.path == "/outdir/check":
            return self._json(check_outdir(self._body().get("path", "")))

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

    busy = port_busy(PORT)
    if busy:
        log(f"\n  !! {PORT} 포트를 이미 {busy} 가 쓰고 있다.")
        log(f"     그쪽이 화면에 응답하므로 이 실행은 반영되지 않는다.\n")
        log(f"     이전 창에서 Ctrl+C 로 끄거나, 명령창에서 다음을 실행해달라.")
        log(f"       taskkill /F /IM python.exe\n")
        log(f"     또는 다른 포트로 띄운다.")
        log(f"       set PORT=8766 && python app.py\n")
        return

    load_env()
    os.makedirs(OUTDIR, exist_ok=True)
    load_state()
    threading.Thread(target=queue_loop, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}"
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
    with STATE_LOCK:
        n_wait = sum(1 for x in QUEUE["items"] if x.get("state") == "waiting")
        n_int = sum(1 for x in QUEUE["items"] if x.get("state") == "interrupted")
        n_hist = len(HISTORY["items"])
    log(f"  대기열 — 대기 {n_wait}건"
        + (f" · 중단됨 {n_int}건" if n_int else "") + f" · 이력 {n_hist}건")
    log(f"  종료하려면 이 창에서 Ctrl+C\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    with Server(("127.0.0.1", PORT), Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            log("  종료했다.\n")
    log(f"── 받아쓰기 v{APP_VERSION} 종료")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
