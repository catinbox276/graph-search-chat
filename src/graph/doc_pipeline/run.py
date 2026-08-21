"""배치 오케스트레이션 — 문서 조회 → 동시 판정(judge) → 직렬 병합 → 상태 기록.

실행 구조: LLM 판정은 동시(스레드풀, 판정만 — DB 없음), 그래프 병합은 직렬
(커서 공유 안전). 멱등·이어하기: graph_status IS NULL만 처리, 문서마다 커밋.
"""
import argparse
import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from types import SimpleNamespace

import oracledb

from core import config, settings
from graph.graph_pipeline import CHAT_MODEL, ddl, get_or_create
from graph.graph_pipeline.merge import apply_extras, default_merge_cfg

from . import runs
from .judge import DEFAULT_SCHEMA, PACK_MAX_DOCS, chain_view, classify_doc, judge_pack


def doc_ddl(cur):
    """corpus_docs에 구조화 상태 컬럼 추가 (멱등)."""
    for col, spec in (("GRAPH_STATUS", "graph_status VARCHAR2(20)"),
                      ("GRAPH_NOTE", "graph_note VARCHAR2(1000)")):
        cur.execute("""SELECT COUNT(*) FROM user_tab_columns
                       WHERE table_name = 'CORPUS_DOCS' AND column_name = :1""", [col])
        if not cur.fetchone()[0]:
            cur.execute(f"ALTER TABLE corpus_docs ADD ({spec})")


def _load_settings(limit_override: int = 0) -> SimpleNamespace:
    """운영 설정 로드 — app_settings(관리 UI) 우선, 없으면 .env 기본값.
    main()·run_for_source() 공용 (같은 블록이 두 곳에 있던 것을 합침)."""
    st = settings.get_all()
    return SimpleNamespace(
        limit=limit_override or settings.get_int(st, "doc_extract_limit",
                                                 config.DOC_EXTRACT_LIMIT),
        conc=max(1, settings.get_int(st, "doc_concurrency", config.DOC_CONCURRENCY)),
        body_chars=settings.get_int(st, "doc_body_chars", config.DOC_BODY_CHARS),
        pack_tokens=settings.get_int(st, "doc_pack_tokens", config.DOC_PACK_TOKENS),
        no_think=bool(settings.get_int(st, "doc_no_think", config.DOC_NO_THINK)),
        model=(st.get("doc_extract_model") or "").strip(),
        doc_prompt=(st.get("struct_doc_prompt") or "").strip(),    # 빈값=코드 기본(judge)
        pack_prompt=(st.get("struct_pack_prompt") or "").strip(),
        dedup=default_merge_cfg(),   # 클러스터(dedup) 기본 — run 지정 시 스냅샷으로 덮음
        schema=DEFAULT_SCHEMA,       # 추출 스키마 — run 지정 시 스냅샷으로 덮음
        embed_model="",              # run별 임베딩 — 빈값=기본
        lines=None)                  # 라우터 run 라인 후보 — run 스냅샷만 채움


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="이번 실행 처리 문서 수 (0=설정값 doc_extract_limit)")
    args = ap.parse_args()

    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD, dsn=config.ORACLE_DSN)
    cur = con.cursor()
    ddl(cur)      # nodes/edges/node_evidence/domain_registry 보장
    doc_ddl(cur)
    con.commit()
    s = _load_settings(args.limit)
    print(f"설정: limit={s.limit} concurrency={s.conc} body_chars={s.body_chars} "
          f"pack_tokens={s.pack_tokens} no_think={s.no_think} "
          f"model={s.model or CHAT_MODEL}", flush=True)

    # 도메인이 지정된 소스만 대상 (미지정 = 검색 전용, 그래프화 안 함).
    # 대화 전용(scope=chat) 도메인은 제외 — 등록 API가 막지만 SQL 직접 수정 대비 2차 방어.
    cur.execute("""SELECT s.source_name, s.domain, NVL(d.extract_hint, ' ')
                   FROM source_registry s
                   JOIN domain_registry d ON d.name = s.domain
                   WHERE s.enabled = 'Y' AND s.domain IS NOT NULL
                     AND NVL(d.scope, 'both') != 'chat'""")
    sources = cur.fetchall()
    if not sources:
        print("그래프 구조화 대상 소스 없음 (소스 관리에서 도메인을 지정하면 대상이 됨)")
        return

    budget = s.limit
    stats = {"done": 0, "excluded": 0, "error": 0}
    for source_name, domain, hint in sources:
        if budget <= 0:
            break
        rid = runs.current_run(cur, source_name)
        # 활성 run 스냅샷 적용 — run은 "이 조합으로 판정한다"는 약속이라 야간 배치도 따라야 한다.
        # (2026-08-21 버그: 이 줄이 없어 CLI·야간 배치가 코드 기본 스키마로 판정했고, 관리자가
        #  정의한 속성이 통째로 빠졌다. HTTP '지금 구조화' 버튼만 스냅샷을 적용하고 있었다.)
        # s는 소스 루프 공유 상태라 소스별 복사본에 덮는다 — 스냅샷이 다음 소스로 새지 않게.
        ss, r_count = SimpleNamespace(**vars(s)), True
        try:
            r_domain, r_hint, r_count = _run_overrides(cur, rid, ss)
            domain, hint = r_domain or domain, r_hint or hint
        except ValueError:
            ss = s          # run 행이 없으면(구버전 DB) 종전대로 코드 기본
        n = _structure_one(cur, con, source_name, domain, hint, budget, ss, stats, rid,
                           count=r_count)
        if n:
            runs.finish_run(cur, rid)
            con.commit()
        budget -= n

    cur.execute("""SELECT NVL(graph_status, '미처리'), COUNT(*) FROM corpus_docs
                   GROUP BY graph_status""")
    print(f"\n이번 실행: {stats} / 전체 현황: {dict(cur.fetchall())}")
    con.close()


