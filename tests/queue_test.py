#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
queue_test.py — 대기열·이력·설정·모델 캐시 인수 시험.

    python tests/queue_test.py

실제 앱을 띄우고 HTTP로 몰아본다. 화면을 갈아엎을 때 백엔드가
그대로인지 보는 물건이다. 통과하면 0, 하나라도 실패하면 1을 돌려준다.

음원이 필요한 절이 있다. audio/sample3.wav 나 sample3.wav 가 없으면
그 절은 건너뛰고, 음원 없이 볼 수 있는 것만 본다.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp(prefix="stt_queue_test_")
TESTDATA = os.path.join(WORK, "data")
TESTOUT = os.path.join(WORK, "out")
PORT = os.environ.get("TESTPORT", "8791")

os.makedirs(TESTDATA, exist_ok=True)
os.makedirs(TESTOUT, exist_ok=True)
os.environ["DATADIR"] = TESTDATA
os.environ["OUTDIR"] = TESTOUT
os.environ["PORT"] = PORT
sys.path.insert(0, BASE)
os.chdir(BASE)
import app  # noqa: E402

app.webbrowser.open = lambda *a, **k: None

AUDIO = next((p for p in (os.path.join(BASE, "audio", "sample3.wav"),
                          os.path.join(BASE, "sample3.wav")) if os.path.isfile(p)), None)

# 전사에 실패할 파일. 실패 격리와 순서 시험에 쓴다. 음원이 없어도 만들 수 있다.
BAD1 = os.path.join(WORK, "깨진음원1.wav")
BAD2 = os.path.join(WORK, "깨진음원2.wav")
for p in (BAD1, BAD2):
    with open(p, "wb") as f:
        f.write(b"NOT A WAV FILE" * 100)

FAST = dict(app.DEFAULT_OPT, diarize=False, hotwords="이승은",
            formats={"plain": True, "timed": False, "srt": False, "canon": False})

ok, bad, skipped = [], [], []


def check(name, cond, detail=""):
    (ok if cond else bad).append(name)
    print(f"  {'통과' if cond else '실패'}  {name}" + (f"   {detail}" if detail else ""))


def skip(name, why):
    skipped.append(name)
    print(f"  건너뜀 {name}   {why}")


def post(path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=30) as r:
        return json.loads(r.read())


