"""그래프 파이프라인 — sessions -> 게이트 판정 -> 4계층 추출 -> nodes/edges 적재.

design.md §2~§5 구현. 역할별 모듈 (한 파일 = 한 역할):
- schema.py  — 그래프 DDL + 도메인 닫힌 목록(시드·분류)
- llm.py     — 모델 호출 계층 (챗·프롬프트·임베딩) — 모델을 부르는 곳은 여기뿐
- gate.py    — 세션 게이트 (세그먼트 분할 + 행동 신호 판정 — 전부 룰/코드)
- merge.py   — 클러스터/dedup (임베딩→문자 가드→LLM 선택 3단 병합)
- weights.py — 가중치 보정 (채택률 재계산 + 재발 소급 취소)
- run.py     — 오케스트레이션 (main)

구 graph_pipeline.py(단일 673줄)에서 승격 — 외부 import 경로는 아래 re-export로 불변.
usage: PYTHONPATH=src .venv/bin/python -m graph.graph_pipeline
"""
from .gate import (  # noqa: F401
    CORRECTION_RE,
    FAIL_ADMIT_RE,
    IDENT_RE,
    judge_by_signals,
    session_turns,
    split_segments,
)
from .llm import (  # noqa: F401
    CHAT_MODEL,
    EXTRACT_PROMPT,
    JUDGE_PROMPT,
    cosine,
    embed,
    llm,
    llm_same,
    llm_select,
)
from .merge import LAYER_KIND, SIM_HIGH, SIM_THRESHOLD, get_or_create  # noqa: F401
from .run import expects, main  # noqa: F401
from .schema import (  # noqa: F401
    DATAHUB_TOOLS,
    SEED_DOMAINS,
    classify_domain,
    ddl,
    ensure_domain_registry,
)
from .weights import recompute_weights, retract_recurrences  # noqa: F401
