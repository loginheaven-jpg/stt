#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup.py — 받아쓰기 설치기. 이 파일을 더블클릭하면 된다.

문서로 다섯 관문을 넘게 하는 것은 무리라 스크립트가 대신한다.
중간에 실패해도 다음으로 간다. 토큰이 없으면 화자 분리만 못 쓸 뿐
받아쓰기는 된다.

명령창에서 돌리려면 — python setup.py
"""

import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE, "env.local")
PY_URL = "https://www.python.org/downloads/"
DIA_MODEL = "pyannote/speaker-diarization-community-1"
MODEL_NAME = "large-v3-turbo"

# (항목, 상태, 덧붙일 말). 상태는 셋이다.
#   ok   — 됐다
#   skip — 사용자가 일부러 건너뛰었다. 실패가 아니다
#   fail — 하려다 안 됐다. 요약에서 이것만 경고한다
steps = []
OK, SKIP, FAIL = "ok", "skip", "fail"


# ─────────────────────────────────────────────────────────────
# 화면
# ─────────────────────────────────────────────────────────────

def line(ch="─", n=62):
    print(ch * n)


def title(n, total, text):
    print()
    line()
    print(f"  {n}/{total}   {text}")
    line()


def say(*a):
    print("  " + " ".join(str(x) for x in a))


def done(name, status, note=""):
    if status is True:
        status = OK
    elif status is False:
        status = FAIL
    steps.append((name, status, note))
    mark = {OK: "완료  ", SKIP: "건너뜀", FAIL: "못 했다"}[status]
    say(mark + f"  {name}" + (f" — {note}" if note else ""))


def ask(q, default=True):
    """예/아니오. 그냥 Enter 를 치면 기본값이다."""
    tail = " (Y/n) " if default else " (y/N) "
    try:
        v = input("  " + q + tail).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    if not v:
        return default
    return v in ("y", "yes", "ㅛ", "예", "네")


def ask_text(q):
    try:
        return input("  " + q).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def pip_install(*pkgs) -> tuple:
    """pip 를 PATH 에서 찾지 않는다. 지금 도는 파이썬으로 부른다."""
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + list(pkgs)
    say("  " + " ".join(cmd[1:]))
    print()
    try:
        r = subprocess.run(cmd)
        return r.returncode == 0, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def have(mod: str) -> str:
    try:
        m = __import__(mod)
        for part in mod.split(".")[1:]:
            m = getattr(m, part)
        return getattr(m, "__version__", "설치됨")
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────
# 단계
# ─────────────────────────────────────────────────────────────

def step_python() -> bool:
    title(1, 10, "파이썬 확인")
    v = sys.version_info
    say(f"파이썬 {v.major}.{v.minor}.{v.micro}")
    say(f"위치 {sys.executable}")
    if v >= (3, 10):
        done("파이썬 3.10 이상", True, f"{v.major}.{v.minor}.{v.micro}")
        return True
    print()
    say("파이썬이 너무 낡았다. 3.10 이상이 필요하다.")
    say(f"내려받는 곳 — {PY_URL}")
    print()
    say("설치할 때 첫 화면 아래의")
    say('    [v] Add Python to PATH')
    say("에 반드시 체크한다. 이걸 놓치면 설치해도 찾지 못한다.")
    done("파이썬 3.10 이상", FAIL, "설치 후 이 파일을 다시 더블클릭한다")
    return False


def step_whisper() -> bool:
    title(2, 10, "받아쓰기 엔진 설치")
    v = have("faster_whisper")
    if v:
        done("faster-whisper", True, f"이미 있다 ({v})")
        return True
    say("faster-whisper 를 내려받는다. 몇 분 걸린다.")
    req = os.path.join(BASE, "requirements.txt")
    ok, why = (pip_install("-r", req) if os.path.isfile(req)
               else pip_install("faster-whisper"))
    if ok and have("faster_whisper"):
        done("faster-whisper", True, have("faster_whisper"))
        return True
    print()
    say("설치에 실패했다. 위의 오류 내용을 그대로 알려주면 도움이 된다.")
    say("인터넷 연결과 회사망 차단 여부를 먼저 본다.")
    done("faster-whisper", FAIL, why or "pip 오류")
    return False


def step_pyannote() -> bool:
    """돌려주는 값은 '쓰겠다고 했는가' 다. 설치 성패와는 별개다.

    설치가 실패해도 토큰은 받아둔다. 나중에 pip 만 다시 돌리면 되도록.
    """
    title(3, 10, "화자 분리")
    say("화자 분리는 '화자1', '화자2' 로 말한 사람을 나눠 적는 기능이다.")
    say("이 도구의 기본 기능이다. 특별한 이유가 없으면 그대로 쓴다.")
    say("쓰려면 약 2.5GB 를 더 내려받는다. 혼자 말하는 강의 녹음만 다룰 거면 빼도 된다.")
    print()
    if not ask("화자 분리를 쓸까?", True):
        done("화자 분리", SKIP, "쓰지 않기로 했다. 나중에 다시 돌리면 된다")
        return False
    if have("pyannote.audio") and have("torch"):
        done("pyannote.audio · torch", OK, "이미 있다")
        return True
    say("2.5GB 를 내려받는다. 시간이 걸린다.")
    if not ask("계속할까?", True):
        done("pyannote.audio · torch", SKIP, "내려받기를 미뤘다")
        return True
    ok, why = pip_install("pyannote.audio", "torch")
    if ok and have("pyannote.audio"):
        done("pyannote.audio · torch", OK, have("pyannote.audio"))
        return True
    done("pyannote.audio · torch", FAIL, why or "pip 오류")
    say("화자 분리는 못 쓰지만 받아쓰기는 된다. 토큰은 미리 받아둔다.")
    return True


def step_token() -> str:
    title(4, 10, "화자 분리 토큰")
    say("화자 분리를 쓰려면 HuggingFace 토큰이 필요하다. 무료다.")
    print()
    say("  1) huggingface.co 에 가입한다")
    say(f"  2) huggingface.co/{DIA_MODEL} 에서")
    say("     약관에 동의한다")
    say("  3) huggingface.co/settings/tokens 에서")
    say('     "Create new token" → 종류를 Read 로 고른다')
    say("  4) 만들어진 hf_ 로 시작하는 값을 복사해 아래에 붙여넣는다")
    print()
    say("붙여넣기는 창 안에서 마우스 오른쪽 단추를 누르면 된다.")
    print()
    tok = ask_text("토큰 (건너뛰려면 그냥 Enter) > ")
    if not tok:
        done("HF 토큰", SKIP, "건너뛰었다. 화자 분리만 못 쓴다")
        return ""
    if not tok.startswith("hf_"):
        say(f"hf_ 로 시작해야 한다. 받은 값은 '{tok[:4]}…' 로 시작한다.")
        if not ask("그래도 저장할까?", False):
            done("HF 토큰", FAIL, "형식이 맞지 않아 저장하지 않았다")
            return ""
    done("HF 토큰", OK, f"{len(tok)}자")
    return tok


def step_env(tok: str) -> bool:
    title(5, 10, "설정 파일에 저장")
    if not tok:
        # 토큰이 없으면 파일을 아예 건드리지 않는다. 기존 값이 있을 수 있다.
        done("env.local", SKIP, "저장할 토큰이 없어 파일을 건드리지 않았다")
        return False
    # 기존 파일이 있으면 HF_TOKEN 줄만 갈아 끼운다. 다른 줄은 손대지 않는다.
    lines, replaced = [], False
    if os.path.isfile(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8-sig", errors="replace") as f:
            lines = f.read().splitlines()
        pat = re.compile(r"^\s*(?:export\s+|set\s+|\$[Ee]nv:)?HF_TOKEN\s*=", re.I)
        for i, ln in enumerate(lines):
            if pat.match(ln):
                lines[i] = f"HF_TOKEN={tok}"
                replaced = True
                break
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"HF_TOKEN={tok}")
    try:
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip("\n") + "\n")
    except OSError as e:
        done("env.local", FAIL, str(e))
        return False
    done("env.local", OK,
         ("HF_TOKEN 줄을 바꿨다" if replaced else "HF_TOKEN 줄을 더했다")
         + f" — {ENV_PATH}")
    return True


def env_token() -> str:
    """설치 화면에서 받지 않았어도 env.local 에 이미 있을 수 있다."""
    try:
        sys.path.insert(0, BASE)
        import app
        app.load_env()
        return app.hf_token()
    except Exception:
        return ""


def load_app():
    """app.py 를 빌려 쓴다. 점검 로직을 두 벌로 두지 않는다."""
    sys.path.insert(0, BASE)
    import app
    return app


def step_gpu() -> bool:
    title(6, 10, "그래픽카드")
    try:
        smi = load_app().nvidia_smi()
    except Exception as e:
        done("그래픽카드", SKIP, f"확인하지 못했다. {type(e).__name__}")
        return False

    if not smi["ok"]:
        say("NVIDIA 그래픽카드를 찾지 못했다. CPU 로 동작한다.")
        say("받아쓰기는 되지만 1시간 녹음에 30분쯤 걸린다.")
        done("그래픽카드", SKIP, smi["why"])
        return False

    say(f"{smi['name']} · 드라이버 CUDA {smi['cuda'] or '?'}")
    if smi["old"]:
        print()
        say(smi["why"])
        say("드라이버를 갱신하고 이 파일을 다시 더블클릭하면 된다.")
        done("그래픽카드", FAIL, f"드라이버 CUDA {smi['cuda']} — 12.0 이상이 필요하다")
        return False

    print()
    say("관련 파일 약 700MB 를 받으면 20배쯤 빨라진다.")
    say("1시간 녹음이 30분에서 3분으로 준다.")
    print()
    if not ask("받을까?", True):
        done("그래픽카드", SKIP, "받지 않기로 했다. CPU 로 동작한다")
        return False

    req = os.path.join(BASE, "requirements-gpu.txt")
    ok, why = (pip_install("-r", req) if os.path.isfile(req)
               else pip_install("nvidia-cublas-cu12", "nvidia-cudnn-cu12>=9"))
    if ok:
        done("그래픽카드", OK, smi["name"])
        return True
    done("그래픽카드", FAIL, why or "pip 오류")
    say("받아쓰기는 CPU 로 그대로 된다. 느릴 뿐이다.")
    return False


def step_gpu_check(want_gpu: bool) -> None:
    """
    **설치만으로는 부족하다.** DLL 이 디스크에 있어도 경로에 없으면 못 찾는다.
    이번 사고가 그것이었다. 끊긴 고리를 짚는다.

    진단 화면과 같은 함수를 쓴다. 두 벌로 두면 한쪽만 고치게 된다.
    """
    if not want_gpu:
        return
    print()
    say("그래픽카드 점검")
    try:
        app = load_app()
        rows = app.gpu_report(probe=os.path.isfile(app.PROBE_WAV))
    except Exception as e:
        say(f"  점검하지 못했다. {type(e).__name__}: {e}")
        return
    mark = {"ok": "통과", "fail": "못 함", "skip": "건너뜀"}
    for name, state, detail in rows:
        say(f"  {mark[state]}  {name:<22s} {detail[:70]}")
    if any(s == "fail" for _, s, _ in rows):
        print()
        say("위에서 '못 함' 이 난 줄이 원인이다. 화면의 [진단] 에서 다시 볼 수 있다.")


def fetch_whisper() -> bool:
    if not have("faster_whisper"):
        done("받아쓰기 모델", SKIP, "엔진이 없어 건너뛴다")
        return False
    say(f"받아쓰기 모델 {MODEL_NAME} 를 받는다. 약 1.6GB 다.")
    print()
    try:
        from faster_whisper import WhisperModel
        WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
        done("받아쓰기 모델", OK, MODEL_NAME)
        return True
    except Exception as e:
        done("받아쓰기 모델", FAIL, f"{type(e).__name__}: {e}")
        say("첫 실행 때 다시 받으므로 큰 문제는 아니다.")
        return False


def fetch_diarization(tok: str) -> bool:
    """화자 분리 모델도 여기서 받는다. 첫 작업 때 받으면 멈춘 것처럼 보인다."""
    if not have("pyannote.audio"):
        done("화자 분리 모델", SKIP, "pyannote.audio 가 없어 건너뛴다")
        return False
    tok = tok or env_token()
    if not tok:
        done("화자 분리 모델", SKIP, "토큰이 없어 건너뛴다")
        return False
    print()
    say("화자 분리 모델을 받는다. 약 31MB 다.")
    print()
    try:
        from pyannote.audio import Pipeline
        try:
            pipe = Pipeline.from_pretrained(DIA_MODEL, token=tok)
        except TypeError:                      # 3.x 계열은 인자 이름이 다르다
            pipe = Pipeline.from_pretrained(DIA_MODEL, use_auth_token=tok)
        if pipe is None:
            raise RuntimeError("모델 접근이 거부됐다")
        done("화자 분리 모델", OK, DIA_MODEL)
        return True
    except Exception as e:
        msg = str(e)
        done("화자 분리 모델", FAIL, f"{type(e).__name__}: {msg[:120]}")
        print()
        if any(k in msg.lower() for k in ("gated", "401", "403", "access", "authoriz")):
            say("거의 이 이유다 — 모델 약관에 아직 동의하지 않았다.")
            say(f"  https://huggingface.co/{DIA_MODEL}")
            say("에 로그인해서 들어가면 동의 단추가 있다. 누른 뒤 이 파일을 다시 돌린다.")
        else:
            say("토큰이 Read 권한인지, 인터넷이 되는지 본다.")
        say("받아쓰기는 그대로 쓸 수 있다. 화자 분리만 못 한다.")
        return False


def step_model(want_dia: bool, tok: str) -> bool:
    title(7, 10, "모델 미리 받기")
    say("받아쓰기 모델 약 1.6GB"
        + (" · 화자 분리 모델 약 31MB" if want_dia else "") + " 를 받는다.")
    say("지금 받아두면 첫 실행이 바로 시작된다.")
    say("건너뛰면 처음 돌릴 때 받느라 한참 멈춘 것처럼 보인다.")
    print()
    if not ask("지금 받을까?", True):
        done("받아쓰기 모델", SKIP, "건너뛰었다. 첫 실행 때 받는다")
        if want_dia:
            done("화자 분리 모델", SKIP, "건너뛰었다. 첫 작업 때 받는다")
        return False
    say("내려받는 중이다. 진행 표시가 멈춘 것처럼 보여도 기다린다.")
    print()
    a = fetch_whisper()
    b = fetch_diarization(tok) if want_dia else True
    return a and b


def step_dirs() -> bool:
    title(8, 10, "폴더 만들기")
    made, bad = [], []
    for name in ("audio", "out_text", "data"):
        p = os.path.join(BASE, name)
        try:
            os.makedirs(p, exist_ok=True)
            made.append(name)
        except OSError as e:
            bad.append(f"{name} ({e})")
    for name, what in (("audio", "여기에 음원을 넣는다"),
                       ("out_text", "결과가 여기에 쌓인다"),
                       ("data", "설정과 기록이 들어간다")):
        say(f"{os.path.join(BASE, name)}   — {what}")
    done("폴더", OK if not bad else FAIL, ", ".join(bad) if bad else " · ".join(made))
    return not bad


def step_shortcut() -> bool:
    title(9, 10, "바탕화면 바로가기")
    if os.name != "nt":
        done("바로가기", SKIP, "윈도우에서만 만든다")
        return False
    try:
        sys.path.insert(0, BASE)
        import app
        r = app.make_shortcut()
    except Exception as e:
        done("바로가기", FAIL, f"{type(e).__name__}: {e}")
        return False
    if r.get("ok"):
        done("바로가기", OK, r["path"])
        return True
    done("바로가기", FAIL, r.get("why", ""))
    say(f"바탕화면에 만들지 못했다. 대신 이 파일을 더블클릭하면 된다 —")
    say(f"  {r.get('start') or os.path.join(BASE, 'start.vbs')}")
    return False


def step_summary() -> None:
    title(10, 10, "설치 결과")
    for name, status, note in steps:
        mark = {OK: "통과  ", SKIP: "건너뜀", FAIL: "못 했다"}[status]
        print(f"  {mark} {name}" + (f"   {note}" if note else ""))
    print()
    fails = [n for n, s, _ in steps if s == FAIL]
    skips = [n for n, s, _ in steps if s == SKIP]
    if not fails:
        say("설치가 끝났다." + (f"  건너뛴 것 — {', '.join(skips)}" if skips else ""))
    else:
        say(f"{len(fails)}개를 못 했다 — {', '.join(fails)}")
        say("그래도 받아쓰기는 대개 동작한다. 일단 띄워 보고,")
        say("화면의 [진단] 을 누르면 무엇이 빠졌는지 한자리에서 볼 수 있다.")
    print()
    line("═")
    say("바탕화면의 [받아쓰기] 를 더블클릭하면 시작된다.")
    say("바로가기가 없으면 이 폴더의 start.vbs 를 더블클릭한다.")
    say("음원은 audio 폴더에 넣는다.")
    line("═")


def main() -> int:
    print()
    line("═")
    print("  받아쓰기 설치")
    print("  이 PC에서만 도는 한국어 받아쓰기 도구다. 비용이 들지 않는다.")
    line("═")

    if not step_python():
        return 1
    step_whisper()
    want_dia = step_pyannote()
    tok = step_token() if want_dia else ""
    if want_dia:
        step_env(tok)
    want_gpu = step_gpu()
    step_model(want_dia, tok)
    step_dirs()
    step_shortcut()
    step_gpu_check(want_gpu)
    step_summary()
    return 0


if __name__ == "__main__":
    # 더블클릭했는데 pythonw 로 열리면 보이는 창이 없다. python 으로 다시 띄운다.
    if sys.stdout is None:
        exe = os.path.join(os.path.dirname(sys.executable), "python.exe")
        if os.path.isfile(exe):
            subprocess.Popen([exe, os.path.abspath(__file__)])
        sys.exit(0)
    code = 0
    try:
        code = main()
    except KeyboardInterrupt:
        print("\n  그만뒀다.")
    except Exception as e:
        import traceback
        print("\n  뜻밖의 오류가 났다. 아래 내용을 그대로 알려주면 도움이 된다.\n")
        traceback.print_exc()
        code = 1
    print()
    try:
        input("  창을 닫으려면 Enter 를 누른다. ")
    except (EOFError, KeyboardInterrupt):
        pass
    sys.exit(code)
