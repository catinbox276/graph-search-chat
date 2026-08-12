"""배치 엔트리 — python -m graph.graph_pipeline (야간 CronJob 03:00과 동일).

실행 결과(성공/실패·소요시간·스택트레이스)를 활동 로그(app_events)에 남긴다.
"""
import time
import traceback

from core import events

from .run import main

if __name__ == "__main__":
    t0 = time.time()
    try:
        main()
        events.log("batch", source="graph-pipeline", level="info", status="ok",
                   duration_ms=int((time.time() - t0) * 1000), summary="graph-pipeline 완료")
    except Exception as e:
        events.log("batch", source="graph-pipeline", level="error", status="fail",
                   duration_ms=int((time.time() - t0) * 1000),
                   summary=f"{type(e).__name__}: {str(e)[:200]}",
                   detail=traceback.format_exc())
        raise
