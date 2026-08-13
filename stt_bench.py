#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stt_bench.py — STT 엔진 비교 채점기

정답 전사(reference)와 엔진 출력(hypothesis)을 대조해 6개 지표를 산출한다.
일반 STT 벤치마크와 달리 간투사 보존율과 화자 오귀속률을 함께 잰다.
정본화 규칙에서 이 둘이 CER보다 중요하기 때문이다.

사용법
    python3 stt_bench.py ref.txt rtzr.txt clova.txt whisper.txt
    python3 stt_bench.py ref.txt *.txt --nouns 황미혜 스쿼트 에스컬레이터
    python3 stt_bench.py --template > ref_template.txt

입력 형식 (정답·엔진 출력 공통, 둘 다 지원)
    형식 A   [01:23] 코치<TAB>어 안녕하세요 반갑습니다
    형식 B   **[01:23] 코치**
             어 안녕하세요 반갑습니다

의존성 없음. rapidfuzz가 설치돼 있으면 편집거리 계산에 자동으로 쓴다.
"""

import argparse
import difflib
import glob
import os
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Optional

# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────

# 원칙 1이 보존을 요구하는 간투사·말버릇. 엔진이 지우면 감점된다.
FILLERS = [
    # 주저·머뭇거림. 원칙 1이 보존을 요구하는 핵심이다.
    "어", "어어", "음", "음음", "아", "으", "에", "엄", "아이고", "어유", "아유", "어우", "어잉", "오",
    # 맞장구·말버릇. 정본화 규칙이 예로 든 "이제" "좀" "그"를 포함한다.
    "네", "예", "응", "그", "저", "뭐", "이제", "인제", "좀", "막", "약간",
]
# 뺀 것 — 그래서·그거·아니처럼 실질 의미를 지닌 어휘는 넣지 않는다.
# 넣어 두면 오인식(그거→그걸)이 간투사 삭제로 잘못 집계된다.

# 비교 전에 제거할 문장부호. 구어에 문장부호는 엔진마다 정책이 달라 비교 대상이 아니다.
PUNCT = re.compile(r"[.,!?~…·\"'“”‘’()\[\]{}<>《》「」『』:;\-—/\\|＊*#]")

# 발화 헤더 두 형식
RE_HDR_A = re.compile(r"^\s*\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]\s*([^\t]+?)\s*\t(.*)$")
RE_HDR_B = re.compile(r"^\s*\*\*\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]\s*(.+?)\s*\*\*\s*$")
# 시각 없이 화자만 적는 형식.  화자1: 안녕하세요 / 코치: 네
# 라벨은 12자 이내, 숫자만으로 된 것(시각 오인)은 제외한다.
RE_HDR_C = re.compile(r"^\s*(?!\d+\s*[:：])([^:：]{1,12}?)\s*[:：]\s*(\S.*)$")

TEMPLATE = """\
# 정답 전사 파일 — 작성 규칙
#   1. 들리는 대로 적는다. 문법 교정 금지.
#   2. 간투사("어" "음" "그")와 반복, 더듬음을 전부 남긴다.
#   3. 숫자는 발음대로 적는다. 16분 → 십육 분
#   4. 문장부호는 넣지 않는다.
#   5. 알아들을 수 없으면 (불명)으로 표기하고 채점에서 제외한다.
#   6. 겹침 구간은 두 화자 모두 적는다.
#   7. '#'으로 시작하는 줄은 주석이다.
#
# 형식:  [MM:SS] 화자<TAB>발화내용

