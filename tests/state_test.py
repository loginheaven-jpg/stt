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

# 전사 대상. 내용은 보지 않는다. 가짜 모델이 대신 답한다.
FAKE = os.path.join(WORK, "가짜음원.wav")
with open(FAKE, "wb") as f:
    f.write(b"\0" * 64)

OPT = dict(app.DEFAULT_OPT, diarize=False, fixterms=False,
           formats={"plain": True, "timed": False, "srt": False, "canon": False})

ok, bad = [], []


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
    """app.get_whisper 를 갈아 끼운다."""
    def fake(name, device, compute):
        if raise_on_load:
            raise raise_on_load
        return model, False
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
        return FakeModel([Seg(0.0, 3.0, "가")], 12.0,
                         boom=RuntimeError("일부러 낸 오류")), False
    return FakeModel([Seg(0.0, 6.0, "나")], 12.0), False


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

print(f"\n{'=' * 60}")
print(f"  통과 {len(ok)} · 실패 {len(bad)}")
for b in bad:
    print(f"    실패 — {b}")
print(f"{'=' * 60}\n")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if bad else 0)
