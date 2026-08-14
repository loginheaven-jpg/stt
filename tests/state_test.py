#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
state_test.py — 작업이 끝맺어지는 자리를 본다.

    python tests/state_test.py

모델을 가짜로 바꿔 끼워 실패를 흉내 낸다. **음원이 필요 없다.**

종료 상태에서 phase 가 남으면 화면이 진행 중으로 보인다. 오류 띠가 떠 있는데
"받아쓰는 중" 이 함께 남고 실패한 작업에 중단 버튼이 붙는다. 실제로 겪었고
2-0단계에서 고친 자리다. 여기가 무너지면 그 증상이 그대로 돌아온다.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp(prefix="stt_state_test_")
os.environ["DATADIR"] = os.path.join(WORK, "data")
os.environ["OUTDIR"] = os.path.join(WORK, "out")
os.environ["PORT"] = os.environ.get("TESTPORT", "8794")
os.makedirs(os.environ["OUTDIR"], exist_ok=True)
sys.path.insert(0, BASE)
os.chdir(BASE)
import app  # noqa: E402

app.webbrowser.open = lambda *a, **k: None
PORT = os.environ["PORT"]
REAL_GET_WHISPER = app.get_whisper      # 갈아 끼운 뒤 되돌리려면 원본을 붙들어야 한다

# 전사 대상. 내용은 보지 않는다. 가짜 모델이 대신 답한다.
FAKE = os.path.join(WORK, "가짜음원.wav")
with open(FAKE, "wb") as f:
    f.write(b"\0" * 64)

# 기본은 cpu 다. auto 로 두면 §11 의 대체 경로가 다른 절에까지 끼어든다.
OPT = dict(app.DEFAULT_OPT, diarize=False, fixterms=False, device="cpu",
           formats={"plain": True, "timed": False, "srt": False, "canon": False})

ok, bad, skipped = [], [], []


def check(name, cond, detail=""):
    (ok if cond else bad).append(name)
    print(f"  {'통과' if cond else '실패'}  {name}" + (f"   {detail}" if detail else ""))


def post(p, b):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{p}",
                                 data=json.dumps(b).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def get(p):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{p}", timeout=30) as r:
        return json.loads(r.read())


class Seg:
    def __init__(self, s, e, t):
        self.start, self.end, self.text, self.words = s, e, t, None


class Info:
    def __init__(self, d):
        self.duration = d


class FakeModel:
    """구간을 내주다가 원하면 터진다. 전사 중 실패를 흉내 내는 물건이다."""

    def __init__(self, segs, duration, boom=None):
        self.segs, self.duration, self.boom = segs, duration, boom

    def transcribe(self, path, **kw):
        def gen():
            for s in self.segs:
                yield s
            if self.boom:
                raise self.boom
        return gen(), Info(self.duration)


def use(model=None, raise_on_load=None):
    """app.get_whisper 를 갈아 끼운다. (모델, 캐시적중, 실제기기, 실제정밀도)"""
    def fake(name, device, compute):
        if raise_on_load:
            raise raise_on_load
        return model, False, ("cpu" if device == "auto" else device), compute
    app.get_whisper = fake


def run_one(timeout=60):
    post("/queue/add", {"paths": [FAKE], "settings": OPT,
                        "outdir": os.environ["OUTDIR"]})
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = get("/state")
        pend = [x for x in s["queue"] if x["state"] in ("waiting", "running")]
        if not pend and s["job"]["state"] in app.TERMINAL:
            return s["job"]
        time.sleep(0.2)
    return get("/state")["job"]


threading.Thread(target=app.main, daemon=True).start()
time.sleep(2.0)
print(f"\n  받아쓰기 v{app.APP_VERSION} · 종료 상태 시험\n")

print("■ 1. 모델을 못 만들면\n")
use(raise_on_load=RuntimeError("Library cublas64_12.dll is not found or cannot be loaded"))
j = run_one()
check("state 가 error", j["state"] == "error", j["state"])
check("phase 가 비었다", j["phase"] == "", repr(j["phase"]))
check("eta 가 0", j["eta"] == 0)
check("어느 파일인지 밝힌다", "가짜음원.wav" in j["message"])
check("무엇을 하면 되는지 알린다", "→" in j["message"])
check("GPU 안내를 짚는다", "기기를 cpu" in j["message"], j["message"].replace("\n", " ")[:90])