def _structure_one(cur, con, source_name, domain, hint, budget, s, stats, run_id="-",
                   count=True, by_run=False, should_stop=None):
    """소스 1개의 미처리 문서를 최대 budget건 판정·병합. 처리한 문서 수를 반환.
    main()(전 소스 루프)과 run_for_source()(즉시 실행) 공용. s = _load_settings().
    by_run=True: '이 run이 아직 판정 안 한 문서'가 대상 (활성 캐시와 무관 — 재판정용).
    count=False: 비활성 run — 엣지 가중치를 올리지 않고 corpus_docs 캐시도 안 건드림."""
    if by_run:
        cur.execute("""SELECT c.src_id, NVL(c.title, ' '), NVL(c.kind, ' '), c.body
                       FROM corpus_docs c
                       WHERE c.source_name = :1
                         AND NOT EXISTS (SELECT 1 FROM doc_results r
                                         WHERE r.run_id = :2 AND r.source_name = c.source_name
                                           AND r.src_id = c.src_id)
                       ORDER BY c.src_id
                       FETCH FIRST :3 ROWS ONLY""", [source_name, run_id, budget])
    else:
        cur.execute("""SELECT src_id, NVL(title, ' '), NVL(kind, ' '), body
                   FROM corpus_docs
                   WHERE source_name = :1 AND graph_status IS NULL
                   ORDER BY src_id
                   FETCH FIRST :2 ROWS ONLY""", [source_name, budget])
    # CLOB은 fetch 직후 바로 읽는다 — SQL dbms_lob.substr는 한글에서 VARCHAR2
    # 4000바이트 한계로 ORA-06502가 나고, 로케이터를 커밋 뒤까지 들고 있지 않기 위해
    docs = [(r[0], r[1], r[2],
             r[3].read() if hasattr(r[3], "read") else (r[3] or ""))
            for r in cur.fetchall()]
    if not docs:
        return 0
    lines = getattr(s, "lines", None)
    if lines:
        # 라우터 run — 문서마다 저비용 분류(라인 후보 중 하나 또는 제외) 후
        # 라인별 그룹으로 나눠 각 라인의 스키마·프롬프트로 판정·병합.
        # 분류는 순수 계층(DB 없음)이라 스레드 병렬, 기록·병합은 직렬 유지.
        with ThreadPoolExecutor(max_workers=s.conc) as rex:
            verdicts = list(rex.map(
                lambda d: classify_doc(domain, lines, d[2], d[1], d[3],
                                       model=s.model, no_think=s.no_think), docs))
        by_line, counts = {}, {}
        for d, v in zip(docs, verdicts):
            pt, ct = v.get("_usage", (0, 0))
            stats["in_tok"] = stats.get("in_tok", 0) + pt
            stats["out_tok"] = stats.get("out_tok", 0) + ct
            if v.get("_error"):     # 분류 실패 = error (제외 아님 — 실패 재시도 대상)
                status, note, key = "error", ("라우터: " + v["_error"])[:1000], "오류"
            elif not v.get("line"):
                status = "excluded"
                note = ("라우터 제외: " + (v.get("reason") or "해당 유형 없음"))[:1000]
                key = "제외"
            else:
                by_line.setdefault(v["line"], []).append(d)
                counts[v["line"]] = counts.get(v["line"], 0) + 1
                continue
            counts[key] = counts.get(key, 0) + 1
            if count:  # 활성 run만 corpus_docs 캐시 갱신
                cur.execute("""UPDATE corpus_docs SET graph_status = :1, graph_note = :2
                               WHERE source_name = :3 AND src_id = :4""",
                            [status, note or None, source_name, d[0]])
            if run_id != "-":
                runs.record_result(cur, run_id, source_name, d[0], status, note)
            stats[status] += 1
        con.commit()
        print(f"[라우터] {len(docs)}건 분류 → "
              + " · ".join(f"{k} {n}" for k, n in counts.items()), flush=True)
        for ln in lines:   # 스냅샷 순서대로 라인별 판정·병합
            grp = by_line.get(ln["line"])
            if not grp:
                continue
            if should_stop and should_stop():
                break
            _judge_merge(cur, con, source_name, domain, hint, grp, s, stats,
                         run_id, count, should_stop,
                         ln["schema"], ln["doc_prompt"], ln["pack_prompt"], ln["line"])
        return len(docs)
    return _judge_merge(cur, con, source_name, domain, hint, docs, s, stats,
                        run_id, count, should_stop,
                        s.schema, s.doc_prompt, s.pack_prompt)


