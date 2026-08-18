"""문서 구조화 예약 스케줄러 — 인앱, 앱 프로세스 1개(단일 워커/레플리카) 전제.

요구(관리 UI):
- 예약 시각(HH:MM, KST) + 활성/비활성 토글. 상태는 app_settings에 영속.
- 처리 중이면 예약 시각이어도 시작하지 않는다(중복 금지). 놓친 회차는 **당겨 실행하지
  않고** 다음 날 같은 시각에만(미실행분 이월 없음).
- 중지: 실행 중이면 배치 경계에서 협조적 취소 + 영속 플래그(sched_stopped). 페이지를
  껐다 켜도 유지되고, '다시 시작'으로 해제해야 재개된다.
- 실행 상태(진행 소스·처리 건수)는 status()로 노출 → 처리 현황이 폴링.

ponytail: 단일 워커 전제 — 상태를 인메모리 _state로 든다. 다중 워커면 예약이 워커마다
떠서 중복 실행되므로 uvicorn --workers 1 유지(현재 배포 그대로). 시간은 tzdata 의존 없이
고정 KST(+9) — 한국은 DST 없어 항상 정확.
"""
import threading
import time as _time
from datetime import datetime, timedelta, timezone

import oracledb

from core import config

KST = timezone(timedelta(hours=9))
DEFAULT_TIME = "03:40"   # 기존 야간 배치 시각

_lock = threading.Lock()
_state = {"running": False, "trigger": "", "current": "", "processed": 0, "started": ""}
_started = False

# 다른 곳(수동 '지금 구조화')의 처리 여부 — server.py가 startup에서 주입(순환 import 회피).
external_busy = None


def _settings() -> dict:
    from core import settings
    return settings.get_all()


def _set(d: dict):
    from core import settings
    settings.set_many(d)


def is_stopped() -> bool:
    try:
        return _settings().get("sched_stopped", "") == "1"
    except Exception:
        return False


def _should_stop() -> bool:
    return is_stopped()


def is_running() -> bool:
    return _state["running"]


def _busy() -> bool:
    return _state["running"] or bool(external_busy and external_busy())


def get_schedule(st: dict | None = None) -> dict:
    st = st if st is not None else _settings()
    return {"enabled": st.get("sched_enabled", "") == "1",
            "time": st.get("sched_time", "") or DEFAULT_TIME,
            "stopped": st.get("sched_stopped", "") == "1",
            "last_run": st.get("sched_last_run", "") or ""}


def set_schedule(enabled: bool, time_hhmm: str):
    hhmm = (time_hhmm or "").strip()
    try:  # HH:MM 검증 — 잘못되면 기본값
        datetime.strptime(hhmm, "%H:%M")
    except ValueError:
        hhmm = DEFAULT_TIME
    _set({"sched_enabled": "1" if enabled else "", "sched_time": hhmm})


def stop():
    """중지 — 영속 플래그. 실행 중이면 다음 배치 경계에서 멈춘다."""
    _set({"sched_stopped": "1"})


def resume():
    """다시 시작 — 중지 해제. 이후 예약 시각에 다시 실행되고, 수동 실행도 가능."""
    _set({"sched_stopped": ""})


def status() -> dict:
    sc = get_schedule()
    return {**sc, "running": _state["running"], "trigger": _state["trigger"],
            "current": _state["current"], "processed": _state["processed"],
            "started": _state["started"]}


def _eligible_sources() -> list:
    """도메인 지정·활성·비대화(scope!=chat) 소스 — run_for_source와 같은 기준."""
    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD,
                           dsn=config.ORACLE_DSN)
    try:
        cur = con.cursor()
        cur.execute("""SELECT s.source_name FROM source_registry s
                       JOIN domain_registry d ON d.name = s.domain
                       WHERE s.enabled = 'Y' AND s.domain IS NOT NULL
                         AND NVL(d.scope, 'both') != 'chat'
                       ORDER BY s.source_name""")
        return [r[0] for r in cur.fetchall()]
    finally:
        con.close()


def run_all(trigger: str) -> bool:
    """모든 대상 소스를 끝까지(drain) 구조화 — 백그라운드 스레드. 이미 실행 중이거나
    중지 상태면 시작하지 않는다. 시작했으면 True."""
    from graph.doc_pipeline.run import run_for_source
    from core import events
    with _lock:
        if _busy() or is_stopped():
            return False
        _state.update(running=True, trigger=trigger, processed=0, current="",
                      started=datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"))

    def _job():
        t0 = _time.time()
        try:
            for src in _eligible_sources():
                if _should_stop():
                    break
                _state["current"] = src
                r = run_for_source(src, drain=True, should_stop=_should_stop)
                _state["processed"] += int(r.get("processed", 0) or 0)
            events.log("batch", source="doc-structure-sched", level="info", status="ok",
                       actor=trigger, duration_ms=int((_time.time() - t0) * 1000),
                       summary=f"예약 구조화({trigger}): {_state['processed']}건"
                               + (" · 중지됨" if _should_stop() else ""))
        except Exception as e:
            import traceback
            events.log("batch", source="doc-structure-sched", level="error", status="fail",
                       actor=trigger, summary=f"{type(e).__name__}: {str(e)[:200]}",
                       detail=traceback.format_exc())
        finally:
            with _lock:
                _state.update(running=False, current="")

    threading.Thread(target=_job, daemon=True).start()
    return True


def _loop():
    while True:
        try:
            st = _settings()
            sc = get_schedule(st)
            if sc["enabled"] and not sc["stopped"] and not _busy():
                now = datetime.now(KST)
                today = now.strftime("%Y-%m-%d")
                if now.strftime("%H:%M") == sc["time"] and sc["last_run"] != today:
                    _set({"sched_last_run": today})  # 먼저 기록 → 같은 분 재실행 방지
                    run_all("schedule")
        except Exception:
            pass  # 스케줄러는 절대 죽지 않는다
        _time.sleep(30)


def start_scheduler():
    """앱 startup에서 1회 호출 — 예약 확인 데몬 스레드 기동(멱등)."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()