print("\n■ 2. 새로 고쳐도 같은 상태다\n")
again = get("/state")["job"]
check("state 유지", again["state"] == "error")
check("phase 유지", again["phase"] == "")
check("message 유지", again["message"] == j["message"])

# 실행기가 0.4초마다 놀이 분기를 돈다. 거기서 종료 상태를 건드리면
# 알림이 저 혼자 사라진다. 사람이 읽기도 전에 없어지면 아무 소용이 없다.
# 종료 상태를 푸는 것은 [닫기] 와 다음 작업뿐이다.
time.sleep(1.6)
held = get("/state")["job"]
check("가만 둬도 종료 상태가 저 혼자 사라지지 않는다",
      held["state"] == "error", f"1.6초 뒤 {held['state']}")
check("사유도 그대로 남는다", held["message"] == j["message"])
check("hid 도 그대로 남는다", held["hid"] == again["hid"])

print("\n■ 3. 끝난 작업에서 버튼이 갈린다\n")
check("이력으로 내려가 hid 가 붙는다", bool(again["hid"]))
h = get("/state")["history"]
check("기록에 실패로 남는다", h and h[0]["state"] == "error")
check("대기열은 비었다", get("/state")["queue"] == [])

print("\n■ 4. 다시 시도\n")
post("/queue/stopall", {"on": True})
r = post("/history/again", {"id": again["hid"]})
check("이력 경로로 다시 담긴다", bool(r.get("ok")))
check("대기열에 들어왔다", len(get("/state")["queue"]) == 1)
for x in get("/state")["queue"]:
    post("/queue/remove", {"id": x["id"]})
post("/queue/stopall", {"on": False})

print("\n■ 5. 알림 닫기\n")
r = post("/job/dismiss", {})
check("닫힌다", bool(r.get("ok")))
j = get("/state")["job"]
check("idle 로 돌아간다", j["state"] == "idle", j["state"])
check("메시지가 사라진다", j["message"] == "")