def _judge_merge(cur, con, source_name, domain, hint, docs, s, stats,
                 run_id, count, should_stop, schema, doc_prompt, pack_prompt,
                 line_name=""):
    """문서 목록을 주어진 스키마·프롬프트로 판정·병합 (기존 _structure_one 본문 추출).
    단일 run은 1회 호출(동작 불변), 라우터 run은 라인 그룹마다 호출 —
    mc/스키마 키를 지역 계산해 그룹 간 공유 상태가 없다. 처리 문서 수 반환."""
    # 묶음 구성: 입력 토큰 예산(문자 ≈ 토큰×2 근사)까지 문서를 묶는다.
    # 0이면 1건씩. 묶음당 상한 PACK_MAX_DOCS — 출력 길이·판정 품질 보호.
    if s.pack_tokens <= 0:
        packs = [[d] for d in docs]
    else:
        budget_chars = s.pack_tokens * 2
        packs, pk, chars = [], [], 0
        for d in docs:
            dlen = min(len(d[3] or ""), s.body_chars) + 400  # 제목·스캐폴드 여유
            if pk and (len(pk) >= PACK_MAX_DOCS or chars + dlen > budget_chars):
                packs.append(pk)
                pk, chars = [], 0
            pk.append(d)
            chars += dlen
        if pk:
            packs.append(pk)
    tag = f" [라인 {line_name}]" if line_name else ""
    print(f"[{source_name}] 도메인 '{domain}' 기준 {len(docs)}건 구조화 시작{tag} "
          f"(동시 {s.conc}, 묶음 {len(packs)}개)", flush=True)
    # 연속 파이프라인: 판정 요청(묶음)을 항상 conc건 서버에 걸어둔다 — 하나
    # 끝나면 즉시 다음 묶음 투입, 병합(직렬·메인 스레드)은 그 사이에 처리.
    # 청크 락스텝(최장 응답이 전체를 잡고, 병합 동안 요청 0건)을 피하는 구조.
    mc = {**s.dedup, "embed_model": s.embed_model}  # run별 클러스터(dedup)·임베딩 설정
    sc = schema if isinstance(schema, dict) else DEFAULT_SCHEMA
    chain, spos = chain_view(sc)   # 계층 체인 + 검증귀속 위치 (v1 스냅샷 폴백 포함)
    # 병합 LLM의 종류 라벨도 스키마 표시명을 따르게 (미지정 시 merge의 기본 LAYER_KIND)
    mc["layer_kind"] = {2 + i: (c.get("label") or c["key"]) for i, c in enumerate(chain)}
    ex = ThreadPoolExecutor(max_workers=s.conc)
    it = iter(packs)
    pending = set()
    for p in packs[:s.conc]:
        next(it)
        pending.add(ex.submit(judge_pack, domain, hint, p, s.model, s.body_chars,
                              s.no_think, doc_prompt, pack_prompt))
    while pending:
        finished, pending = wait(pending, return_when=FIRST_COMPLETED)
        for fut in finished:
            # 중지 요청 시 새 묶음 투입 중단 — 진행 중인 것만 마치고 배치 종료(묶음 단위 취소)
            np_ = None if (should_stop and should_stop()) else next(it, None)
            if np_ is not None:
                pending.add(ex.submit(judge_pack, domain, hint, np_, s.model,
                                      s.body_chars, s.no_think, doc_prompt, pack_prompt))
            for (src_id, title, kind, body), j in fut.result():
                ref = f"{source_name}:{src_id}"[:400]  # 문서 증거 (kind='doc')
                # 이 문서에서 뽑힌 속성들 — run별 결과 기록용 (라우터 run은 라인 귀속 포함)
                attrs_out = {"_line": line_name} if line_name else {}
                if not j or j.get("_error"):
                    status, note = "error", (j.get("_error") if j
                                             else "LLM 응답 파싱 실패")
                elif j.get("fits") and all(j.get(c["key"]) for c in chain):
                    try:
                        # 계층 체인 사다리 — 도메인(1) 밑으로 체인 칸을 순서대로 (layer 2+i).
                        # 태그: 첫 칸=entry(검색진입·속성 부착), spos 칸=solution(검증귀속)
                        d = get_or_create(cur, 1, domain, None, "doc", ref,
                                          use_embedding=False, run_id=run_id, count=count, mc=mc)
                        parent, val2node, entry_node = d, {}, None
                        for i, c in enumerate(chain):
                            cv_ = str(j[c["key"]]).strip()[:400]
                            rt = "entry" if i == 0 else ("solution" if i == spos else None)
                            parent = get_or_create(cur, 2 + i, cv_, parent, "doc", ref,
                                                   run_id=run_id, count=count, mc=mc,
                                                   role_tag=rt)
                            val2node[(c["key"], cv_)] = parent
                            if i == 0:
                                entry_node = parent
                        # 속성(attr, 9층) — 세션 파이프라인과 공용 apply_extras.
                        # 같은 값(회사·시점 등)은 전역 1노드라 문서들이 이 노드로 이어진다.
                        ej = {a["key"]: j.get(a["key"]) for a in sc["attrs"]}   # 스키마 top-level 키
                        legacy = j.get("entities")   # 구 run 스냅샷(중첩 entities) 하위호환
                        if not any(v for v in ej.values()) and isinstance(legacy, dict):
                            ej = legacy
                        ats, _ = apply_extras(cur, sc, ej, None, entry_node, val2node,
                                              "doc", ref, run_id=run_id, count=count)
                        attrs_out.update(ats)
                        status, note = "done", str(j.get("reason") or "")[:1000]
                    except oracledb.DatabaseError as e:
                        # 병합 중 DB 오류 — 이 문서만 버리고 배치는 계속한다
                        # (실측 2026-08-21: ORA-00001 한 건이 2192건 배치를 죽였다).
                        # 부분 기록은 롤백해 반쪽 그래프를 남기지 않는다 —
                        # 재시도는 [실패 재시도]가 미처리로 되돌려 다시 판정한다.
                        con.rollback()
                        status = "error"
                        note = f"병합 실패: {type(e).__name__}: {str(e)[:200]}"
                elif j.get("fits"):   # fits인데 체인 값 일부 누락 — 우회 연결 금지
                    missing = ", ".join(c["key"] for c in chain if not j.get(c["key"]))
                    status, note = "excluded", f"체인 값 누락: {missing}"[:1000]
                else:
                    status, note = "excluded", str(j.get("reason") or "기준 미달")[:1000]
                if count:  # 활성 run만 corpus_docs 캐시 갱신 (비활성은 결과만 기록)
                    cur.execute("""UPDATE corpus_docs SET graph_status = :1, graph_note = :2
                               WHERE source_name = :3 AND src_id = :4""",
                            [status, (note or "")[:1000] or None, source_name, src_id])
                if run_id != "-":  # run별 결과 기록 (B-full 버저닝)
                    ents_json = (json.dumps(attrs_out, ensure_ascii=False)[:4000]
                                 if attrs_out else "")
                    runs.record_result(cur, run_id, source_name, src_id, status, note,
                                       entities=ents_json)
                con.commit()
                stats[status] += 1
                pt, ct = (j or {}).get("_usage", (0, 0))   # 입력·출력 토큰
                stats["in_tok"] = stats.get("in_tok", 0) + pt
                stats["out_tok"] = stats.get("out_tok", 0) + ct
                mark = {"done": "+", "excluded": "-", "error": "!"}[status]
                tok = f" [in {pt} / out {ct} tok]" if pt or ct else ""
                print(f"  {mark} {src_id}: {status}{tok}"
                      f"{' — ' + note if status != 'done' and note else ''}", flush=True)
    ex.shutdown()
    return len(docs)


