"""한국어 형태소 토큰화 (Kiwi) — 적재·쿼리 공용 단일 소스.

FTS5는 한국어 형태소를 모르므로, 앞단에서 Kiwi로 원형(lemma) 분해해 공백 조인한
문자열을 만들어 둔다(적재 시 corpus_chunks.text_tokenized, 쿼리 시 질의 토큰화).
**적재와 쿼리가 반드시 이 같은 함수를 써야** 매칭이 깨지지 않는다(설계문서 §6 조건).

의존: kiwipiepy (pip install kiwipiepy — JVM·외부사전 불필요).
"""
import threading

_kiwi = None
_lock = threading.Lock()


def _get_kiwi():
    global _kiwi
    if _kiwi is None:
        with _lock:
            if _kiwi is None:
                from kiwipiepy import Kiwi  # 지연 임포트 — 미설치 환경 기동 보호
                _kiwi = Kiwi()
    return _kiwi


def tokenize_for_search(text: str) -> str:
    """원형(lemma) 기준 공백 조인. 적재·쿼리 동일 사용.

    예: "환불 규정을 알려줘" -> "환불 규정 을 알리 어 주"
    빈/None 입력은 "" 반환(FTS5 빈 문서 가드).
    """
    if not text:
        return ""
    return " ".join(t.lemma for t in _get_kiwi().tokenize(text))


if __name__ == "__main__":  # 자체 점검 (kiwipiepy 설치 시)
    a = tokenize_for_search("환불 규정을 알려줘")
    b = tokenize_for_search("환불은 어떻게 하나요")
    assert "환불" in a.split() and "환불" in b.split(), (a, b)  # 조사 제거 → 어근 공유
    assert tokenize_for_search("") == ""
    print("ok:", a)