print("\n■ 6. 전사 도중에 터지면\n")
use(FakeModel([Seg(0.0, 3.0, "앞부분은 받아썼다")], 12.0,
              boom=RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")))
j = run_one()
check("state 가 error", j["state"] == "error", j["state"])
check("phase 가 비었다", j["phase"] == "")
check("전사 중이라고 밝힌다", "전사 중 멈췄다" in j["message"])
check("GPU 안내를 짚는다", "기기를 cpu" in j["message"])
check("멈춘 지점을 남긴다", j["processed"] == 3.0, str(j["processed"]))
check("길이는 유지한다", j["duration"] == 12.0)
post("/job/dismiss", {})

print("\n■ 7. 정상 완료면 막대가 100% 에 닿는다\n")
# 마지막 구간이 11초에 끝나고 음원은 12초다. 옛 코드는 여기서 91.7% 로 멈췄다.
use(FakeModel([Seg(0.0, 5.0, "첫째 구간"), Seg(5.0, 11.0, "둘째 구간")], 12.0))
j = run_one()
check("state 가 done", j["state"] == "done", j["state"])
check("phase 가 비었다", j["phase"] == "")
check("processed 가 duration 과 같다", j["processed"] == j["duration"],
      f"{j['processed']} / {j['duration']}")
check("백분율이 100", round(j["processed"] / j["duration"] * 100, 1) == 100.0)
check("산출물이 있다", bool(j["outputs"]))
post("/job/dismiss", {})

print("\n■ 8. 길이를 모르면 백분율을 만들지 않는다\n")
use(FakeModel([Seg(0.0, 4.0, "길이 미상")], 0.0))
j = run_one()
check("duration 이 0", j["duration"] == 0.0)
check("끝나도 processed 를 부풀리지 않는다", j["processed"] == 4.0, str(j["processed"]))
post("/job/dismiss", {})

print("\n■ 9. 받아쓴 내용이 없으면\n")
use(FakeModel([], 12.0))
j = run_one()
check("state 가 error", j["state"] == "error", j["state"])
check("phase 가 비었다", j["phase"] == "")
check("무엇을 하면 되는지 알린다", "무음 기준" in j["message"])
post("/job/dismiss", {})

print("\n■ 10. 실패해도 대기열이 이어진다\n")
# 첫 호출만 터지게 한다. 시점을 폴링에 맡기면 두 번째가 먼저 시작해 어긋난다.
calls = {"n": 0}


def once_boom(name, device, compute):
    calls["n"] += 1
    if calls["n"] == 1:
        return (FakeModel([Seg(0.0, 3.0, "가")], 12.0,
                          boom=RuntimeError("일부러 낸 오류")), False, "cpu", compute)
    return FakeModel([Seg(0.0, 6.0, "나")], 12.0), False, "cpu", compute


app.get_whisper = once_boom
post("/queue/stopall", {"on": True})
post("/queue/add", {"paths": [FAKE], "settings": OPT, "outdir": os.environ["OUTDIR"]})
post("/queue/add", {"paths": [FAKE], "settings": OPT, "outdir": os.environ["OUTDIR"]})
post("/queue/stopall", {"on": False})
t0 = time.time()
while time.time() - t0 < 60:
    s = get("/state")
    if not [x for x in s["queue"] if x["state"] in ("waiting", "running")]:
        break
    time.sleep(0.2)
check("모델을 두 번 만들었다", calls["n"] == 2, str(calls["n"]))
states = [x["state"] for x in reversed(get("/state")["history"][:2])]
check("첫째가 실패해도 둘째가 돈다", states == ["error", "done"], str(states))
check("마지막 화면은 둘째 것이다", get("/state")["job"]["state"] == "done")

CUDA_ERR = "Library cublas64_12.dll is not found or cannot be loaded"

print("\n■ 11. auto 인데 CUDA 가 안 되면 CPU 로 다시 시작한다\n")
# 진짜 사고는 모델 생성이 아니라 전사 도중에 났다. 여기서도 그렇게 흉내 낸다.
seen = []


def gw_loop_boom(name, device, compute):
    seen.append(device)
    if device != "cpu":
        return (FakeModel([Seg(0.0, 3.0, "GPU가 남긴 반쪽")], 12.0,
                          boom=RuntimeError(CUDA_ERR)), False, "cuda", compute)
    return (FakeModel([Seg(0.0, 5.0, "CPU 첫째"), Seg(5.0, 11.0, "CPU 둘째")], 12.0),
            False, "cpu", "int8")


app.get_whisper = gw_loop_boom
OPT_AUTO = dict(OPT, device="auto")
post("/queue/add", {"paths": [FAKE], "settings": OPT_AUTO, "outdir": os.environ["OUTDIR"]})
t0 = time.time()
while time.time() - t0 < 60:
    s = get("/state")
    if not [x for x in s["queue"] if x["state"] in ("waiting", "running")] \
            and s["job"]["state"] in app.TERMINAL:
        break
    time.sleep(0.2)
j = get("/state")["job"]
check("완주한다", j["state"] == "done", j["state"])
check("기기가 cpu 로 남는다", j["device"] == "cpu", j["device"])
check("전환 사유를 남긴다", "CPU로 다시 시작" in j["device_note"], j["device_note"][:70])
check("cuda 를 먼저 시도하고 cpu 로 갔다", seen == ["auto", "cpu"], str(seen))

body = open(os.path.join(os.environ["OUTDIR"], "가짜음원.txt"), encoding="utf-8").read()
check("GPU 가 남긴 반쪽이 지워졌다", "GPU가 남긴 반쪽" not in body, repr(body[:60]))
check("CPU 판만 남는다", "CPU 첫째" in body and "CPU 둘째" in body)
check("이력에도 기기가 남는다", get("/state")["history"][0]["device"] == "cpu")
post("/job/dismiss", {})

print("\n■ 12. cuda 를 명시로 고르면 내려가지 않는다\n")
seen.clear()
app.get_whisper = gw_loop_boom
post("/queue/add", {"paths": [FAKE], "settings": dict(OPT, device="cuda"),
                    "outdir": os.environ["OUTDIR"]})
t0 = time.time()
while time.time() - t0 < 60:
    s = get("/state")
    if not [x for x in s["queue"] if x["state"] in ("waiting", "running")] \
            and s["job"]["state"] in app.TERMINAL:
        break
    time.sleep(0.2)
j = get("/state")["job"]
check("오류로 끝난다", j["state"] == "error", j["state"])
check("CPU 로 내려가지 않았다", seen == ["cuda"], str(seen))
check("전사 중이라고 밝힌다", "전사 중 멈췄다" in j["message"])
check("GPU 안내를 짚는다", "기기를 cpu" in j["message"])
post("/job/dismiss", {})

print("\n■ 13. 재시작은 한 번뿐이다\n")
seen.clear()


def gw_always_boom(name, device, compute):
    seen.append(device)
    return (FakeModel([Seg(0.0, 3.0, "조각")], 12.0, boom=RuntimeError(CUDA_ERR)),
            False, device, compute)


app.get_whisper = gw_always_boom
post("/queue/add", {"paths": [FAKE], "settings": OPT_AUTO, "outdir": os.environ["OUTDIR"]})
t0 = time.time()
while time.time() - t0 < 60:
    s = get("/state")
    if not [x for x in s["queue"] if x["state"] in ("waiting", "running")] \
            and s["job"]["state"] in app.TERMINAL:
        break
    time.sleep(0.2)
j = get("/state")["job"]
check("두 번째도 실패하면 오류다", j["state"] == "error", j["state"])
check("판은 둘까지만 돈다", seen == ["auto", "cpu"], str(seen))
post("/job/dismiss", {})

print("\n■ 14. 캐시 키에 실제 기기가 들어간다\n")
app.get_whisper = REAL_GET_WHISPER      # 앞 절의 가짜를 걷어낸다. 진짜를 시험한다
import faster_whisper  # noqa: E402

real_model = faster_whisper.WhisperModel
made = []


class StubModel:
    def __init__(self, name, device=None, compute_type=None):
        made.append((name, device, compute_type))
        if device == "cuda":
            raise RuntimeError(CUDA_ERR)


faster_whisper.WhisperModel = StubModel
try:
    app._WHISPER.update(key=None, obj=None)
    _, cached1, dev1, comp1 = app.get_whisper("모형", "auto", "float16")
    check("auto 가 cpu 로 내려간다", (dev1, comp1) == ("cpu", "int8"), f"{dev1}/{comp1}")
    check("정밀도도 함께 내린다", comp1 == "int8")
    check("키가 실제 기기로 잡힌다", app._WHISPER["key"] == ("모형", "cpu", "int8"),
          str(app._WHISPER["key"]))
    n_before = len(made)
    _, cached2, dev2, _ = app.get_whisper("모형", "auto", "float16")
    check("다음 항목은 캐시에서 나온다", cached2 is True)
    check("CUDA 를 다시 시도하지 않는다", len(made) == n_before, f"{len(made)-n_before}회 더 만들었다")
finally:
    faster_whisper.WhisperModel = real_model
    app._WHISPER.update(key=None, obj=None)

print("\n■ 15. CUDA DLL 경로 등록\n")
app.register_cuda_dlls._done = False
app.CUDA_DLL_DIRS.clear()
before = os.environ.get("PATH", "")
dirs = app.register_cuda_dlls()
check("예외 없이 끝난다", isinstance(dirs, list))
check("두 번 불러도 다시 하지 않는다", app.register_cuda_dlls() is dirs)
if dirs:
    check("찾은 폴더가 PATH 앞에 붙는다", all(d in os.environ["PATH"] for d in dirs),
          " · ".join(os.path.basename(os.path.dirname(d)) for d in dirs))
    check("실제로 있는 폴더다", all(os.path.isdir(d) for d in dirs))
    check("손잡이를 붙들고 있다", len(app._DLL_HANDLES) > 0 or os.name != "nt")
else:
    skipped.append("DLL 경로")
    print("  건너뜀 DLL 경로   이 PC 에서 찾지 못했다")

print("\n■ 16. GPU 탐침 — 고장 난 상태를 재현하는가\n")
# 실패하지 않는 탐침은 쓸모가 없다. 갈래마다 실제로 실패하는지 본다.
app.get_whisper = REAL_GET_WHISPER
import wave  # noqa: E402

check("탐침 음원이 있다", os.path.isfile(app.PROBE_WAV), app.PROBE_WAV)
if os.path.isfile(app.PROBE_WAV):
    with wave.open(app.PROBE_WAV) as wf:
        sec = wf.getnframes() / wf.getframerate()
        ch, sr = wf.getnchannels(), wf.getframerate()
    check("2~3초다", 2.0 <= sec <= 3.5, f"{sec:.2f}초")
    check("16kHz 모노다", (ch, sr) == (1, 16000), f"{ch}채널 {sr}Hz")
    check("저장소에 넣을 만한 크기다", os.path.getsize(app.PROBE_WAV) < 200_000,
          f"{os.path.getsize(app.PROBE_WAV):,}바이트")


def reset_gpu(count=1):
    app.GPU.update(count=count, usable=None, why="", name="")
    app._WHISPER.update(key=None, obj=None)
    app._AUTO_DEV[0] = None


class BoomOnTranscribe:
    """모델은 만들어지는데 추론에서 터진다. 진짜 사고가 이 모양이었다."""

    def __init__(self, name, device=None, compute_type=None):
        pass

    def transcribe(self, path, **kw):
        raise RuntimeError(CUDA_ERR)


real_model2 = faster_whisper.WhisperModel
faster_whisper.WhisperModel = BoomOnTranscribe
try:
    reset_gpu(1)
    r = app.gpu_probe()
    check("추론에서 터지면 탐침이 실패한다", r["ok"] is False, str(r.get("why", ""))[:60])
    check("배지가 'GPU 있으나 사용 불가' 로 바뀐다",
          app.gpu_badge()["state"] == "broken", app.gpu_badge()["label"])
    check("사유를 남긴다", "cublas" in app.gpu_badge()["why"])
finally:
    faster_whisper.WhisperModel = real_model2

reset_gpu(0)
r = app.gpu_probe()
check("GPU 가 없으면 탐침을 돌리지 않는다",
      r["ok"] is False and "찾지 못했다" in r["why"], r.get("why", ""))
check("배지가 'CPU만 사용'", app.gpu_badge()["state"] == "cpu", app.gpu_badge()["label"])

app.GPU.update(count=None, usable=None, why="")
app._WHISPER.update(key=None, obj=None)
if app.gpu_count() > 0:
    r = app.gpu_probe()
    check("실제 탐침이 통과한다", r["ok"] is True,
          f"{r.get('sec')}초 · {r.get('device')} · 구간 {r.get('segments')}개")
    if r["ok"]:
        check("배지가 'GPU 사용 가능'", app.gpu_badge()["state"] == "gpu")
        check("구간이 실제로 나온다", r.get("segments", 0) >= 1)

    # 무음을 탐침으로 쓰지 않는 이유.
    #
    # 실측 기록 — 이 PC 에서 CUDA 라이브러리 16개는 **import 시점에** 이미 올라온다.
    # import faster_whisper 가 torch 를 끌어오고 torch 가 즉시 올리기 때문이다.
    # 모델 생성에서도, 무음 추론에서도, 실제 음성 추론에서도 그 수는 16 그대로다.
    # 즉 "무음은 CUDA 를 안 부른다" 는 이 기계에서 성립하지 않는다.
    #
    # 그래도 탐침은 실제 음성이어야 한다. 무음이 내는 것은 환각이라 통과·실패를
    # 가릴 근거가 되지 못한다. 아래가 그것을 보인다.
    import numpy as np  # noqa: E402
    m, _, _, _ = app.get_whisper(app.DEFAULT_OPT["model"], "cuda", "int8")
    quiet = [s.text.strip() for s in
             m.transcribe(np.zeros(16000 * 3, dtype="float32"),
                          language="ko", vad_filter=False)[0]]
    spoken = [s.text.strip() for s in
              m.transcribe(app.PROBE_WAV, language="ko", vad_filter=False)[0]]
    print(f"     무음 3초  → {quiet}")
    print(f"     탐침 2.8초 → {spoken}")
    check("탐침은 알아들을 말을 낸다", any("안녕" in t for t in spoken), str(spoken))
    check("무음이 내는 것은 탐침의 답과 다르다", quiet != spoken, str(quiet))

else:
    for n in ("실제 탐침", "무음 대조"):
        skipped.append(n)
        print(f"  건너뜀 {n}   이 PC 에서 GPU 를 찾지 못했다")

print(f"\n{'=' * 60}")
print(f"  통과 {len(ok)} · 실패 {len(bad)}"
      + (f" · 건너뜀 {len(skipped)}" if skipped else ""))
for b in bad:
    print(f"    실패 — {b}")
print(f"{'=' * 60}\n")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if bad else 0)
