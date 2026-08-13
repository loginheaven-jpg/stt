#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
convert_clovanote.py — 클로바노트 내려받기 파일을 채점기 입력 형식으로 바꾼다.

클로바노트 원본 형식
    참석자 1 00:47
    네 안녕하세요. 김도현입니다.

변환 결과
    [00:47]	참석자1	네 안녕하세요. 김도현입니다.

발췌 구간만 잘라낼 수 있다. 시각은 구간 시작점 기준 00:00으로 다시 매긴다.
발췌한 음원을 엔진에 돌린 결과와 시각 기준을 맞추기 위해서다.

사용법
    python3 convert_clovanote.py sample.txt -o clovanote_full.txt
    python3 convert_clovanote.py sample.txt --from 08:30 --to 11:30 -o clip01A__clovanote.txt
    python3 convert_clovanote.py sample.txt --stats
"""

import argparse
import re
import sys
import unicodedata
from typing import List, Tuple, Optional

# "참석자 1 00:47" / "화자 2 1:02:03" / "김도현 00:47" 모두 받는다
RE_HEAD = re.compile(r"^\s*(.+?)\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")

# 본문으로 볼 수 없는 줄
RE_NOISE = re.compile(r"^\s*(clovanote\.naver\.com|https?://)\S*\s*$", re.I)


def parse_head(line: str) -> Optional[Tuple[str, float]]:
    m = RE_HEAD.match(line)
    if not m:
        return None
    name, a, b, c = m.groups()
    name = name.strip()
    # 화자명이 지나치게 길면 본문 끝의 숫자를 오인한 것이다
    if len(name) > 20:
        return None
    sec = (int(a) * 3600 + int(b) * 60 + int(c)) if c else (int(a) * 60 + int(b))
    return name.replace(" ", ""), float(sec)


def to_sec(hhmmss: str) -> float:
    parts = [int(p) for p in hhmmss.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"시각 형식 오류: {hhmmss}")


def fmt_time(sec: float) -> str:
    sec = max(0.0, sec)
    return f"{int(sec) // 60:02d}:{int(sec) % 60:02d}"


def load(path: str) -> List[Tuple[float, str, str]]:
    with open(path, encoding="utf-8-sig") as f:      # BOM 제거
        raw = unicodedata.normalize("NFC", f.read())

    utts: List[Tuple[float, str, str]] = []
    cur_spk: Optional[str] = None
    cur_sec = 0.0
    buf: List[str] = []

    def flush():
        if cur_spk is not None and buf:
            text = " ".join(" ".join(buf).split())
            if text:
                utts.append((cur_sec, cur_spk, text))

    for line in raw.splitlines():
        if not line.strip() or RE_NOISE.match(line):
            continue
        head = parse_head(line)
        if head:
            flush()
            cur_spk, cur_sec = head
            buf = []
            continue
        if cur_spk is not None:
            buf.append(line.strip())
    flush()
    return utts


def main() -> int:
    ap = argparse.ArgumentParser(description="클로바노트 → 채점기 형식 변환")
    ap.add_argument("input", help="클로바노트 내려받기 txt")
    ap.add_argument("-o", "--output", help="출력 파일 (없으면 표준출력)")
    ap.add_argument("--from", dest="start", help="발췌 시작 MM:SS")
    ap.add_argument("--to", dest="end", help="발췌 종료 MM:SS")
    ap.add_argument("--no-rebase", action="store_true",
                    help="발췌해도 시각을 원본 기준으로 유지")
    ap.add_argument("--stats", action="store_true", help="통계만 출력")
    args = ap.parse_args()

    utts = load(args.input)
    if not utts:
        print("발화를 찾지 못했다. 형식을 확인해달라.", file=sys.stderr)
        return 1

    if args.stats:
        spks = {}
        for sec, spk, text in utts:
            s = spks.setdefault(spk, {"n": 0, "chars": 0, "first": sec, "last": sec})
            s["n"] += 1
            s["chars"] += len(text.replace(" ", ""))
            s["last"] = sec
        print(f"\n총 발화 {len(utts)}건 · 화자 {len(spks)}명 · "
              f"종료 {fmt_time(utts[-1][0])}\n")
        print(f"{'화자':<10}{'발화':>6}{'문자':>8}{'첫 등장':>10}{'끝':>8}")
        print("─" * 42)
        for spk, s in sorted(spks.items(), key=lambda x: -x[1]["chars"]):
            print(f"{spk:<10}{s['n']:>6}{s['chars']:>8}"
                  f"{fmt_time(s['first']):>10}{fmt_time(s['last']):>8}")
        print()
        return 0

    lo = to_sec(args.start) if args.start else None
    hi = to_sec(args.end) if args.end else None
    if lo is not None or hi is not None:
        utts = [u for u in utts
                if (lo is None or u[0] >= lo) and (hi is None or u[0] < hi)]
        if lo is not None and not args.no_rebase:
            utts = [(s - lo, spk, t) for s, spk, t in utts]

    lines = [f"[{fmt_time(s)}]\t{spk}\t{t}" for s, spk, t in utts]
    body = "\n".join(lines) + "\n"

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"발화 {len(utts)}건 → {args.output}")
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
