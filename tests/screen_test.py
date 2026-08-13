#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
screen_test.py — 화면이 쓰는 경로를 서버 쪽에서 확인한다.

    python tests/screen_test.py

브라우저를 띄우지 않고, 화면의 JS가 부르는 것과 같은 요청을 같은 모양으로
보내 응답에 필요한 항목이 다 들어 있는지 본다. 화면을 고칠 때 백엔드와
어긋나면 여기서 걸린다.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp(prefix="stt_screen_test_")
os.environ["DATADIR"] = os.path.join(WORK, "data")
os.environ["OUTDIR"] = os.path.join(WORK, "out")
os.environ["PORT"] = os.environ.get("TESTPORT", "8793")
os.makedirs(os.environ["OUTDIR"], exist_ok=True)
sys.path.insert(0, BASE)
os.chdir(BASE)
import app  # noqa: E402

app.webbrowser.open = lambda *a, **k: None

AUDIO = next((p for p in (os.path.join(BASE, "audio", "sample3.wav"),
                          os.path.join(BASE, "sample3.wav")) if os.path.isfile(p)), None)
PORT = os.environ["PORT"]
ok, bad, skipped = [], [], []


def check(name, cond, detail=""):
    (ok if cond else bad).append(name)
    print(f"  {'통과' if cond else '실패'}  {name}" + (f"   {detail}" if detail else ""))


def get(p):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{p}", timeout=60) as r:
        return json.loads(r.read())


def post(p, b):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{p}",
                                 data=json.dumps(b).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


threading.Thread(target=app.main, daemon=True).start()
time.sleep(2.0)
print(f"\n  받아쓰기 v{app.APP_VERSION} · 화면 인수 시험\n")

print("■ 1. 페이지가 온전히 나오는가\n")
with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=30) as r:
    page = r.read().decode("utf-8")
check("페이지 응답", r.status == 200)
check("자리표시자가 남지 않았다", not re.findall(r"__[A-Z]+__", page))
ext = [u for u in re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)', page)
       if not u.startswith(("/", "#"))]
check("외부 자원을 부르지 않는다", not ext, str(ext))
check("localStorage 를 쓰지 않는다", "localStorage" not in page)
check("시간 눈금 막대가 있다", 'class="ticks"' in page and 'class="track"' in page)
check("배속이 화면의 주인공이다", 'class="rate"' in page)
check("종료 버튼이 있다", 'id="quit"' in page)
for lab in ("지금 하는 일", "대기열", "담기", "최근 기록"):
    check(f"구역 — {lab}", lab in page)

print("\n■ 2. /state 가 화면이 읽는 것을 다 내려주는가\n")
s = get("/state")
check("최상위 키", set(s) >= {"job", "queue", "stopall", "history", "settings", "version"},
      str(sorted(s)))
need_job = {"state", "phase", "dia_pct", "speakers", "file", "stem", "duration",
            "processed", "elapsed", "speed", "eta", "segments", "chars",
            "corrections", "holds", "tail", "outputs", "message", "qid", "outdir", "cached"}
check("job 키", set(s["job"]) >= need_job, str(sorted(need_job - set(s["job"]))))
check("settings 키", set(s["settings"]) >= {"last", "recent_outdirs", "presets"})
check("프리셋 2종", len(s["settings"]["presets"]) == 2)

print("\n■ 3. 담기 — 화면이 보내는 모양 그대로\n")
opt = dict(app.DEFAULT_OPT, diarize=False, hotwords="이승은",
           formats={"plain": True, "timed": False, "srt": False, "canon": False})
target = AUDIO or os.path.join(WORK, "가짜.wav")
if not AUDIO:
    with open(target, "wb") as f:
        f.write(b"NOT A WAV" * 50)
post("/queue/stopall", {"on": True})
r = post("/queue/add", {"paths": [target], "settings": opt,
                        "outdir": os.environ["OUTDIR"]})