def _lob_str(v) -> str:
    """CLOB/문자열 공용 문자열화 — 스냅샷 값이 LOB으로 올 수 있다."""
    return (v.read() if hasattr(v, "read") else (v or "")) or ""


def _run_overrides(cur, run_id: str, s):
    """run 조합 스냅샷을 설정에 덮어씀 — (domain, hint, count) 반환.
    domain은 run에 스냅샷된 값(프리셋 오버라이드 포함) — 소스 등록 도메인보다 우선."""
    import json as _json
    cur.execute("""SELECT domain, domain_version, chat_model, settings, active, embed_model
                   FROM doc_runs WHERE run_id = :1""", [run_id])
    r = cur.fetchone()
    if not r:
        raise ValueError(f"run이 없습니다: {run_id}")
    domain, dver, model, st_json, active, embed_model = r
    if hasattr(st_json, "read"):     # settings가 CLOB이면 문자열로
        st_json = st_json.read()
    st = _json.loads(st_json or "{}")
    s.model = (model or "").strip()
    s.body_chars = st.get("body_chars", s.body_chars)
    s.pack_tokens = st.get("pack_tokens", s.pack_tokens)
    s.no_think = bool(st.get("no_think", s.no_think))
    s.doc_prompt = st.get("doc_prompt", s.doc_prompt)    # 엔티티 추출 프롬프트 스냅샷 적용
    s.pack_prompt = st.get("pack_prompt", s.pack_prompt)
    if isinstance(st.get("schema"), dict):               # 추출 스키마 스냅샷 (역할·키·설명)
        s.schema = st["schema"]
    if isinstance(st.get("lines"), list) and st["lines"]:  # 라우터 run — 라인별 스냅샷
        s.lines = st["lines"]
    if isinstance(st.get("dedup"), dict):                # 클러스터(dedup) 스냅샷 적용
        s.dedup = {**s.dedup, **st["dedup"]}
    s.embed_model = (embed_model or "").strip() or s.embed_model   # run별 임베딩
    # 도메인 지침: run 스냅샷의 값이 1순위 (2026-08-21) — 버전 행을 다시 읽으면 그 행이
    # 지워지거나 비어 있을 때 판정 기준이 조용히 사라진다. 옛 run은 값이 없어 행으로 폴백.
    hint = (st.get("hint") or "").strip()
    if not hint:
        cur.execute("""SELECT extract_hint FROM domain_versions
                       WHERE name = :1 AND version = :2""", [domain, dver])
        h = cur.fetchone()
        hint = _lob_str(h[0]) if h and h[0] else ""
    return domain, hint, active == "Y"


