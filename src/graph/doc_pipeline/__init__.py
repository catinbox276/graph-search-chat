"""적재 문서 → 지식그래프 구조화 (docs/plan.md §7의 '문서 구조화').

소스 관리에서 **도메인이 지정된** 소스의 corpus_docs 문서를, LLM이 그 도메인의
정의·추출 지침(domain_registry.extract_hint)을 기준으로 판정·구조화한다:

- 기준에 맞으면(fits=true): 목표·접근법을 추출해 대화와 같은 그래프에 병합
  (graph_pipeline.get_or_create 재사용 — 2단계 임베딩→LLM dedup 동일 적용)
- 기준 미달이면(fits=false): graph_status='excluded' — 그래프에 안 들어간다
- 증거는 node_evidence(kind='doc', ref='소스명:원천id') — 세션 증거와 분리되므로
  성공/실패 판정 카운트에는 안 섞이고, 통행(raw_count)·출처 추적에만 기여한다.

역할별 모듈 (한 파일 = 한 역할):
- judge.py — LLM 판정 (프롬프트·단건/묶음 판정 — DB 없음, 스레드 병렬 안전)
- run.py   — 배치 오케스트레이션 (조회→동시 판정→직렬 병합→상태 기록)

usage: PYTHONPATH=src .venv/bin/python -m graph.doc_pipeline [--limit N]
"""
from .judge import PACK_MAX_DOCS, judge_doc, judge_pack  # noqa: F401
from .run import doc_ddl, main, run_for_source  # noqa: F401
