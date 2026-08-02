"""현재 처리 중인 세션 id를 도구까지 전달하는 contextvar.

서버가 요청 시작 시 set, suggest_paths가 노출 기록에 사용.
(langchain 동기 도구는 copy_context로 실행되어 contextvar가 전파됨)
"""
from contextvars import ContextVar

current_session: ContextVar[str | None] = ContextVar("current_session", default=None)