check("대기열에 담긴다", bool(r.get("ok")))
q = get("/state")["queue"]
check("대기열 항목에 화면이 쓰는 키", q and set(q[0]) >= {"id", "name", "state", "settings"})

print("\n■ 4. 대기열 단추\n")
post("/queue/add", {"paths": [target], "settings": opt, "outdir": os.environ["OUTDIR"]})
q = get("/state")["queue"]
first = q[0]["id"]
post("/queue/move", {"id": first, "dir": 1})
check("▼ 아래로", get("/state")["queue"][1]["id"] == first)
post("/queue/move", {"id": first, "dir": -1})
check("▲ 위로", get("/state")["queue"][0]["id"] == first)
post("/queue/remove", {"id": first})
check("✕ 빼기", all(x["id"] != first for x in get("/state")["queue"]))
r = post("/queue/stopall", {"on": False})
check("전체 중지·재개", r.get("stopall") is False)

print("\n■ 5. 진행과 기록\n")
if AUDIO:
    t0 = time.time()
    seen = set()
    while time.time() - t0 < 600:
        s = get("/state")
        seen.add(s["job"]["state"])
        pend = [x for x in s["queue"] if x["state"] in ("waiting", "running")]
        if not pend and s["job"]["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.4)
    check("진행 상태를 거친다", {"loading", "running", "done"} <= seen, str(sorted(seen)))
    h = get("/state")["history"]
    check("기록이 쌓인다", len(h) >= 1)
    need_h = {"id", "name", "finished", "duration", "speed", "speakers",
              "state", "outputs", "settings", "path"}
    check("기록 키", h and set(h[0]) >= need_h, str(sorted(need_h - set(h[0] if h else {}))))
    check("결과 열기 경로가 있다", bool(h[0]["outputs"]))
    body = urllib.request.urlopen(
        f"http://127.0.0.1:{PORT}/open?p=" + urllib.parse.quote(h[0]["outputs"][0]),
        timeout=30).read()
    check("결과가 열린다", len(body) > 0)
    r = post("/history/again", {"id": h[0]["id"]})
    check("같은 설정으로 다시", bool(r.get("ok")))
    post("/queue/stopall", {"on": True})
    for x in get("/state")["queue"]:
        post("/queue/remove", {"id": x["id"]})
    post("/queue/stopall", {"on": False})
else:
    for n in ("진행 상태", "기록", "같은 설정으로 다시"):
        skipped.append(n)
        print(f"  건너뜀 {n}   음원이 없다")

print("\n■ 6. 진단 화면\n")
d = get("/diag")
check("진단 최상위 키",
      set(d) >= {"version", "python", "packages", "token", "device", "models",
                 "dirs", "log", "log_lines"}, str(sorted(d)))
check("토큰 값을 노출하지 않는다",
      "…" in (d["token"].get("shown") or "") or not d["token"].get("ok"),
      d["token"].get("shown", ""))
tok = app.hf_token()
check("토큰 원문이 응답에 없다", not tok or tok not in json.dumps(d, ensure_ascii=False))
check("패키지 목록", any(p["name"] == "faster-whisper" for p in d["packages"]))
check("기록 200줄", isinstance(d["log_lines"], list))

print("\n■ 7. 종료\n")
r = post("/quit", {})
check("종료 응답", bool(r.get("ok")))
time.sleep(2.5)
import socket
sk = socket.socket()
sk.settimeout(0.6)
closed = sk.connect_ex(("127.0.0.1", int(PORT))) != 0
sk.close()
check("포트가 닫힌다", closed)

print(f"\n{'=' * 60}")
print(f"  통과 {len(ok)} · 실패 {len(bad)}" + (f" · 건너뜀 {len(skipped)}" if skipped else ""))
for b in bad:
    print(f"    실패 — {b}")
print(f"{'=' * 60}\n")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if bad else 0)