def wait_idle(timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = get("/state")
        pend = [x for x in s["queue"] if x["state"] in ("waiting", "running")]
        busy = s["job"]["state"] in ("loading", "running", "diarizing", "merging")
        if not pend and not busy:
            return True
        time.sleep(0.5)
    return False


def clear_queue():
    post("/queue/stopall", {"on": True})
    for x in get("/queue")["items"]:
        post("/queue/remove", {"id": x["id"]})
    post("/queue/stopall", {"on": False})


threading.Thread(target=app.main, daemon=True).start()
time.sleep(2.0)
print(f"\n  받아쓰기 v{app.APP_VERSION} · 대기열 인수 시험")
print(f"  작업 폴더 {WORK}")
print(f"  음원 {AUDIO or '없다 — 전사가 필요한 절은 건너뛴다'}")

CACHE_NUM = None

if AUDIO:
    print("\n■ 1. 대기열 — 3건을 담고 순서대로 끝나는가\n")
    app.release_cache("시험 시작")
    t0 = time.time()
    r = post("/queue/add", {"paths": [AUDIO, AUDIO, AUDIO],
                            "settings": FAST, "outdir": TESTOUT})
    check("3건 담기", r.get("ok") and len(r["ids"]) == 3)
    check("같은 이름 충돌을 알린다", len(r.get("warn") or []) == 2)
    q = get("/queue")["items"]
    check("대기열에 3건", len(q) == 3)
    check("동시 실행 금지", sum(1 for x in q if x["state"] == "running") <= 1)
    check("대기열 3건 완료", wait_idle())
    total_cached = time.time() - t0
    h = get("/history?n=10")["items"]
    done3 = [x for x in h if x["state"] == "done"][:3]
    check("이력 3건 기록", len(done3) == 3)
    order = [x["started"] for x in reversed(done3)]
    check("순서대로 실행", order == sorted(order))

    print("\n■ 2. 모델 캐시 — 효과가 숫자로 보이는가\n")
    el = [x["elapsed"] for x in reversed(done3)]
    cached = [x["cached"] for x in reversed(done3)]
    print(f"     소요 {el}  ·  캐시적중 {cached}  ·  총 {total_cached:.1f}초")
    check("1번째는 적재", cached[0] is False)
    check("2·3번째는 재사용", cached[1] is True and cached[2] is True)
    check("2·3번째가 1번째보다 빠르다", el[1] < el[0] and el[2] < el[0],
          f"{el[0]:.1f} → {el[1]:.1f} / {el[2]:.1f}초")

    print("\n■ 3. 캐시 없는 경우 대비 — 항목마다 캐시를 비우고 같은 일을 시킨다\n")
    # 설정은 위와 똑같이 두고 캐시만 없앤다. 그래야 캐시 효과만 남는다.
    t0 = time.time()
    for _ in range(3):
        app.release_cache()
        post("/queue/add", {"paths": [AUDIO], "settings": FAST, "outdir": TESTOUT})
        wait_idle()
    total_nocache = time.time() - t0
    h = get("/history?n=10")["items"][:3]
    cached2 = [x["cached"] for x in reversed(h)]
    el2 = [x["elapsed"] for x in reversed(h)]
    print(f"     캐시적중 {cached2}  ·  소요 {el2}  ·  총 {total_nocache:.1f}초")
    check("캐시를 비우면 매번 재적재", cached2 == [False, False, False])
    check("캐시가 총 소요를 줄인다", total_cached < total_nocache,
          f"캐시 {total_cached:.1f}초 < 무캐시 {total_nocache:.1f}초")
    CACHE_NUM = (el, cached, total_cached, el2, cached2, total_nocache)

    print("\n■ 4. 실패 격리 — 2번이 실패해도 3번이 도는가\n")
    post("/queue/stopall", {"on": True})
    post("/queue/add", {"paths": [AUDIO], "settings": FAST, "outdir": TESTOUT})
    post("/queue/add", {"paths": [BAD1], "settings": FAST, "outdir": TESTOUT})
    post("/queue/add", {"paths": [AUDIO], "settings": FAST, "outdir": TESTOUT})
    check("전체 중지 중에는 담기만 된다",
          all(x["state"] == "waiting" for x in get("/queue")["items"]))
    post("/queue/stopall", {"on": False})
    check("3건 처리 완료", wait_idle())
    states = [x["state"] for x in reversed(get("/history?n=10")["items"][:3])]
    print(f"     결과 {states}")
    check("2번이 실패했다", states[1] == "error")
    check("3번이 실행됐다", states[2] == "done")

    print("\n■ 5. 재시작 복원과 중단 처리\n")
    post("/queue/stopall", {"on": True})
    post("/queue/add", {"paths": [AUDIO, AUDIO], "settings": FAST, "outdir": TESTOUT})
    qpath = os.path.join(TESTDATA, "queue.json")
    saved = json.load(open(qpath, encoding="utf-8"))
    check("queue.json 에 남는다", len(saved["items"]) == 2)
    check("version 필드가 있다", saved.get("version") == 1)
    saved["items"][0]["state"] = "running"       # 돌던 중 앱이 죽은 상황
    json.dump(saved, open(qpath, "w", encoding="utf-8"), ensure_ascii=False)
    app.load_state()
    q = get("/queue")["items"]
    check("껐다 켜도 대기 항목이 살아 있다", len(q) == 2)
    check("running 은 interrupted 로", q[0]["state"] == "interrupted")
    post("/queue/stopall", {"on": False})
    time.sleep(1.5)
    check("interrupted 는 자동 재실행하지 않는다",
          any(x["state"] == "interrupted" for x in get("/queue")["items"]))
    wait_idle()

    print("\n■ 6. 부분 산출 보존과 다시 하기\n")
    left = [x for x in get("/queue")["items"] if x["state"] == "interrupted"]
    if left:
        stem = os.path.splitext(os.path.basename(left[0]["path"]))[0]
        with open(os.path.join(TESTOUT, stem + ".txt"), "w", encoding="utf-8") as f:
            f.write("부분 결과")
        r = post("/queue/resume", {"id": left[0]["id"], "mode": "keep"})
        check("부분 산출을 보존하고 다시 담는다", bool(r.get("ok") and r.get("kept")))
        check("_부분 파일이 생겼다",
              os.path.exists(os.path.join(TESTOUT, stem + "_부분.txt")))
        wait_idle()
    else:
        check("중단 항목 확보", False)
else:
    for name in ("대기열 3건", "모델 캐시", "실패 격리", "재시작 복원", "부분 산출 보존"):
        skip(name, "음원이 없다")

clear_queue()

print("\n■ 7. 순서 바꾸기·빼기·출력 폴더 검사\n")
post("/queue/stopall", {"on": True})
post("/queue/add", {"paths": [BAD1], "settings": FAST, "outdir": TESTOUT})
post("/queue/add", {"paths": [BAD2], "settings": FAST, "outdir": TESTOUT})
q = get("/queue")["items"]
first = q[0]["id"]
post("/queue/move", {"id": first, "dir": 1})
check("아래로 옮긴다", get("/queue")["items"][1]["id"] == first)
post("/queue/move", {"id": first, "dir": -1})
check("위로 옮긴다", get("/queue")["items"][0]["id"] == first)
post("/queue/remove", {"id": first})
check("대기 항목을 뺀다", all(x["id"] != first for x in get("/queue")["items"]))
clear_queue()

r = post("/outdir/check", {"path": os.path.join(WORK, "새폴더", "안쪽")})
check("없는 출력 폴더를 만든다", r.get("ok"))

# 못 쓰는 폴더 시험은 권한이 있어야 성립한다.
# 리눅스 루트는 chmod 500 폴더에도 쓰므로 이 시험이 뜻을 잃는다.
if os.name == "nt":
    bad_path = "Z:\\없는드라이브\\x"
elif hasattr(os, "geteuid") and os.geteuid() == 0:
    bad_path = None
else:
    bad_path = "/proc/못쓰는폴더"
if bad_path:
    r = post("/outdir/check", {"path": bad_path})
    check("못 쓰는 폴더는 이유를 알린다", not r.get("ok"), (r.get("why") or "")[:40])
else:
    skip("못 쓰는 폴더는 이유를 알린다", "루트로 돌고 있다. 무엇에나 쓸 수 있어 시험이 성립하지 않는다")

print("\n■ 8. /open 경로 검사\n")


def open_denied(path):
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/open?p=" + urllib.parse.quote(path), timeout=10)
        return False
    except urllib.error.HTTPError as e:
        return e.code == 404


check("등록 폴더 밖 파일을 막는다", open_denied(os.path.join(BASE, "app.py")))
sibling = TESTOUT + "2"
os.makedirs(sibling, exist_ok=True)
with open(os.path.join(sibling, "x.txt"), "w", encoding="utf-8") as f:
    f.write("옆 폴더")
check("out2 를 out 으로 통과시키지 않는다", open_denied(os.path.join(sibling, "x.txt")))
inside = os.path.join(TESTOUT, "안쪽.txt")
with open(inside, "w", encoding="utf-8") as f:
    f.write("등록된 폴더")
check("등록 폴더 안 파일은 열린다", not open_denied(inside))

print("\n■ 9. 설정 저장과 프리셋\n")
s = get("/settings")
check("기본 프리셋 2종", len(s["presets"]) == 2,
      " · ".join(p["name"] for p in s["presets"]))
check("최근 출력 폴더가 기록된다", TESTOUT in s["recent_outdirs"])
post("/settings", {"presets": s["presets"] + [{"name": "시험용", "settings": FAST}]})
check("프리셋 저장", len(get("/settings")["presets"]) == 3)
saved = json.load(open(os.path.join(TESTDATA, "settings.json"), encoding="utf-8"))
check("settings.json 에 남는다", len(saved["presets"]) == 3)
check("마지막 설정이 남는다", saved["last"]["hotwords"] == "이승은")

print(f"\n{'=' * 60}")
if CACHE_NUM:
    e1, c1, t1, e2, c2, t2 = CACHE_NUM
    print("  모델 캐시 실측")
    print(f"    캐시 있음   소요 {[round(x, 1) for x in e1]}  적중 {c1}  총 {t1:.1f}초")
    print(f"    캐시 없음   소요 {[round(x, 1) for x in e2]}  적중 {c2}  총 {t2:.1f}초")
    print(f"    단축        {t2 - t1:.1f}초  ({(1 - t1 / t2) * 100:.0f}%)")
    print(f"{'-' * 60}")
print(f"  통과 {len(ok)} · 실패 {len(bad)}"
      + (f" · 건너뜀 {len(skipped)}" if skipped else ""))
for b in bad:
    print(f"    실패 — {b}")
print(f"{'=' * 60}\n")

shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if bad else 0)