def run_for_source(source_name: str, limit: int = 0, drain: bool = False,
                   run_id: str = "", should_stop=None) -> dict:
    """소스 1개를 즉시 구조화 (관리 UI '지금 구조화'). 자체 커넥션으로 동작 —
    HTTP 요청은 이걸 백그라운드 스레드로 돌리고, 진행은 처리 현황이 폴링한다.

    drain=True면 미처리가 0이 될 때까지 limit건씩 반복 (즉시 버튼은 '끝까지'가 기대 —
    야간 배치 main()만 회당 limit 상한 유지). 커밋은 _structure_one 안에서 건별로 돼
    중간에 죽어도 진행분은 남는다.

    run_id 지정 시: 그 run의 조합(도메인 버전 지침·모델·설정)으로 그 run에 미판정인
    문서를 처리한다. 비활성 run이면 가중치를 올리지 않고(count=False) 증거·결과만
    축적 — 반영은 활성 전환(activate_run) 시 델타로."""
    con = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD,
                           dsn=config.ORACLE_DSN)
    cur = con.cursor()
    ddl(cur)
    doc_ddl(cur)
    con.commit()
    s = _load_settings(limit)
    stats = {"done": 0, "excluded": 0, "error": 0}
    total = 0
    if run_id:
        # run 지정 실행 — 도메인은 run 스냅샷(프리셋 오버라이드 포함)이 우선이라
        # 소스 등록 도메인이 없어도 된다. 활성·존재만 확인.
        cur.execute("""SELECT 1 FROM source_registry
                       WHERE source_name = :1 AND enabled = 'Y'""", [source_name])
        if not cur.fetchone():
            con.close()
            return {"error": "활성 소스가 아닙니다"}
        rid = run_id
        domain, hint, count = _run_overrides(cur, rid, s)
        by_run = True
        if not domain:
            con.close()
            return {"error": "run에 도메인이 없습니다 — 소스나 프리셋에 도메인을 지정하세요"}
        if not hint:   # run의 도메인 버전에 지침이 없으면 registry(현재 기본) 폴백
            cur.execute("SELECT extract_hint FROM domain_registry WHERE name = :1", [domain])
            h = cur.fetchone()
            hint = (h[0] or "") if h else ""
    else:
        # 대화 전용(scope=chat) 도메인은 제외 — main()과 동일 기준
        cur.execute("""SELECT s.source_name, s.domain, NVL(d.extract_hint, ' ')
                       FROM source_registry s
                       JOIN domain_registry d ON d.name = s.domain
                       WHERE s.source_name = :1 AND s.enabled = 'Y' AND s.domain IS NOT NULL
                         AND NVL(d.scope, 'both') != 'chat'""", [source_name])
        row = cur.fetchone()
        if not row:
            con.close()
            return {"error": "도메인이 지정된 활성 소스가 아닙니다 (검색 전용은 구조화 대상 아님)"}
        domain, hint, count, by_run = row[1], row[2], True, False
        rid = runs.current_run(cur, source_name)
    con.commit()
    stopped = False
    while True:
        if should_stop and should_stop():  # 예약/수동 중지 — 배치 경계에서 협조적 취소
            stopped = True
            break
        n = _structure_one(cur, con, source_name, domain, hint, s.limit, s, stats, rid,
                           count=count, by_run=by_run, should_stop=should_stop)
        total += n
        if not drain or n == 0:  # drain: 미처리가 바닥날 때까지 반복
            break
    runs.finish_run(cur, rid)
    con.commit()
    con.close()
    return {"processed": total, "stopped": stopped, **stats}