[00:00]	코치	어 안녕하세요 저는 이제 김미영 코치라고 합니다
[00:04]	고객	네 안녕하세요
[00:06]	코치	오늘 코칭을 시작하기 전에 어 비밀보장에 대해서 좀 말씀을 드릴게요
"""


# ─────────────────────────────────────────────────────────────
# 파싱
# ─────────────────────────────────────────────────────────────

PLAIN_SPK = "?"        # 화자 표기가 없는 정답지에 붙이는 자리표시


@dataclass
class Utt:
    """발화 한 건."""
    start: float          # 초 단위 시작 시각
    speaker: str          # 원본 화자 태그
    text: str             # 발화 내용
    tokens: List[str] = field(default_factory=list)   # 정규화된 어절


def _to_sec(mm: str, ss: str, frac: Optional[str]) -> float:
    v = int(mm) * 60 + int(ss)
    if frac:
        v += int(frac) / (10 ** len(frac))
    return v


def parse(path: str) -> List[Utt]:
    """형식 A/B를 모두 받아 발화 목록으로 만든다."""
    utts: List[Utt] = []
    pending: Optional[Utt] = None

    with open(path, encoding="utf-8") as f:
        raw = f.read()
    raw = unicodedata.normalize("NFC", raw)

    for line in raw.splitlines():
        if line.strip().startswith("#"):
            continue

        m = RE_HDR_A.match(line)
        if m:
            if pending:
                utts.append(pending)
                pending = None
            mm, ss, frac, spk, txt = m.groups()
            utts.append(Utt(_to_sec(mm, ss, frac), spk.strip(), txt.strip()))
            continue

        m = RE_HDR_B.match(line)
        if m:
            if pending:
                utts.append(pending)
            mm, ss, frac, spk = m.groups()
            pending = Utt(_to_sec(mm, ss, frac), spk.strip(), "")
            continue

        m = RE_HDR_C.match(line)
        if m:
            if pending:
                utts.append(pending)
                pending = None
            spk, txt = m.groups()
            utts.append(Utt(0.0, spk.strip().replace(" ", ""), txt.strip()))
            continue

        if pending is not None and line.strip():
            pending.text = (pending.text + " " + line.strip()).strip()
        elif line.strip():
            # 시각도 화자도 없는 맨 텍스트. 정답지를 간단히 적을 때 쓴다.
            utts.append(Utt(0.0, PLAIN_SPK, line.strip()))

    if pending:
        utts.append(pending)

    utts = [u for u in utts if u.text]
    for u in utts:
        u.tokens = tokenize(u.text)
    return utts


def normalize(text: str) -> str:
    """비교용 정규화. 문장부호 제거, 공백 정리, 영문 소문자화, (불명) 제거."""
    t = unicodedata.normalize("NFC", text)
    t = re.sub(r"\(불명\)|\(겹침\)|\[불명\]", " ", t)
    t = PUNCT.sub(" ", t)
    t = t.lower()
    t = re.sub(r"\s+", " ", t).strip()
    return t


def tokenize(text: str) -> List[str]:
    n = normalize(text)
    return n.split() if n else []


# ─────────────────────────────────────────────────────────────
# 편집거리
# ─────────────────────────────────────────────────────────────

try:
    from rapidfuzz.distance import Levenshtein as _RF

    def edit_distance(a, b) -> int:
        return _RF.distance(a, b)
except ImportError:
    def edit_distance(a, b) -> int:
        """두 행만 유지하는 DP. 시퀀스(문자열·리스트) 모두 받는다."""
        if len(a) < len(b):
            a, b = b, a
        if not b:
            return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1,          # 삭제
                               cur[j - 1] + 1,       # 삽입
                               prev[j - 1] + (ca != cb)))  # 치환
            prev = cur
        return prev[-1]


def cer(ref: str, hyp: str) -> float:
    """문자 오류율. 한국어는 교착어라 WER보다 CER이 적절하다. 공백은 제외한다."""
    r = ref.replace(" ", "")
    h = hyp.replace(" ", "")
    if not r:
        return 0.0
    return edit_distance(r, h) / len(r)


def wer(ref_toks: List[str], hyp_toks: List[str]) -> float:
    """어절 오류율. 띄어쓰기 정책 차이가 함께 반영되므로 참고용으로만 본다."""
    if not ref_toks:
        return 0.0
    return edit_distance(ref_toks, hyp_toks) / len(ref_toks)


# ─────────────────────────────────────────────────────────────
# 정렬
# ─────────────────────────────────────────────────────────────

def flatten(utts: List[Utt]) -> Tuple[List[str], List[int]]:
    """전체 어절 시퀀스와, 각 어절이 속한 발화 인덱스를 만든다."""
    toks: List[str] = []
    owner: List[int] = []
    for i, u in enumerate(utts):
        for t in u.tokens:
            toks.append(t)
            owner.append(i)
    return toks, owner


def align(ref_toks: List[str], hyp_toks: List[str]) -> List[Tuple[int, int]]:
    """일치하는 어절 쌍의 (ref_idx, hyp_idx) 목록을 돌려준다."""
    sm = difflib.SequenceMatcher(a=ref_toks, b=hyp_toks, autojunk=False)
    pairs: List[Tuple[int, int]] = []
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            pairs.append((blk.a + k, blk.b + k))
    return pairs


# ─────────────────────────────────────────────────────────────
# 지표
# ─────────────────────────────────────────────────────────────

def speaker_metrics(ref: List[Utt], hyp: List[Utt],
                    pairs: List[Tuple[int, int]],
                    ref_owner: List[int], hyp_owner: List[int]) -> Dict:
    """
    화자 오귀속률.
    엔진의 화자 태그(SPK_0 등)와 정답 태그(코치/고객)는 이름이 다르므로,
    일치 어절 수가 최대가 되는 대응을 찾아 매핑한 뒤 계산한다.
    """
    ref_spks = sorted({u.speaker for u in ref})
    hyp_spks = sorted({u.speaker for u in hyp})

    # (정답화자, 엔진화자) 동시 출현 횟수
    tally: Dict[Tuple[str, str], int] = {}
    for ri, hi in pairs:
        key = (ref[ref_owner[ri]].speaker, hyp[hyp_owner[hi]].speaker)
        tally[key] = tally.get(key, 0) + 1

    # 탐욕 매칭. 화자 2인 기준이라 최적해와 사실상 동일하다.
    mapping: Dict[str, str] = {}
    used = set()
    for (rs, hs), _ in sorted(tally.items(), key=lambda x: -x[1]):
        if hs in mapping or rs in used:
            continue
        mapping[hs] = rs
        used.add(rs)
    for hs in hyp_spks:
        mapping.setdefault(hs, "미매핑")

    correct = sum(
        1 for ri, hi in pairs
        if mapping.get(hyp[hyp_owner[hi]].speaker) == ref[ref_owner[ri]].speaker
    )
    total = len(pairs)

    return {
        "화자오귀속률": (total - correct) / total if total else 1.0,
        "정렬어절": total,
        "엔진화자수": len(hyp_spks),
        "정답화자수": len(ref_spks),
        "화자매핑": mapping,
    }


def filler_recall(ref_text: str, hyp_text: str) -> Tuple[float, int, int, List[str]]:
    """
    간투사 보존율.
    정답에 등장한 간투사 토큰 중 엔진 출력에 살아남은 비율이다.
    이 값이 낮으면 CER이 좋아도 정본화에는 쓸 수 없다.
    """
    rt = normalize(ref_text).split()
    ht = normalize(hyp_text).split()

    ref_cnt: Dict[str, int] = {}
    for t in rt:
        if t in FILLERS:
            ref_cnt[t] = ref_cnt.get(t, 0) + 1
    if not ref_cnt:
        return 1.0, 0, 0, []

    hyp_cnt: Dict[str, int] = {}
    for t in ht:
        if t in FILLERS:
            hyp_cnt[t] = hyp_cnt.get(t, 0) + 1

    kept = sum(min(c, hyp_cnt.get(w, 0)) for w, c in ref_cnt.items())
    total = sum(ref_cnt.values())
    lost = [f"{w}×{c - hyp_cnt.get(w, 0)}"
            for w, c in ref_cnt.items() if hyp_cnt.get(w, 0) < c]
    return kept / total, kept, total, lost


def noun_recall(ref_text: str, hyp_text: str, nouns: List[str]) -> Tuple[float, List[str]]:
    """
    고유명사 재현율.
    이름·전문용어가 틀리면 부록 A 작업량이 그대로 늘어난다.
    """
    if not nouns:
        return float("nan"), []
    # "이대표님"과 "이 대표님"은 같은 것으로 본다. 띄어쓰기는 엔진마다 정책이 다르다.
    rt = normalize(ref_text).replace(" ", "")
    ht = normalize(hyp_text).replace(" ", "")
    hit, miss, total = 0, [], 0
    for n in nouns:
        nn = normalize(n).replace(" ", "")
        c_ref = rt.count(nn)
        if c_ref == 0:
            continue
        total += c_ref
        c_hyp = min(ht.count(nn), c_ref)
        hit += c_hyp
        if c_hyp < c_ref:
            miss.append(f"{n}({c_hyp}/{c_ref})")
    return (hit / total if total else float("nan")), miss


def timestamp_mae(ref: List[Utt], hyp: List[Utt],
                  pairs: List[Tuple[int, int]],
                  ref_owner: List[int], hyp_owner: List[int]) -> float:
    """
    발화 시작 시각 평균 절대오차(초).
    정답 발화마다 대응되는 엔진 발화를 다수결로 정하고 시작 시각을 비교한다.
    오프셋 차이는 중앙값을 빼서 제거한다. 정렬은 어차피 후처리에서 하기 때문이다.
    """
    votes: Dict[int, Dict[int, int]] = {}
    for ri, hi in pairs:
        r, h = ref_owner[ri], hyp_owner[hi]
        votes.setdefault(r, {})
        votes[r][h] = votes[r].get(h, 0) + 1

    diffs = []
    for r, v in votes.items():
        h = max(v.items(), key=lambda x: x[1])[0]
        diffs.append(hyp[h].start - ref[r].start)
    if not diffs:
        return float("nan")

    diffs.sort()
    offset = diffs[len(diffs) // 2]
    return sum(abs(d - offset) for d in diffs) / len(diffs)


# ─────────────────────────────────────────────────────────────
# 채점
# ─────────────────────────────────────────────────────────────

def is_plain(utts: List[Utt]) -> bool:
    """화자 표기가 전혀 없는 정답지인가."""
    return all(u.speaker == PLAIN_SPK for u in utts)


def has_clock(utts: List[Utt]) -> bool:
    """시각 표기가 있는가. 전부 0이면 없는 것으로 본다."""
    return any(u.start > 0 for u in utts)


def indel(ref_toks: List[str], hyp_toks: List[str]) -> Tuple[int, int, List[str]]:
    """
    삽입 어절과 삭제 어절을 따로 센다.
    삽입은 원본에 없는 말을 만들어낸 것이다. 오인식보다 위험하다.
    """
    sm = difflib.SequenceMatcher(a=ref_toks, b=hyp_toks, autojunk=False)
    raw_ins, dele, added = [], 0, []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "insert":
            raw_ins += hyp_toks[j1:j2]
        elif tag == "delete":
            dele += i2 - i1

    # 어순이 바뀐 어절은 삽입이자 삭제로 잡힌다. 없는 말을 만든 것이 아니므로 뺀다.
    ref_pool = Counter(ref_toks)
    hyp_pool = Counter(hyp_toks)
    # 띄어쓰기 차이도 삽입으로 잡힌다. "만나뵙게"를 "만나 뵙게"로 적으면
    # 어절 두 개가 새로 생긴 것처럼 보인다. 붙여 쓴 원문에 있으면 삽입이 아니다.
    ref_flat = "".join(ref_toks)
    true_ins = [w for w in raw_ins
                if hyp_pool[w] > ref_pool[w] and w not in ref_flat]
    moved = len(raw_ins) - len(true_ins)
    if true_ins:
        added = [" ".join(true_ins)]
    return len(true_ins), max(0, dele - moved), added


def score(ref: List[Utt], hyp: List[Utt], nouns: List[str]) -> Dict:
    ref_toks, ref_owner = flatten(ref)
    hyp_toks, hyp_owner = flatten(hyp)
    pairs = align(ref_toks, hyp_toks)

    ref_text = " ".join(ref_toks)
    hyp_text = " ".join(hyp_toks)

    fr, kept, ftotal, lost_fill = filler_recall(ref_text, hyp_text)
    ins, dele, added = indel(ref_toks, hyp_toks)
    nr, nmiss = noun_recall(ref_text, hyp_text, nouns)
    sm = speaker_metrics(ref, hyp, pairs, ref_owner, hyp_owner)
    plain = is_plain(ref)
    clock = has_clock(ref) and has_clock(hyp)

    return {
        "CER": cer(ref_text, hyp_text),
        "어절오류율": wer(ref_toks, hyp_toks),
        "화자오귀속률": float("nan") if plain else sm["화자오귀속률"],
        "정렬어절": sm["정렬어절"],
        # 텍스트도 맞고 화자도 맞은 어절이 정답 전체에서 차지하는 비율.
        # 화자오귀속률만 보면 정렬된 어절이 적은 엔진이 유리해 보이는 착시가 생긴다.
        "화자정답률": (float("nan") if plain else
                    sm["정렬어절"] * (1 - sm["화자오귀속률"]) / max(1, len(ref_toks))),
        "엔진화자수": sm["엔진화자수"],
        "간투사보존율": fr,
        "간투사": f"{kept}/{ftotal}",
        "누락간투사": lost_fill,
        "삽입어절": ins,
        "삭제어절": dele,
        "삽입내용": added,
        "고유명사재현율": nr,
        "누락고유명사": nmiss,
        "시각오차초": (timestamp_mae(ref, hyp, pairs, ref_owner, hyp_owner)
                    if clock else float("nan")),
        "발화수": len(hyp),
        "어절수": len(hyp_toks),
        "화자매핑": sm["화자매핑"],
    }


HALLUCINATION_LIMIT = 0.02      # 정답 어절 대비 삽입 비율 상한


def verdict(name: str, s: Dict, ref_spk_count: int, ref_len: int = 0) -> str:
    """
    승자 판정 규칙. 실험 전에 못 박아 두어야 사후 합리화가 생기지 않는다.
      게이트 1  삽입 어절 비율 2% 초과 → 탈락 (환각. 없는 말을 만들어냈다)
      게이트 2  간투사 보존율 80% 미만 → 탈락 (원칙 1 위반)
      게이트 3  엔진 화자 수가 정답보다 많음 → 감점 (§2-1 분리 오류)
      순위      화자 오귀속률 → CER 순

    삽입을 첫 게이트에 둔 이유는 복구 불가능하기 때문이다.
    누락과 오인식은 부록 B로 보류할 수 있으나, 없던 발화는 걸러낼 근거가 없다.
    """
    flags = []
    ins = s.get("삽입어절", 0)
    if ins >= 2 and ref_len and ins / ref_len > HALLUCINATION_LIMIT:
        flags.append(f"탈락:환각{ins}어절")
    elif ins:
        flags.append(f"주의:삽입{ins}어절")
    if s["간투사보존율"] < 0.80:
        flags.append("탈락:간투사삭제")
    if ref_spk_count > 0 and s["엔진화자수"] > ref_spk_count:
        flags.append(f"주의:화자{s['엔진화자수']}개")
    if s["화자오귀속률"] == s["화자오귀속률"] and s["화자오귀속률"] > 0.10:
        flags.append("주의:화자오귀속10%초과")
    return " ".join(flags) if flags else "통과"


def fmt_clock(sec: float) -> str:
    return f"{int(sec) // 60:02d}:{int(sec) % 60:02d}"


def fmt_pct(v: float) -> str:
    return "  n/a " if v != v else f"{v * 100:6.2f}%"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="STT 엔진 비교 채점기",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reference", nargs="?", help="정답 전사 파일")
    ap.add_argument("hypotheses", nargs="*", help="엔진 출력 파일 (여러 개)")
    ap.add_argument("--nouns", nargs="*", default=[],
                    help="추적할 고유명사 목록 (이름, 전문용어)")
    ap.add_argument("--template", action="store_true",
                    help="정답 전사 템플릿을 출력하고 종료")
    ap.add_argument("--detail", action="store_true",
                    help="엔진별 상세 진단을 함께 출력")
    ap.add_argument("--full", action="store_true",
                    help="구간 자동 절단을 끄고 엔진 출력 전체를 비교")
    ap.add_argument("--margin", type=float, default=8.0,
                    help="구간 경계 여유 초 (기본 8)")
    args = ap.parse_args()

    if args.template:
        print(TEMPLATE)
        return 0

    if not args.reference or not args.hypotheses:
        ap.print_help()
        return 1

    ref = parse(args.reference)
    if not ref:
        print(f"오류: 정답 파일에서 발화를 찾지 못했다 — {args.reference}", file=sys.stderr)
        return 1

    ref_toks, _ = flatten(ref)
    ref_spk_count = len({u.speaker for u in ref})

    plain_ref = is_plain(ref)
    clock_ref = has_clock(ref)
    win_lo = min(u.start for u in ref)
    win_hi = max(u.start for u in ref)
    if not clock_ref:
        args.full = True          # 시각이 없으면 잘라낼 기준이 없다
    if plain_ref:
        ref_spk_count = 0         # 화자 지표는 재지 않는다

    print()
    print(f"정답  {os.path.basename(args.reference)}   "
          f"발화 {len(ref)}건 · 어절 {len(ref_toks)}개"
          + (f" · 화자 {ref_spk_count}명" if ref_spk_count else ""))
    if plain_ref or not clock_ref:
        note = []
        if plain_ref:
            note.append("화자 표기 없음 → 화자 지표 생략")
        if not clock_ref:
            note.append("시각 표기 없음 → 구간 절단·시각 지표 생략")
        print("      " + " · ".join(note))
    elif args.full:
        print("      구간 절단 없음 (--full)")
    else:
        print(f"      비교 구간 {fmt_clock(win_lo)}~{fmt_clock(win_hi)} "
              f"(경계 여유 {args.margin:.0f}초). 엔진 출력에서 이 범위만 잘라 비교한다.")
    print()

    header = (f"{'엔진':<24}{'CER':>8}{'어절오류':>9}{'화자오귀속':>11}{'화자정답률':>11}"
              f"{'간투사보존':>11}{'고유명사':>9}{'시각오차':>9}{'화자수':>7}  판정")
    print(header)
    print("─" * len(header))

    # 윈도우 PowerShell은 와일드카드를 확장하지 않는다. 직접 처리한다.
    hyp_paths = []
    for pat in args.hypotheses:
        hyp_paths.extend(sorted(glob.glob(pat)) or [pat])
    hyp_paths = [p for p in hyp_paths if os.path.isfile(p)]
    if not hyp_paths:
        print("엔진 출력 파일을 찾지 못했다.", file=sys.stderr)
        return 1

    results = []
    for path in hyp_paths:
        try:
            hyp = parse(path)
        except OSError as e:
            print(f"{os.path.basename(path):<24}  읽기 실패: {e}")
            continue
        if not args.full:
            hyp = [u for u in hyp
                   if win_lo - args.margin <= u.start <= win_hi + args.margin]
        if not hyp:
            print(f"{os.path.basename(path):<24}  구간 내 발화 없음")
            continue

        s = score(ref, hyp, args.nouns)
        name = os.path.splitext(os.path.basename(path))[0][:23]
        ts = "  n/a " if s["시각오차초"] != s["시각오차초"] else f"{s['시각오차초']:7.2f}s"
        print(f"{name:<24}{fmt_pct(s['CER']):>8}{fmt_pct(s['어절오류율']):>9}"
              f"{fmt_pct(s['화자오귀속률']):>11}{fmt_pct(s['화자정답률']):>11}"
              f"{fmt_pct(s['간투사보존율']):>11}"
              f"{fmt_pct(s['고유명사재현율']):>9}{ts:>9}{s['엔진화자수']:>7}"
              f"  {verdict(name, s, ref_spk_count, len(ref_toks))}")
        results.append((name, s))

    if args.detail:
        for name, s in results:
            print()
            print(f"── {name} ──")
            print(f"   발화 수      {s['발화수']}건 (정답 {len(ref)}건)")
            print(f"   어절 수      {s['어절수']}개 (정답 {len(ref_toks)}개)")
            print(f"   간투사       {s['간투사']} 보존"
                  + (f"  ·  누락 {', '.join(s['누락간투사'])}" if s['누락간투사'] else ""))
            print(f"   삽입/삭제    삽입 {s['삽입어절']}어절 · 삭제 {s['삭제어절']}어절")
            print(f"   정렬 어절    {s['정렬어절']}개 — 화자 지표는 이 범위에서만 잰다")
            if s["삽입내용"]:
                print(f"   삽입 내용    {' | '.join(s['삽입내용'][:5])}")
            print(f"   화자 매핑    {s['화자매핑']}")
            if s["누락고유명사"]:
                print(f"   누락 고유명사 {', '.join(s['누락고유명사'])}")

    print()
    print("판정 규칙  삽입 어절이 정답의 2%를 넘으면 탈락한다. 없는 발화는 복구할 방법이 없다.")
    print("           간투사 보존율 80% 미만도 탈락한다. 원칙 1을 지킬 수 없기 때문이다.")
    print("           통과한 엔진 중 화자 오귀속률이 낮은 쪽을 우선하고, 같으면 CER로 가른다.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
