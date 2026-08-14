#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup_test.py — 설치기 흐름 시험.

    python tests/setup_test.py

실제로 내려받거나 설치하지 않는다. 파이썬이 낡은 경우, 패키지가 하나도
없는 경우, 토큰을 건너뛴 경우 따위를 흉내 내 안내와 갈래가 맞는지 본다.
env.local 은 임시 폴더에만 쓴다. 진짜 파일은 건드리지 않는다.
"""
import builtins
import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ok, bad = [], []


def check(name, cond, detail=""):
    (ok if cond else bad).append(name)
    print(f"  {'통과' if cond else '실패'}  {name}" + (f"   {detail}" if detail else ""))


def load():
    spec = importlib.util.spec_from_file_location("setup_mod",
                                                  os.path.join(BASE, "setup.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def run(m, answers, work):
    """answers 를 차례로 input() 에 물린다. 화면 출력을 문자열로 돌려준다."""
    it = iter(answers)
    m.ENV_PATH = os.path.join(work, "env.local")
    m.BASE = work
    m.steps.clear()
    orig = builtins.input
    builtins.input = lambda p="": next(it, "")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            m.main()
    finally:
        builtins.input = orig
    return buf.getvalue(), list(m.steps)


print("\n■ 1. 파이썬이 낡은 경우 — 여기서 멈춘다\n")
m = load()


class FakeVer(tuple):
    major, minor, micro = 3, 9, 0


m.sys = type("S", (), {"version_info": FakeVer((3, 9, 0)),
                       "executable": sys.executable, "path": sys.path})()
work = tempfile.mkdtemp()
out, st = run(m, [""], work)
check("다운로드 주소를 알린다", m.PY_URL in out)
check("Add Python to PATH 안내", "Add Python to PATH" in out)
check("여기서 멈춘다", len(st) == 1 and st[0][1] == "fail")
check("폴더를 만들지 않았다", not os.path.isdir(os.path.join(work, "audio")))
shutil.rmtree(work, ignore_errors=True)

print("\n■ 2. 패키지가 하나도 없는 경우 — 안내하고 계속 간다\n")
m = load()
m.have = lambda mod: ""                       # 아무것도 안 깔린 척
m.pip_install = lambda *p: (False, "네트워크 없음")
m.step_shortcut = lambda: m.done("바로가기", False, "시험에서는 건너뛴다")
work = tempfile.mkdtemp()
out, st = run(m, ["y", "y", "hf_TESTTOKEN1234567890", "n", ""], work)
names = {n: s2 for n, s2, _ in st}
check("faster-whisper 실패를 알린다", names.get("faster-whisper") == "fail")
check("실패해도 다음 단계로 간다", "폴더" in names)
check("오류를 알려달라고 한다", "오류 내용을 그대로 알려주면" in out)
check("설치가 실패해도 토큰은 받아둔다", names.get("HF 토큰") == "ok")
check("env.local 이 생겼다", os.path.isfile(os.path.join(work, "env.local")))
check("폴더 셋을 만든다",
      all(os.path.isdir(os.path.join(work, d)) for d in ("audio", "out_text", "data")))
check("요약에 못 한 것이 보인다", "못 했다" in out)
check("진단 화면을 안내한다", "[진단]" in out)
shutil.rmtree(work, ignore_errors=True)

print("\n■ 3. 화자 분리를 쓰지 않기로 한 경우\n")
m = load()
m.have = lambda mod: "1.2.1" if mod == "faster_whisper" else ""
m.pip_install = lambda *p: (True, "")
m.step_shortcut = lambda: m.done("바로가기", False, "시험에서는 건너뛴다")
work = tempfile.mkdtemp()
out, st = run(m, ["n", "n", ""], work)
names = {n: s2 for n, s2, _ in st}
check("화자 분리를 건너뛴다", names.get("화자 분리") == "skip")
check("토큰을 묻지 않는다", "HF 토큰" not in names)
check("env.local 을 만들지 않는다", not os.path.isfile(os.path.join(work, "env.local")))
check("받아쓰기는 준비된다", names.get("faster-whisper") == "ok")
shutil.rmtree(work, ignore_errors=True)

print("\n■ 4. 토큰을 건너뛴 경우 — 기존 env.local 을 지키는가\n")
m = load()
m.have = lambda mod: "1.0"
m.pip_install = lambda *p: (True, "")
m.step_shortcut = lambda: m.done("바로가기", False, "시험에서는 건너뛴다")
work = tempfile.mkdtemp()
before = "RTZR_CLIENT_ID=keepme\nOPENAI_API_KEY=sk-keepme\n"
with open(os.path.join(work, "env.local"), "w", encoding="utf-8") as f:
    f.write(before)
out, st = run(m, ["y", "", "n", ""], work)
after = open(os.path.join(work, "env.local"), encoding="utf-8").read()
check("토큰을 건너뛰면 env.local 을 건드리지 않는다", after == before, repr(after))
check("화자 분리만 못 쓴다고 알린다", "화자 분리만 못 쓴다" in out)
shutil.rmtree(work, ignore_errors=True)

print("\n■ 5. 토큰 형식이 틀린 경우\n")
m = load()
m.have = lambda mod: "1.0"
m.pip_install = lambda *p: (True, "")
m.step_shortcut = lambda: m.done("바로가기", False, "시험에서는 건너뛴다")
work = tempfile.mkdtemp()
out, st = run(m, ["y", "abcd1234", "n", "n", ""], work)
names = {n: s2 for n, s2, _ in st}
check("hf_ 로 시작해야 한다고 알린다", "hf_ 로 시작해야 한다" in out)
check("거절하면 저장하지 않는다", names.get("HF 토큰") == "fail")
shutil.rmtree(work, ignore_errors=True)

print("\n■ 6. 모든 것이 갖춰진 경우\n")
m = load()
m.have = lambda mod: "1.0"
m.pip_install = lambda *p: (True, "")
m.step_shortcut = lambda: m.done("바로가기", True, "바탕화면")
work = tempfile.mkdtemp()
out, st = run(m, ["y", "hf_ABCDEFGHIJKLMNOPQRST", "n", ""], work)
check("못 한 것이 없다", not [n for n, s2, _ in st if s2 == "fail"],
      str([n for n, s2, _ in st if s2 == "fail"]))
check("일부러 건너뛴 것은 실패로 세지 않는다", "건너뛴 것 —" in out)
check("마무리 안내가 있다", "더블클릭하면 시작된다" in out)
check("음원 폴더를 알려준다", "audio 폴더에 넣는다" in out)
shutil.rmtree(work, ignore_errors=True)

print("\n■ 7. 화자 분리 모델 받기 — 갈래별로\n")
import types  # noqa: E402


def with_fake_pyannote(raiser):
    """pyannote.audio 를 잠깐 가짜로 바꿔 끼운다. 끝나면 되돌린다."""
    saved = {k: sys.modules.get(k) for k in ("pyannote", "pyannote.audio")}
    pkg = types.ModuleType("pyannote")
    mod = types.ModuleType("pyannote.audio")

    class Pipeline:
        @staticmethod
        def from_pretrained(*a, **k):
            return raiser()

    mod.Pipeline = Pipeline
    pkg.audio = mod
    sys.modules["pyannote"] = pkg
    sys.modules["pyannote.audio"] = mod
    return saved


def restore(saved):
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


m = load()
m.have = lambda mod: "1.0"

m.env_token = lambda: ""
m.steps.clear()
with contextlib.redirect_stdout(io.StringIO()):
    r = m.fetch_diarization("")
check("토큰이 없으면 건너뛴다", r is False and m.steps[-1][1] == "skip")

m.have = lambda mod: ""
m.steps.clear()
with contextlib.redirect_stdout(io.StringIO()):
    r = m.fetch_diarization("hf_x")
check("pyannote 가 없으면 건너뛴다", r is False and m.steps[-1][1] == "skip")

m.have = lambda mod: "1.0"
saved = with_fake_pyannote(lambda: (_ for _ in ()).throw(
    RuntimeError("401 Client Error. Access to model is gated.")))
m.steps.clear()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    r = m.fetch_diarization("hf_x")
restore(saved)
out = buf.getvalue()
check("약관 미동의를 짚어준다", "약관에 아직 동의하지 않았다" in out)
check("동의하러 갈 주소를 준다", m.DIA_MODEL in out)
check("받아쓰기는 된다고 알린다", "받아쓰기는 그대로 쓸 수 있다" in out)
check("실패로 센다", r is False and m.steps[-1][1] == "fail")

saved = with_fake_pyannote(lambda: object())
m.steps.clear()
with contextlib.redirect_stdout(io.StringIO()):
    r = m.fetch_diarization("hf_x")
restore(saved)
check("받아지면 통과", r is True and m.steps[-1][1] == "ok")

saved = with_fake_pyannote(lambda: None)
m.steps.clear()
with contextlib.redirect_stdout(io.StringIO()):
    r = m.fetch_diarization("hf_x")
restore(saved)
check("None 을 돌려주면 거부로 본다", r is False and m.steps[-1][1] == "fail")

m.env_token = lambda: "hf_from_env"
saved = with_fake_pyannote(lambda: object())
m.steps.clear()
with contextlib.redirect_stdout(io.StringIO()):
    r = m.fetch_diarization("")
restore(saved)
check("설치 화면에서 안 받았어도 env.local 토큰을 쓴다", r is True)

print("\n■ 8. 모델 단계가 둘을 다 다루는가\n")
m = load()
m.have = lambda mod: "1.0"
m.fetch_whisper = lambda: m.done("받아쓰기 모델", "ok", "가짜")
m.fetch_diarization = lambda t: m.done("화자 분리 모델", "ok", "가짜")
m.steps.clear()
it = iter(["y"])
builtins_input = builtins.input
builtins.input = lambda p="": next(it, "")
with contextlib.redirect_stdout(io.StringIO()):
    m.step_model(True, "hf_x")
builtins.input = builtins_input
names = [n for n, _, _ in m.steps]
check("받겠다고 하면 둘 다 받는다", names == ["받아쓰기 모델", "화자 분리 모델"], str(names))

m.steps.clear()
it = iter(["n"])
builtins.input = lambda p="": next(it, "")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.step_model(True, "hf_x")
builtins.input = builtins_input
check("건너뛰면 둘 다 건너뛴다",
      [s2 for _, s2, _ in m.steps] == ["skip", "skip"], str(m.steps))
check("화자 분리 크기를 미리 알린다", "31MB" in buf.getvalue())

m.steps.clear()
it = iter(["y"])
builtins.input = lambda p="": next(it, "")
with contextlib.redirect_stdout(io.StringIO()):
    m.step_model(False, "")
builtins.input = builtins_input
check("화자 분리를 안 쓰면 그 모델은 건드리지 않는다",
      [n for n, _, _ in m.steps] == ["받아쓰기 모델"], str(m.steps))

print("\n■ 9. 그래픽카드 단계\n")


class FakeApp:
    def __init__(self, smi):
        self._smi = smi
        self.PROBE_WAV = os.path.join(BASE, "tests", "probe.wav")

    def nvidia_smi(self):
        return self._smi

    def gpu_report(self, probe=False):
        return [("드라이버", "ok", "가짜"), ("시험 전사", "skip", "가짜")]


def gpu_case(smi, answers):
    m = load()
    m.load_app = lambda: FakeApp(smi)
    m.pip_install = lambda *a: (True, "")
    m.steps.clear()
    it = iter(answers)
    orig = builtins.input
    builtins.input = lambda p="": next(it, "")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            r = m.step_gpu()
    finally:
        builtins.input = orig
    return r, m.steps[-1] if m.steps else None, buf.getvalue()


NO_GPU = {"ok": False, "why": "nvidia-smi 가 없다. NVIDIA 드라이버가 깔려 있지 않다."}
OLD = {"ok": True, "cuda": "11.8", "name": "NVIDIA GeForce GTX 1060", "old": True,
       "why": "드라이버가 낡았다. CUDA 12.0 이상이 필요하다."}
GOOD = {"ok": True, "cuda": "12.6", "name": "NVIDIA GeForce RTX 3060", "old": False,
        "why": ""}

r, st, out = gpu_case(NO_GPU, [])
check("GPU 가 없으면 건너뛴다", r is False and st[1] == "skip", str(st))
check("CPU 로 동작한다고 알린다", "CPU 로 동작한다" in out)

r, st, out = gpu_case(OLD, [])
check("드라이버가 낡으면 못 했다로 센다", r is False and st[1] == "fail", str(st))
check("CUDA 12.0 이상이 필요하다고 알린다", "12.0 이상" in out)
check("무엇을 하면 되는지 알린다", "드라이버를 갱신" in out)

r, st, out = gpu_case(GOOD, ["y"])
check("GPU 가 있으면 받는다", r is True and st[1] == "ok", str(st))
check("용량과 이득을 미리 알린다", "700MB" in out and "20배" in out)

r, st, out = gpu_case(GOOD, ["n"])
check("싫다고 하면 건너뛴다 — 실패가 아니다", r is False and st[1] == "skip", str(st))

r, st, out = gpu_case(GOOD, [""])
check("Enter 만 쳐도 받는다", r is True and st[1] == "ok", str(st))

print("\n■ 10. 의존성 파일\n")
for f, must in (("requirements.txt", "faster-whisper>="),
                ("requirements-gpu.txt", "nvidia-cudnn-cu12>=9"),
                ("requirements.lock", "faster-whisper==")):
    p = os.path.join(BASE, f)
    body = open(p, encoding="utf-8").read() if os.path.isfile(p) else ""
    check(f"{f} 가 있다", bool(body))
    check(f"{f} 에 {must}", must in body)
check("설치용은 하한만 건다", "==" not in open(
    os.path.join(BASE, "requirements.txt"), encoding="utf-8").read())
for f in ("LICENSE", "NOTICE"):
    check(f"{f} 가 있다", os.path.isfile(os.path.join(BASE, f)))
check("NOTICE 가 모델 라이선스를 밝힌다",
      "CC-BY-4.0" in open(os.path.join(BASE, "NOTICE"), encoding="utf-8").read())

print(f"\n{'=' * 60}")
print(f"  통과 {len(ok)} · 실패 {len(bad)}")
for b in bad:
    print(f"    실패 — {b}")
print(f"{'=' * 60}\n")
sys.exit(1 if bad else 0)
