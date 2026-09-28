"""문의 51건을 한꺼번에 접수하고, 멈춤 기준 후보를 **같은 초안으로** 비교한다.

    python hitl/sweep.py           접수(처음 한 번만 API 호출) → 채점 → 비교표
    python hitl/sweep.py --table   저장된 초안으로 표만 다시 (API 없음)

접수는 실제 그래프(hitl.open_app)로 한다. 돌리고 나면 CHOSEN 기준에 걸린 건이
데모의 «승인 대기» 에 그대로 쌓여 있다.

세 값을 센다:
  개입률  사람에게 온 비율
  놓침    사람확인 건인데 판정 답장이 사람을 거치지 않고 나간 것 — 가장 비싼 실수
  헛멈춤  볼 필요 없는데 사람에게 온 것 — 많으면 담당자가 안 읽고 승인만 누른다
그리고 **틀린 답 자동 발송** — 채점기로 본 실제 사고. 놓침은 라벨 기준이고 이것은 결과 기준이다.
"""
import argparse
import json

import hitl
import evaluate                                     # 루트 채점기 (hitl 이 경로를 잡아 준다)

CASES = hitl.HERE / "cases.json"
DRAFTS = hitl.RUNS / "drafts.json"
TABLE = hitl.HERE / "criteria.md"                   # runs/ 는 안 올라가므로 표는 여기 남긴다


def route_of(v: dict) -> str:
    return "답변" if v.get("reply") else "되묻기" if v.get("missing") else "넘김"


def check(case: dict, v: dict) -> dict:
    """나간(나갈) 답이 맞는가. 경로가 틀리면 틀린 것, 답변이면 채점기로 사실을 본다."""
    want = case["경로"] if isinstance(case["경로"], list) else [case["경로"]]
    route = route_of(v)
    if route not in want:
        return {"맞음": False, "채점": f"경로 {route} (정답 {'/'.join(want)})"}
    if route != "답변" or not (case["must_include"] or case["must_not"]):
        return {"맞음": True, "채점": ""}
    g = evaluate.grade(case, v["answer"])
    miss = [x for x, ok in zip(case["must_include"], g.담았는가) if not ok]
    bad = [x for x, b in zip(case["must_not"], g.어겼는가) if b]
    return {"맞음": not miss and not bad,
            "채점": "; ".join([*("빠뜨림: " + x for x in miss), *("어김: " + x for x in bad)])}


def draft(app, case: dict) -> dict:
    t = hitl.info(app, case["id"])
    if not t["values"]:                              # 중간에 끊겼으면 이미 접수한 건은 다시 안 보낸다
        t = hitl.submit(app, case["id"], case["query"])
    v = t["values"]
    # 신호 자체가 아니라 **신호의 재료**를 남긴다. 기준을 새로 짜도 API 를 다시 부르지 않게.
    keep = {k: v.get(k) for k in ("query", "terms", "reply", "violations", "판정", "인용확인")}
    keep["evidence"] = [{"카테고리": e["카테고리"]} for e in v.get("evidence") or []]
    return {"id": case["id"], "경로": route_of(v), "판정": v.get("판정", ""),
            "멈춤": t["waiting"], "재료": keep, "인용": v.get("인용", ""),
            "초안": v["answer"], **check(case, v)}


def collect(cases: list[dict]) -> list[dict]:
    rows = json.loads(DRAFTS.read_text(encoding="utf-8")) if DRAFTS.exists() else []
    done = {r["id"] for r in rows}
    app = hitl.open_app()
    for i, c in enumerate(cases, 1):
        if c["id"] in done:
            continue
        rows.append(draft(app, c))
        r = rows[-1]
        print(f"[{i:2}/{len(cases)}] {c['id']:4} {r['경로']:3} {r['판정'] or '-':6} "
              f"{'멈춤' if r['멈춤'] else '발송'}  {'O' if r['맞음'] else 'X ' + r['채점'][:60]}",
              flush=True)
        DRAFTS.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return rows


def ids(xs: list[str]) -> str:
    return f"{len(xs)}건" + (f" ({', '.join(xs)})" if xs else "")


def table(cases: list[dict], rows: list[dict]) -> str:
    """라벨은 cases.json 에서 **표를 만들 때** 읽는다. 라벨을 고쳐도 API 를 다시 부를 필요가 없다."""
    need = {c["id"]: c["사람확인"] for c in cases}
    rows = [{**r, "신호": hitl.signals(r["재료"])} for r in rows if r["id"] in need]
    out = [f"## 기준별 비교 ({len(rows)}건, 사람확인 {sum(need[r['id']] for r in rows)}건)", "",
           "| 기준 | 개입률 | 놓침 | 헛멈춤 | 틀린 답 자동 발송 |", "|---|---:|---|---|---|"]
    for name, f in hitl.CRITERIA.items():
        stop = {r["id"]: f(r["신호"]) for r in rows}
        miss = [r["id"] for r in rows if need[r["id"]] and not stop[r["id"]] and r["경로"] == "답변"]
        false = [r["id"] for r in rows if stop[r["id"]] and not need[r["id"]]]
        wrong = [r["id"] for r in rows if not stop[r["id"]] and r["경로"] == "답변" and not r["맞음"]]
        mark = " ← 채택" if name == hitl.CHOSEN else ""
        out.append(f"| {name}{mark} | {sum(stop.values())}/{len(rows)} | {ids(miss)} | "
                   f"{ids(false)} | {ids(wrong)} |")

    out += ["", "## 건별", "",
            "| 건 | 사람확인 | 경로 | 판정 | 영역 | 품목 | 허용 | 숫자 | 인용 없음 | 채택 기준 | 채점 |",
            "|---|:-:|---|---|:-:|:-:|:-:|:-:|:-:|---|---|"]
    chosen = hitl.CRITERIA[hitl.CHOSEN]
    for r in rows:
        g = r["신호"]
        o = lambda k: "●" if g[k] and g["답변"] else ""
        out.append(f"| {r['id']} | {'●' if need[r['id']] else ''} | {r['경로']} | {r['판정']} | "
                   f"{o('영역')} | {o('품목')} | {o('허용')} | {o('숫자')} | {o('인용')} | "
                   f"{'멈춤' if chosen(g) else '자동'} | {'O' if r['맞음'] else 'X ' + r['채점']} |")
    return "\n".join(out) + "\n"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--table", action="store_true", help="API 없이 저장된 초안으로 표만")
    a = p.parse_args()
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    rows = (json.loads(DRAFTS.read_text(encoding="utf-8")) if a.table else collect(cases))
    md = table(cases, rows)
    TABLE.write_text("# 멈춤 기준 비교 — `python hitl/sweep.py --table` 이 만든다\n\n" + md,
                     encoding="utf-8")
    print("\n" + md.split("\n## 건별")[0])


if __name__ == "__main__":
    main()
