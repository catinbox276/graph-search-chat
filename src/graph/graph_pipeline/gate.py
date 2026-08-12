"""세션 게이트 — 세션 턴 로드 + 세그먼트 분할 + 행동 신호 판정 (전부 룰/코드, LLM 없음).

design §3: 실사용(UI) 세션에는 정답 기준(expect)이 없으므로, 감정·말투가 아니라
행동 신호(재질문·정정·재방문·조급함 / 구체화·화제 전진·조용한 종료)를 코드로 센다.
"""
import json
import re
import statistics

from core import config

from .llm import cosine, embed

# 정정 언어 — 사용자 턴 앞머리의 부정·정정 표현 (design §3: 사용자 턴만 본다)
CORRECTION_RE = re.compile(
    r"^\s*(아니|아뇨|아니요|아닌데)\b|그게 아니라|그거 말고|내가 말한 건|틀렸|잘못 (알|이해|찾)")
# 구체화 신호용 식별자 — 영문 식별자(테이블·컴포넌트명)나 3자리 이상 숫자(로그ID 등)
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{2,}|\d{3,}")
# 명시적 실패 인정 — 에이전트 최종 답변의 "접근이 막혔음" 표지 (design §3 NG 분기).
# 사용자 감정 분석이 아니라 결정적 텍스트 표지 — 조용한 종료의 오탐(포기=성공)을 막는다.
FAIL_ADMIT_RE = re.compile(
    r"찾지 못했|찾을 수 없|존재하지 않|조회(가|할 수) 불가능|접근 권한이 없|"
    r"데이터가 없|해당하는 (글|문서|데이터셋?)[이가] 없")


def _read(v):
    return v.read() if hasattr(v, "read") else (v or "")


def session_turns(cur, sid):
    """세션의 전 턴을 시간순으로 — 신호 계산과 다턴 집계용."""
    cur.execute("""SELECT turn, ts, question, tool_calls, answer FROM sessions
                   WHERE id = :1 ORDER BY turn""", [sid])
    return [{"turn": t, "ts": ts, "q": _read(q),
             "calls": json.loads(_read(c) or "[]"), "a": _read(a)}
            for t, ts, q, c, a in cur.fetchall()]


def split_segments(turns):
    """세션을 태스크 단위 세그먼트로 분할 — 인접 질문 임베딩이 SEG_SPLIT_SIM보다
    멀면 화제가 꺾인 것으로 보고 자른다. 게이트·추출은 세그먼트마다 독립 적용.

    "세션 1개 = 문제 1개" 가정의 보강: 한 세션에서 A를 풀고 B로 넘어가면
    A·B가 따로 판정·추출된다 (첫 질문/마지막 답변 짝짝이 방지 + 자산 회수).
    1턴이거나 경계가 없으면 세그먼트 1개(기존 동작과 동일)."""
    if len(turns) < 2:
        return [turns]
    vecs = [embed(t["q"][:500]) for t in turns]
    segs, cur_seg = [], [turns[0]]
    for prev, nxt, va, vb in zip(turns, turns[1:], vecs, vecs[1:]):
        if cosine(va, vb) < config.SEG_SPLIT_SIM:
            segs.append(cur_seg)
            cur_seg = []
        cur_seg.append(nxt)
    segs.append(cur_seg)
    return segs


def judge_by_signals(turns):
    """실사용(UI) 세션 판정 — 감정·말투가 아니라 행동 신호를 코드로 센다 (design §3 보강).

    후퇴 2개 이상 -> fail / 전진 있고 후퇴 없음 -> success / 나머지 -> unknown(미판정 유지).
    '이탈'(답변 후 무응답 종료)은 배치 시점엔 모든 세션이 그렇게 보여 신호로 쓰지 않는다.
    재발(N일 내 같은 증상 재방문)은 즉시 신호가 아니라 retract_recurrences()의
    소급 취소로 처리한다 — "조용한 종료"를 지금 success로 주고 재발이 나중에 교정.
    """
    qs = [t["q"] for t in turns]
    retreat, forward = [], []
    # 명시적 실패 인정 — 에이전트가 최종 답변에서 접근 불가를 인정하면 즉시 fail
    # (design §3 NG 분기: 데이터/글이 존재하지 않아 목표 달성 불가 + 답변이 인정)
    if turns and FAIL_ADMIT_RE.search(turns[-1]["a"]):
        return "fail", "명시적 실패 인정"
    # 후퇴: 정정 언어 (2턴째부터 — 첫 질문의 "아니"는 정정이 아님)
    if any(CORRECTION_RE.search(q) for q in qs[1:]):
        retreat.append("정정 언어")
    # 후퇴: 문서 재방문 — 같은 글을 다른 턴에서 다시 읽음
    seen = {}
    for t in turns:
        for c in t["calls"]:
            if c.get("name") in ("read_doc", "read_blog_post"):  # 구명 세션 호환
                pid = json.dumps(c.get("args", {}), sort_keys=True, ensure_ascii=False)
                seen.setdefault(pid, set()).add(t["turn"])
    if any(len(v) > 1 for v in seen.values()):
        retreat.append("문서 재방문")
    # 후퇴: 조급함 — 턴 간격이 중앙값 대비 급감
    ts = [t["ts"] for t in turns if t["ts"]]
    gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
    if len(gaps) >= 2 and gaps[-1] < statistics.median(gaps) * config.SIG_HASTY_RATIO:
        retreat.append("조급함")
    # 재질문(후퇴) vs 화제 전진(전진) — 질문 임베딩, 2턴 이상일 때만 계산
    if len(qs) >= 2:
        vecs = [embed(q[:500]) for q in qs]
        adj = [cosine(a, b) for a, b in zip(vecs, vecs[1:])]
        if max(adj) >= config.SIG_REPEAT_SIM:
            retreat.append("재질문")
        elif cosine(vecs[0], vecs[-1]) < config.SIG_TOPIC_MOVE_SIM:
            forward.append("화제 전진")  # 멀어지고 되돌아오지 않음 (재질문 없음이 전제)
        # 전진: 구체화 — 증상 서술에서 컴포넌트명·식별자로 좁혀 들어감
        if len(IDENT_RE.findall(qs[-1])) > len(IDENT_RE.findall(qs[0])):
            forward.append("구체화")
    # 전진: 조용한 종료 — 정정 없이, 도구 근거가 있는 답으로 끝남
    if not retreat and any(t["calls"] for t in turns) \
            and not turns[-1]["a"].startswith("[에이전트 오류]"):
        forward.append("조용한 종료")
    if len(retreat) >= 2:
        return "fail", ", ".join(retreat)
    if forward and not retreat:
        return "success", ", ".join(forward)
    return "unknown", ", ".join(retreat + forward)
