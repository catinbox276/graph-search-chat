"""StackExchange Posts.xml + 지식인 parquet -> 블로그 코퍼스 JSONL.

채택답변 있는 질문만 골라 질문+답변을 한 문서로 합치고,
사이트별 점수 상위 TOP_N건을 뽑는다.

usage: python3 scripts/build_corpus.py
output: data/corpus/blog_corpus.jsonl
  {id, title, body, tags, score, source, url}
"""
import html
import json
import re
from pathlib import Path

DATA = Path(__file__).parent.parent / "data"
OUT = DATA / "corpus"
TOP_N = 5000  # ponytail: 사이트당 5천 건이면 PoC 검색 코퍼스로 충분

TAG_RE = re.compile(r"<[^>]+>")


def attrs(line: str) -> dict:
    return dict(re.findall(r'(\w+)="([^"]*)"', line))


def strip_html(s: str) -> str:
    return TAG_RE.sub(" ", html.unescape(s)).strip()


def parse_site(name: str, posts_xml: Path):
    """단일 패스: Id 순서상 답변은 항상 질문 뒤에 나온다."""
    pending = {}  # accepted_answer_id -> question record
    docs = []
    with open(posts_xml, encoding="utf-8") as f:
        for line in f:
            if "<row" not in line:
                continue
            a = attrs(line)
            if a.get("PostTypeId") == "1" and "AcceptedAnswerId" in a:
                pending[a["AcceptedAnswerId"]] = a
            elif a.get("PostTypeId") == "2" and a.get("Id") in pending:
                q = pending.pop(a["Id"])
                docs.append({
                    "id": f"{name}-{q['Id']}",
                    "title": html.unescape(q.get("Title", "")),
                    "body": strip_html(q.get("Body", ""))
                    + "\n\n[해결 답변]\n"
                    + strip_html(a.get("Body", "")),
                    "tags": re.findall(r"[\w#+.-]+", html.unescape(q.get("Tags", ""))),
                    "score": int(q.get("Score", 0)),
                    "source": name,
                    "url": f"https://{name}.com/q/{q['Id']}",
                })
    docs.sort(key=lambda d: d["score"], reverse=True)
    return docs[:TOP_N]


def parse_kin(parquet: Path):
    import pyarrow.parquet as pq
    t = pq.read_table(parquet).to_pylist()
    return [{
        "id": f"kin-{i}",
        "title": r["Instruction"][:100],
        "body": r["Instruction"] + "\n\n[해결 답변]\n" + r["Response"],
        "tags": [],
        "score": 0,
        "source": "naver-kin",
        "url": "",
    } for i, r in enumerate(t)]


def main():
    OUT.mkdir(exist_ok=True)
    all_docs = []
    for site in ("askubuntu", "superuser"):
        docs = parse_site(site, DATA / site / "Posts.xml")
        print(f"{site}: {len(docs)}건 (점수 {docs[-1]['score']}~{docs[0]['score']})")
        all_docs += docs
    kin = parse_kin(DATA / "korean_instruction.parquet")
    print(f"naver-kin: {len(kin)}건")
    all_docs += kin

    out_file = OUT / "blog_corpus.jsonl"
    with open(out_file, "w", encoding="utf-8") as f:
        for d in all_docs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"총 {len(all_docs)}건 -> {out_file}")


if __name__ == "__main__":
    main()
