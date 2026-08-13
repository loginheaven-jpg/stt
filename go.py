#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
go.py — 사전 점검 → 전사 → 채점을 한 번에 실행한다.

셸 래퍼(.cmd/.ps1)를 대신한다.
줄바꿈, 인코딩, 실행 정책 어느 것에도 걸리지 않는다.

사용법
    python go.py -p
    python go.py sample3.wav sample3.txt 2 이승은,황미혜,이대표님
    python go.py sample.mp3 ref_A.txt 0 김도현,변규리,류지현
    python go.py sample.mp3                       정답지 없이 전사만

인자 순서 : 음원  정답지  화자수  키워드(쉼표 구분)
"""

import os
import subprocess
import sys

PY = sys.executable                    # 지금 실행 중인 파이썬을 그대로 쓴다
HERE = os.path.dirname(os.path.abspath(__file__))
ENGINES = "rtzr_sommers,rtzr_whisper,clova,openai,whisper_local"


def run(script: str, args: list) -> int:
    """셸을 거치지 않고 인자 배열을 그대로 넘긴다. 따옴표 문제가 생기지 않는다."""
    cmd = [PY, os.path.join(HERE, script)] + [str(a) for a in args]
    print("\n> " + " ".join(cmd[1:]), flush=True)
    return subprocess.call(cmd)


def banner(step: str, text: str) -> None:
    print("\n" + "=" * 60)
    print(f"  {step}   {text}")
    print("=" * 60, flush=True)


def usage() -> int:
    print(__doc__)
    return 1


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        return usage()

    if argv[0] in ("-p", "--preflight"):
        return run("run_stt.py", ["--preflight", "--engines", ENGINES])

    audio = argv[0]
    ref = argv[1] if len(argv) > 1 else ""
    speakers = argv[2] if len(argv) > 2 else "0"
    keywords = [k.strip() for k in argv[3].split(",")] if len(argv) > 3 else []
    keywords = [k for k in keywords if k]

    if not os.path.isfile(audio):
        print(f"\n음원을 찾지 못했다: {audio}")
        return 1

    stem = os.path.splitext(os.path.basename(audio))[0]
    outdir = f"out_{stem}"

    banner("1/3", "사전 점검")
    rc = run("run_stt.py", ["--preflight", "--engines", ENGINES,
                            "--speakers", speakers])
    if rc != 0:
        print("\n준비되지 않은 엔진이 있다.")
        try:
            ans = input("그대로 진행할까? (y/N) ").strip().lower()
        except EOFError:
            ans = "n"
        if ans != "y":
            return 1

    banner("2/3", f"전사   {audio} · 화자 {speakers} · {outdir}")
    args = [audio, "--speakers", speakers, "--engines", ENGINES,
            "--outdir", outdir]
    if keywords:
        args += ["--keywords"] + keywords
    run("run_stt.py", args)

    if not ref:
        print(f"\n정답지가 없어 채점은 건너뛴다. 결과는 {outdir} 에 있다.")
        return 0
    if not os.path.isfile(ref):
        print(f"\n정답지를 찾지 못했다: {ref}")
        return 1

    banner("3/3", f"채점   {ref}")
    args = [ref, os.path.join(outdir, f"{stem}__*.txt"), "--detail"]
    if keywords:
        args += ["--nouns"] + keywords
    return run("stt_bench.py", args)


if __name__ == "__main__":
    sys.exit(main())
