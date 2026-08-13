#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_stt.py — 여러 STT 엔진에 같은 음원을 돌려 비교용 전사를 만든다.

stt_bench.py가 바로 채점할 수 있는 형식으로 저장한다.
    [MM:SS] 화자<TAB>발화내용

엔진별 설정은 정본화 규칙에 맞춰 고정돼 있다.
    화자 수 지정        --speakers N. 2인 코칭이면 2, 다인 녹취면 실제 인원
    간투사 필터 해제    원칙 1 말버릇 보존
    ITN 해제            숫자·영문 변환 없이 원발화 그대로
    문단 분할 해제      발화 단위 보존

준비
    pip install requests
    (선택) pip install faster-whisper      # whisper_local 트랙용

인증 정보는 환경변수로 넣는다. 코드에 적지 않는다.
    export RTZR_CLIENT_ID=...
    export RTZR_CLIENT_SECRET=...
    export CLOVA_INVOKE_URL=https://clovaspeech-gw.ncloud.com/external/v1/0000/xxxxx
    export CLOVA_SECRET_KEY=...

사용법
    python3 run_stt.py clip01.wav
    python3 run_stt.py clips/*.wav --engines rtzr_sommers,clova
    python3 run_stt.py clip01.wav --keywords 황미혜 스쿼트 에스컬레이터
    python3 run_stt.py --check                      # 인증 정보만 점검
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from typing import List, Dict, Optional, Tuple

try:
    import requests
except ImportError:
    print("requests가 필요하다.  pip install requests", file=sys.stderr)
    sys.exit(1)


RTZR_BASE = "https://openapi.vito.ai"

# 인증 정보를 담은 파일 이름. 앞에서부터 찾아 처음 발견한 것을 쓴다.
ENV_FILES = ("env.local", ".env.local", ".env", "env.txt")
LOADED_KEYS: List[str] = []      # env 파일에서 실제로 읽은 항목 이름
POLL_INTERVAL = 3.0      # 초
POLL_TIMEOUT = 900.0     # 초. 3분 구간이면 넉넉하다


# ─────────────────────────────────────────────────────────────
# 인증 정보 파일
# ─────────────────────────────────────────────────────────────

RE_ENV_LINE = re.compile(r"^[^A-Za-z_]*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
ENV_PREFIXES = ("export ", "$env:", "$Env:", "set ", "SET ")

# 한 줄에 KEY=값 이 여러 개 붙어 들어온 경우를 잡아낸다.
# 줄바꿈이 누락되면 앞 항목의 값이 뒤 항목을 통째로 삼킨다.
RE_KEY_AT = re.compile(r"(?:(?<=^)|(?<=\s))(?:\$[Ee]nv:|export |set |SET )?"
                       r"([A-Z][A-Z0-9_]{2,})\s*=")

# 파이썬이 줄바꿈으로 보지 않는 특수 구분자
ODD_BREAKS = ("\u2028", "\u2029", "\u0085")


def split_records(line: str) -> List[str]:
    """한 줄에 여러 KEY=값이 붙어 있으면 각각으로 쪼갠다."""
    starts = [m.start(1) for m in RE_KEY_AT.finditer(line)]
    if len(starts) <= 1:
        return [line]
    return [line[a:b].strip() for a, b in zip(starts, starts[1:] + [len(line)])]


def load_env_file(explicit: Optional[str] = None) -> Optional[str]:
    """
    KEY=VALUE 형식의 파일을 읽어 환경변수로 올린다.
    아래를 모두 관대하게 받는다.
        RTZR_CLIENT_ID=abc
        export RTZR_CLIENT_ID="abc"
        $env:CLOVA_SECRET_KEY="abc"       ← 파워셸 문법이 섞여 있어도 된다
        set CLOVA_INVOKE_URL=abc
    BOM, CRLF, 따옴표, 줄 끝 세미콜론을 모두 걷어낸다.
    이미 셸에 설정된 값은 덮어쓰지 않는다. 셸이 우선이다.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [explicit] if explicit else [
        c for name in ENV_FILES for c in (name, os.path.join(here, name))
    ]

    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8-sig") as f:      # 실제 BOM 제거
            raw_text = f.read()
        for odd in ODD_BREAKS:                            # 특수 줄바꿈도 줄바꿈으로
            raw_text = raw_text.replace(odd, "\n")

        merged = []
        for raw in raw_text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            for prefix in ENV_PREFIXES:
                if line.startswith(prefix):
                    line = line[len(prefix):].lstrip()
                    break
            records = split_records(line)
            if len(records) > 1:
                merged.append((raw[:24], len(records)))
            for rec in records:
                m = RE_ENV_LINE.match(rec)                # 앞에 붙은 잡문자도 제거
                if not m:
                    continue
                key, val = m.group(1), m.group(2).strip().rstrip(";").strip()
                # 여는 따옴표만 있고 닫히지 않은 줄이 흔하다. 짝을 따지지 않고 걷어낸다.
                val = val.strip("\"'").strip()
                if key:
                    LOADED_KEYS.append(key)
                    if key not in os.environ:
                        os.environ[key] = val

        for head, n in merged:
            print(f"   안내    한 줄에 항목 {n}개가 붙어 있어 나눠 읽었다 — {head}…")
            print(f"           env 파일에서 줄바꿈을 넣어 정리하는 편이 안전하다.")
        return path
    return None


# ─────────────────────────────────────────────────────────────
# 공통 출력 형식
# ─────────────────────────────────────────────────────────────

def fmt_time(sec: float) -> str:
    sec = max(0.0, sec)
    return f"{int(sec) // 60:02d}:{int(sec) % 60:02d}"


def write_transcript(path: str, utts: List[Tuple[float, str, str]]) -> None:
    """(시작초, 화자, 텍스트) 목록을 채점기 입력 형식으로 저장한다."""
    with open(path, "w", encoding="utf-8") as f:
        for start, spk, text in utts:
            text = " ".join(text.split())
            if not text:
                continue
            f.write(f"[{fmt_time(start)}]\t{spk}\t{text}\n")


# ─────────────────────────────────────────────────────────────
# 엔진 1·2 — RTZR STT (sommers / whisper)
# ─────────────────────────────────────────────────────────────

_rtzr_token: Dict[str, object] = {"value": None, "exp": 0.0}


def rtzr_token() -> str:
    """액세스 토큰을 발급받아 캐시한다. 유효기간 내에는 재사용한다."""
    if _rtzr_token["value"] and time.time() < float(_rtzr_token["exp"]):
        return str(_rtzr_token["value"])

    cid = os.environ.get("RTZR_CLIENT_ID")
    sec = os.environ.get("RTZR_CLIENT_SECRET")
    if not (cid and sec):
        raise RuntimeError("RTZR_CLIENT_ID / RTZR_CLIENT_SECRET 미설정")

    r = requests.post(f"{RTZR_BASE}/v1/authenticate",
                      data={"client_id": cid, "client_secret": sec},
                      timeout=30)
    r.raise_for_status()
    body = r.json()
    _rtzr_token["value"] = body["access_token"]
    _rtzr_token["exp"] = time.time() + 60 * 50   # 여유를 두고 50분
    return body["access_token"]


def run_rtzr(audio: str, model: str, keywords: List[str],
             speakers: int) -> List[Tuple[float, str, str]]:
    token = rtzr_token()

    config = {
        "model_name": model,               # "sommers" 또는 "whisper"
        "use_diarization": True,
        "use_word_timestamp": True,
        "use_disfluency_filter": False,    # 원칙 1 간투사 보존
        "use_itn": False,                  # 원발화 그대로
        "use_profanity_filter": False,
        "use_paragraph_splitter": False,   # 발화 단위 보존
    }
    if speakers > 0:
        # 화자 수를 아는 경우에만 고정한다. 2인 코칭이면 2, 다인 녹취면 실제 인원.
        config["diarization"] = {"spk_count": speakers}
    if model == "whisper":
        config["language"] = "ko"          # whisper 모델은 언어 명시가 필요하다
    if keywords:
        config["keywords"] = keywords

    with open(audio, "rb") as fh:
        r = requests.post(
            f"{RTZR_BASE}/v1/transcribe",
            headers={"Authorization": f"bearer {token}"},
            data={"config": json.dumps(config, ensure_ascii=False)},
            files={"file": (os.path.basename(audio), fh)},
            timeout=120)
    r.raise_for_status()
    job_id = r.json()["id"]

    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        g = requests.get(f"{RTZR_BASE}/v1/transcribe/{job_id}",
                         headers={"Authorization": f"bearer {token}"},
                         timeout=60)
        g.raise_for_status()
        body = g.json()
        status = body.get("status")
        if status == "completed":
            break
        if status == "failed":
            raise RuntimeError(f"RTZR 전사 실패: {body}")
    else:
        raise TimeoutError("RTZR 응답 대기 초과")

    utts = []
    for u in body["results"]["utterances"]:
        utts.append((u["start_at"] / 1000.0, f"SPK_{u.get('spk', 0)}", u["msg"]))
    return utts


# ─────────────────────────────────────────────────────────────
# 엔진 3 — CLOVA Speech (NCP 장문 인식)
# ─────────────────────────────────────────────────────────────

def run_clova(audio: str, keywords: List[str],
              speakers: int) -> List[Tuple[float, str, str]]:
    invoke = os.environ.get("CLOVA_INVOKE_URL", "").rstrip("/")
    secret = os.environ.get("CLOVA_SECRET_KEY")
    if not (invoke and secret):
        raise RuntimeError("CLOVA_INVOKE_URL / CLOVA_SECRET_KEY 미설정")

    params = {
        "language": "ko-KR",
        "completion": "sync",
        "fullText": True,
        "wordAlignment": True,
        "diarization": {"enable": True},
    }
    if speakers > 0:
        params["diarization"]["speakerCountMin"] = speakers
        params["diarization"]["speakerCountMax"] = speakers
    if keywords:
        params["boostings"] = [{"words": ",".join(keywords)}]

    def post(as_part: bool):
        """
        params를 보내는 방식이 문서마다 다르다.
        1차 — 파트에 Content-Type을 붙여 보낸다 (일반적인 방식)
        2차 — 문자열로 보내고 type 필드를 따로 붙인다 (문서 curl 예시)
        """
        with open(audio, "rb") as fh:
            payload = json.dumps(params, ensure_ascii=False)
            if as_part:
                files = {
                    "media": (os.path.basename(audio), fh),
                    "params": (None, payload, "application/json"),
                }
                data = None
            else:
                files = {"media": (os.path.basename(audio), fh)}
                data = {"params": payload, "type": "application/json"}
            return requests.post(
                f"{invoke}/recognizer/upload",
                headers={"X-CLOVASPEECH-API-KEY": secret},
                files=files, data=data, timeout=900)

    r = post(True)
    if r.status_code == 400:
        r = post(False)          # 형식 문제일 수 있으니 한 번 더

    if r.status_code in (401, 403):
        raise RuntimeError(
            "CLOVA 인증 거부. 이 키가 CLOVA Speech(장문 인식)용인지 확인해달라. "
            "다른 NCP 서비스(CSR·Voice·Papago) 키로는 동작하지 않는다.")
    if r.status_code == 404:
        raise RuntimeError(
            "CLOVA Invoke URL이 잘못됐다. "
            "clovaspeech-gw.ncloud.com/external/v1/{앱ID}/{키} 형태여야 한다.")
    if r.status_code >= 400:
        raise RuntimeError(f"CLOVA {r.status_code}: {r.text[:250]}")
    body = r.json()

    result = body.get("result")
    if result and result not in ("COMPLETED", "SUCCEEDED"):
        raise RuntimeError(
            f"CLOVA 처리 실패 (result={result}): {body.get('message', '')[:150]}")
    if "segments" not in body:
        raise RuntimeError(f"CLOVA 응답에 segments 없음: {str(body)[:300]}")

    utts = []
    for s in body["segments"]:
        spk = s.get("speaker") or {}
        label = spk.get("label") or spk.get("name") or "SPK_0"
        utts.append((s["start"] / 1000.0, f"SPK_{label}", s["text"]))
    return utts


# ─────────────────────────────────────────────────────────────
# 엔진 4 — OpenAI 전사 API
# ─────────────────────────────────────────────────────────────

OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"

# 응답 형식을 앞에서부터 시도한다. 모델이 거부하면 다음으로 내려간다.
OPENAI_FORMATS = ("diarized_json", "verbose_json", "json")


def openai_key() -> str:  # noqa: C901
    """
    OPENAI_API_KEY를 쓴다.
    WHISPER_LOCAL 등 다른 이름에 sk- 로 시작하는 값이 들어 있으면 그것도 받아준다.
    whisper_local 트랙은 로컬 실행이라 키가 필요 없다. 이름이 헷갈리기 쉬워서 둔 장치다.
    """
    k = os.environ.get("OPENAI_API_KEY", "").strip()
    if k:
        return k
    for alias, v in os.environ.items():
        v = v.strip()
        if (v.startswith("sk-") and len(v) > 20
                and any(t in alias.upper() for t in ("OPENAI", "WHISPER", "GPT", "SK"))):
            print(f"   안내    {alias}에 담긴 OpenAI 키를 사용한다. "
                  f"OPENAI_API_KEY로 이름을 바꾸는 편이 낫다.")
            return v
    raise RuntimeError(
        "OPENAI_API_KEY 미설정. env.local에 OPENAI_API_KEY=sk-... 한 줄을 넣어달라.")


def run_openai(audio: str, keywords: List[str],
               speakers: int) -> List[Tuple[float, str, str]]:
    key = openai_key()
    model = os.environ.get("OPENAI_STT_MODEL", "gpt-4o-transcribe-diarize")

    # 화자분리 모델은 prompt를 받지 않는다. 붙이면 400으로 거부한다.
    # 따라서 이 모델에서는 키워드 유도를 쓸 수 없다. 비교 시 감안해야 한다.
    use_prompt = "diarize" not in model.lower()
    prompt = "코칭 녹음이다. 말버릇과 간투사를 그대로 적는다."
    if keywords:
        prompt += " 등장 이름: " + ", ".join(keywords) + "."

    def post(payload: dict):
        with open(audio, "rb") as fh:
            return requests.post(
                OPENAI_URL,
                headers={"Authorization": f"Bearer {key}"},
                data=payload,
                files={"file": (os.path.basename(audio), fh)},
                timeout=900)

    last_err = None
    for fmt in OPENAI_FORMATS:
        data = {"model": model, "language": "ko", "response_format": fmt}
        if use_prompt:
            data["prompt"] = prompt
        if fmt == "verbose_json":
            data["timestamp_granularities[]"] = "segment"

        # 지원하지 않는 파라미터를 지목하면 그것만 빼고 다시 보낸다
        for _ in range(4):
            r = post(data)

            if r.status_code == 401:
                raise RuntimeError("OpenAI 인증 거부. 키가 만료됐거나 잘못됐다.")
            if r.status_code == 429:
                raise RuntimeError(_openai_429(r))
            if r.status_code == 404:
                raise RuntimeError(
                    f"모델 '{model}'을 찾을 수 없다. OPENAI_STT_MODEL을 "
                    f"gpt-4o-transcribe 또는 whisper-1로 바꿔보라.")

            if r.status_code == 400:
                try:
                    err = r.json().get("error", {})
                except Exception:
                    err = {}
                bad = err.get("param")
                last_err = (err.get("message") or r.text)[:200]
                if bad and bad in data:
                    print(f"   안내    {model}이 '{bad}'를 지원하지 않아 빼고 재시도한다.")
                    del data[bad]
                    continue
                break                    # 형식 자체가 안 맞는다. 다음 형식으로
            if r.status_code >= 400:
                raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:250]}")

            return _parse_openai(r.json(), fmt)

    raise RuntimeError(f"OpenAI 요청 거부 ({model}): {last_err}")


def _openai_429(r) -> str:
    """
    429는 두 가지다. 잔액 부족과 호출 빈도 초과.
    응답 본문의 error.code로 가른다. 첫 호출에서 429면 대개 잔액 부족이다.
    """
    try:
        err = r.json().get("error", {})
    except Exception:
        err = {}
    code = err.get("code") or err.get("type") or ""
    msg = (err.get("message") or r.text)[:200]

    if "insufficient_quota" in code or "insufficient_quota" in msg:
        return ("OpenAI 크레딧 부족. platform.openai.com > Billing 에서 "
                "결제수단 등록과 크레딧 충전이 필요하다. 전사 API에는 무료 구간이 없다.")
    if "rate_limit" in code:
        return f"OpenAI 호출 빈도 초과. 잠시 뒤 다시 시도하면 된다. ({msg})"
    return f"OpenAI 429 ({code or '코드 없음'}): {msg}"


def _parse_openai(body: dict, fmt: str) -> List[Tuple[float, str, str]]:
    """diarized_json · verbose_json · json 어느 쪽이 와도 같은 형태로 되돌린다."""
    segs = body.get("segments") or body.get("chunks") or []
    if segs:
        out = []
        for s in segs:
            start = float(s.get("start", 0.0))
            spk = s.get("speaker") or s.get("speaker_id") or "SPK_0"
            text = (s.get("text") or "").strip()
            if text:
                out.append((start, f"SPK_{spk}".replace("SPK_SPK_", "SPK_"), text))
        if out:
            return out
    text = (body.get("text") or "").strip()
    if not text:
        raise RuntimeError(f"OpenAI 응답에 본문이 없다 (형식 {fmt})")
    return [(0.0, "SPK_0", text)]


# ─────────────────────────────────────────────────────────────
# 엔진 5 — Whisper large-v3 (로컬 기준선. API 키 불필요)
# ─────────────────────────────────────────────────────────────

def run_whisper_local(audio: str, keywords: List[str],
                      speakers: int) -> List[Tuple[float, str, str]]:
    """
    화자분리가 없는 기준선이다. 화자 지표는 의미가 없고
    CER과 간투사 보존율만 본다. 상용 API가 정말 나은지 확인하는 대조군이다.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError("faster-whisper 미설치.  pip install faster-whisper")

    device = os.environ.get("WHISPER_DEVICE", "auto")
    ctype = os.environ.get("WHISPER_COMPUTE", "int8")
    model = WhisperModel("large-v3", device=device, compute_type=ctype)

    prompt = "코칭 시연 녹취. 어, 음, 그, 이제 같은 말버릇을 그대로 적는다."
    if keywords:
        prompt += " " + " ".join(keywords)

    segments, _ = model.transcribe(
        audio,
        language="ko",
        beam_size=5,
        temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        condition_on_previous_text=False,   # 반복 환각 차단
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
        initial_prompt=prompt,
    )
    return [(s.start, "SPK_0", s.text) for s in segments]


# ─────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────

ENGINES = {
    "rtzr_sommers": lambda a, k, n: run_rtzr(a, "sommers", k, n),
    "rtzr_whisper": lambda a, k, n: run_rtzr(a, "whisper", k, n),
    "clova":        lambda a, k, n: run_clova(a, k, n),
    "openai":       lambda a, k, n: run_openai(a, k, n),
    "whisper_local": lambda a, k, n: run_whisper_local(a, k, n),
}

DEFAULT_ENGINES = "rtzr_sommers,rtzr_whisper,clova,openai,whisper_local"

# 비교 조건을 눈으로 확인하기 위한 요약. 실제 값은 각 run_* 함수에 있다.
ENGINE_SETTINGS = {
    "rtzr_sommers":  "model=sommers · 간투사필터 해제 · ITN 해제 · 문단분할 해제 · 단어타임스탬프 · 키워드부스팅",
    "rtzr_whisper":  "model=whisper · language=ko · 나머지 sommers와 동일",
    "clova":         "NEST 엔진 · 장문 인식 도메인 · 화자인식 사용 설정 필요 · wordAlignment · boostings",
    "openai":        "OPENAI_STT_MODEL (기본 gpt-4o-transcribe-diarize) · language=ko · 화자분리 모델은 prompt 미지원이라 키워드 유도 없음",
    "whisper_local": "large-v3 · VAD · condition_on_previous_text=False · 화자분리 없음",
}


EXPECTED_LEN = {          # 대략의 정상 범위. 벗어나면 경고만 띄운다
    "RTZR_CLIENT_ID": (16, 40),
    "RTZR_CLIENT_SECRET": (24, 64),
    "CLOVA_INVOKE_URL": (40, 140),
    "CLOVA_SECRET_KEY": (16, 64),
    "OPENAI_API_KEY": (40, 220),
}


def audit_value(name: str) -> Optional[str]:
    """값에 붙은 흔한 오염을 잡아낸다."""
    v = os.environ.get(name, "")
    if not v:
        return None
    if v[0] in "\"'" or v[-1] in "\"'":
        return "따옴표가 값에 섞였다"
    if any(c in v for c in " \t"):
        return "값에 공백이 있다"
    lo, hi = EXPECTED_LEN.get(name, (1, 10 ** 6))
    if not (lo <= len(v) <= hi):
        return f"길이 {len(v)}자. 정상 범위는 {lo}~{hi}자다"
    return None


def make_probe_wav() -> str:
    """2초짜리 시험 음원을 만든다. 외부 도구 없이 표준 라이브러리만 쓴다."""
    import math
    import struct
    import tempfile
    import wave

    path = os.path.join(tempfile.gettempdir(), "_stt_probe.wav")
    rate, dur = 16000, 2.0
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * dur)):
            # 220Hz 낮은 톤. 말소리가 아니므로 인식 결과는 비는 것이 정상이다.
            v = int(6000 * math.sin(2 * math.pi * 220 * i / rate))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))
    return path


# 사전 점검에서 "연결은 정상"으로 볼 오류 문구
OK_PATTERNS = ("본문이 없다", "segments 없음", "발화 없음", "결과가 비었")


def classify_probe_error(e: Exception) -> Tuple[bool, str]:
    """예외를 보고 연결 실패인지 단순 빈 결과인지 가른다."""
    msg = f"{type(e).__name__}: {e}"
    if any(k in msg for k in OK_PATTERNS):
        return True, "응답 정상 (시험음이라 인식 결과 없음)"
    return False, msg[:150]


def preflight(engines: List[str], speakers: int) -> int:
    """각 엔진을 실제로 한 번 호출해 통과 여부를 확인한다."""
    print("\n■ 사전 점검 — 2초 시험 음원으로 각 엔진을 실제 호출한다\n")
    probe = make_probe_wav()
    failed = 0

    for eng in engines:
        if eng == "whisper_local":
            try:
                import faster_whisper  # noqa: F401
                print(f"  {'준비됨':<6} {eng:<16} {'—':>7}   "
                      f"faster-whisper 확인. 최초 실행 시 모델을 내려받는다")
            except ImportError:
                failed += 1
                print(f"  {'실패':<6} {eng:<16} {'—':>7}   "
                      f"pip install faster-whisper")
            continue

        t0 = time.time()
        try:
            ENGINES[eng](probe, ["시험"], speakers)
            print(f"  {'통과':<6} {eng:<16} {time.time() - t0:6.1f}초   응답 정상")
        except Exception as e:
            ok, why = classify_probe_error(e)
            tag = "통과" if ok else "실패"
            failed += 0 if ok else 1
            print(f"  {tag:<6} {eng:<16} {time.time() - t0:6.1f}초   {why}")

    print()
    for eng in engines:
        print(f"  {eng:<16} {ENGINE_SETTINGS.get(eng, '')}")
    print()
    if failed:
        print(f"  {failed}개 엔진이 준비되지 않았다. 위 사유를 해결한 뒤 다시 점검해달라.\n")
    else:
        print("  모든 엔진이 준비됐다. 본 전사를 진행해도 된다.\n")
    return 1 if failed else 0


def check_credentials() -> None:
    rows = [
        ("rtzr_sommers / rtzr_whisper",
         bool(os.environ.get("RTZR_CLIENT_ID") and os.environ.get("RTZR_CLIENT_SECRET")),
         "RTZR_CLIENT_ID, RTZR_CLIENT_SECRET"),
        ("clova",
         bool(os.environ.get("CLOVA_INVOKE_URL") and os.environ.get("CLOVA_SECRET_KEY")),
         "CLOVA_INVOKE_URL, CLOVA_SECRET_KEY"),
    ]
    # 이름이 조금 달라도 sk- 로 시작하는 값이면 OpenAI 키로 받아준다
    oa = next((k for k, v in os.environ.items()
               if v.startswith("sk-") and len(v) > 20
               and any(t in k.upper() for t in ("OPENAI", "WHISPER", "GPT", "SK"))), None)
    rows.append(("openai", bool(oa), oa or "OPENAI_API_KEY"))
    if LOADED_KEYS:
        print(f"\n  env 파일에서 읽은 항목 {len(LOADED_KEYS)}개 — {', '.join(LOADED_KEYS)}")
    print()
    for name, ok, envs in rows:
        detail = ""
        if ok:
            shown = []
            for e in envs.split(", "):
                v = os.environ.get(e, "")
                shown.append(f"{e}={v[:6]}…({len(v)}자)" if len(v) > 8 else f"{e}={v}")
            detail = "  " + " · ".join(shown)
        print(f"  {'설정됨 ' if ok else '미설정 '}  {name:<28}  {envs}{detail}")
        for e in envs.split(", "):
            warn = audit_value(e)
            if warn:
                print(f"           확인 필요  {e} — {warn}")
    try:
        import faster_whisper  # noqa: F401
        print(f"  {'설정됨 '}  {'whisper_local':<28}  faster-whisper 설치 확인")
    except ImportError:
        print(f"  {'미설치 '}  {'whisper_local':<28}  pip install faster-whisper")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="여러 STT 엔진 일괄 전사")
    ap.add_argument("audio", nargs="*", help="음원 파일 (여러 개 또는 와일드카드)")
    ap.add_argument("--engines", default=DEFAULT_ENGINES,
                    help=f"쉼표 구분. 기본값: {DEFAULT_ENGINES}")
    ap.add_argument("--keywords", nargs="*", default=[],
                    help="키워드 부스팅에 넣을 이름·용어")
    ap.add_argument("--speakers", type=int, default=0,
                    help="화자 수 고정. 2인 코칭이면 2, 다인 녹취면 실제 인원. "
                         "0이면 엔진이 추정한다 (기본 0)")
    ap.add_argument("--outdir", default="out", help="출력 디렉터리 (기본 out)")
    ap.add_argument("--env", help="인증 정보 파일 경로 (기본: env.local · .env 자동 탐색)")
    ap.add_argument("--check", action="store_true", help="인증 정보만 점검")
    ap.add_argument("--preflight", action="store_true",
                    help="시험 음원으로 각 엔진을 실제 호출해 준비 상태를 확인")
    args = ap.parse_args()

    loaded = load_env_file(args.env)
    if loaded:
        print(f"인증 정보 파일 읽음: {loaded}")
    elif not os.environ.get("RTZR_CLIENT_ID"):
        print("인증 정보 파일을 찾지 못했다. "
              f"다음 중 하나를 스크립트와 같은 폴더에 두면 된다 — {', '.join(ENV_FILES)}")

    if args.check:
        check_credentials()
        return 0

    if args.preflight:
        check_credentials()
        eng = [e.strip() for e in args.engines.split(",") if e.strip()]
        return preflight(eng, args.speakers)

    files: List[str] = []
    for pat in args.audio:
        files.extend(sorted(glob.glob(pat)) or [pat])
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        if not args.audio:
            print("\n음원 파일을 지정하지 않았다. "
                  "명령이 여러 줄로 끊겨 들어오지 않았는지 확인해달라.\n", file=sys.stderr)
        else:
            print(f"\n지정한 파일을 찾지 못했다: {' '.join(args.audio)}\n", file=sys.stderr)
        ap.print_help()
        return 1

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    unknown = [e for e in engines if e not in ENGINES]
    if unknown:
        print(f"알 수 없는 엔진: {', '.join(unknown)}", file=sys.stderr)
        return 1

    os.makedirs(args.outdir, exist_ok=True)
    failures = 0

    for audio in files:
        stem = os.path.splitext(os.path.basename(audio))[0]
        print(f"\n■ {os.path.basename(audio)}")
        for eng in engines:
            out = os.path.join(args.outdir, f"{stem}__{eng}.txt")
            if os.path.exists(out):
                print(f"   건너뜀  {eng:<15} 이미 있음 → {out}")
                continue
            t0 = time.time()
            try:
                utts = ENGINES[eng](audio, args.keywords, args.speakers)
                write_transcript(out, utts)
                spks = len({s for _, s, _ in utts})
                print(f"   완료    {eng:<15} 발화 {len(utts):>4}건 · "
                      f"화자 {spks}개 · {time.time() - t0:5.1f}초 → {out}")
            except Exception as e:
                failures += 1
                print(f"   실패    {eng:<15} {type(e).__name__}: {e}")

    print(f"\n출력 디렉터리: {args.outdir}")
    print("채점:  python3 stt_bench.py 정답.txt "
          f"{args.outdir}/<구간>__*.txt --nouns 이름 용어 --detail\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
