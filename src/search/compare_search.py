"""검색 백엔드 A/B 비교 — 같은 질의로 oracle vs inmemory 결과를 나란히 출력.

전제: tokenize_corpus.py(text_tokenized) + embed_corpus.py(embedding) 백필 완료,
      임베딩 모델 서빙(시맨틱 경로). 렉시컬만이면 임베딩 없이도 비교 가능.
usage: .venv/bin/python search/compare_search.py "질의1" "질의2" ...
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from core import config  # noqa: E402

QUERIES = sys.argv[1:] or [
    "사내망에서 파이썬 패키지 설치가 안 됨",
    "환불 규정",
]


def run(engine: str, q: str) -> str:
    config.SEARCH_ENGINE = engine          # 런타임 스위치(모듈 재임포트 불필요)
    from search import corpus_search
    if engine == "oracle":
        corpus_search.load_matrix()
    return corpus_search.search_docs(q, 5)


if __name__ == "__main__":
    for q in QUERIES:
        print("=" * 70, f"\nQ: {q}")
        for eng in ("oracle", "inmemory"):
            print(f"\n--- {eng} ---")
            try:
                print(run(eng, q))
            except Exception as e:  # noqa: BLE001 — 비교용, 실패도 그대로 보여줌
                print(f"[{eng}] 실패: {e}")
