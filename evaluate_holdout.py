"""검증용 평가셋(`data/holdout.json`)을 잰다.

    python evaluate_holdout.py
    python evaluate_holdout.py --repeat 3      # 분산까지

**골든셋과 채점 방식이 다르다.** 왜 다른지가 이 파일의 요점이다.

| | `evaluate.py` (골든셋 12건) | 여기 (검증셋 29건) |
|---|---|---|
| 경로 | 답변만 | **답변 · 되묻기 · 넘김** 세 갈래를 채점한다 |
| 도구 | 집합 **완전일치** | **필수 도구가 포함**되었는지 (부분집합). 교차 조회로 도구가 더 붙는 것은 틀린 게 아니다 |
| 턴 | 1턴 | `turns` 가 있으면 여러 턴을 돌리고 **마지막 턴**을 채점한다 |
| 쓰는 때 | 고치는 동안 계속 본다 | **다 끝난 뒤 한 번** 본다 |

두 갈래가 다 맞는 문항(「술 좀 사왔어요」 는 조건부 답변도, 용량을 되묻는 것도 맞다)은
`"경로": ["답변", "되묻기"]` 로 둘 다 허용한다. 하나만 정답으로 박으면 채점기가 상담을 망친다.

### 이 검증셋의 한계 — 반드시 읽을 것

29건 중 상당수는 **이미 보면서 고쳤다** (#8~#17 이 그 질문들로 고친 회차다).
그래서 이것은 엄밀한 holdout 이 아니라 **«개선용 2호»** 에 가깝다.
진짜 검증은 **다른 사람이 만든 질문**으로 해야 한다 — 내가 만들면 내가 아는 범위에서만 나온다.
`data/holdout_외부.json` 자리를 비워 두었다. 강사님·동료에게 받은 10건을 거기 넣고,
그때는 **한 번만** 재는 것이 규칙이다.
"""
import argparse, json, pathlib, sys

import agent
from evaluate import grade

ROOT = pathlib.Path(__file__).parent
DEFAULT = ROOT / "data" / "holdout.json"


def route_of(state: dict) -> str:
    """이 문의가 어느 길로 나갔는가."""
    if state.get("missing"):
        return "되묻기"
    return "답변" if state.get("tools_called") else "넘김"


def score(case: dict, state: dict) -> dict:
    allowed = case["경로"] if isinstance(case["경로"], list) else [case["경로"]]
    took = route_of(state)
    route_ok = took in allowed

    need = set(case.get("필수_도구") or [])
    tools_ok = need <= set(state["tools_called"])

    if took == "답변" and (case["must_include"] or case["must_not"]):
        g = grade(case, state["answer"])
        answer_ok = all(g.담았는가) and not any(g.어겼는가)
        detail = {"빠뜨림": [x for x, ok in zip(case["must_include"], g.담았는가) if not ok],
                  "어김": [x for x, bad in zip(case["must_not"], g.어겼는가) if bad]}
    else:
        # 되묻기·넘김은 «그 길로 갔는가》 가 곧 답변 채점이다
        answer_ok = route_ok
        detail = {"이유": f"{took} 문항"}

    return {"id": case["id"], "묶음": case["묶음"], "질문": case.get("query") or case["turns"][-1],
            "경로": route_ok, "도구": tools_ok, "답변": answer_ok,
            "간": took, "기대경로": "/".join(allowed),
            "호출": state["tools_called"], "필수": sorted(need),
            "채점": detail, "답변문": state["answer"]}


def run_one(case: dict) -> dict:
    if case.get("turns"):
        return score(case, agent.converse(case["turns"])[-1])
    return score(case, agent.run(case["query"]))


def run_set(cases: list[dict]) -> list[dict]:
    rows = []
    for c in cases:
        r = run_one(c)
        rows.append(r)
        mark = "".join("O" if r[k] else "X" for k in ("경로", "도구", "답변"))
        print(f"  {mark}  {r['id']:<4} [{r['묶음']}] {r['질문'][:34]}")
    return rows


def report(rows: list[dict]) -> None:
    n = len(rows)
    for key, label in [("경로", "경로 적절성  "), ("도구", "도구 호출 적절성"), ("답변", "답변 적절성  ")]:
        k = sum(r[key] for r in rows)
        print(f"\n  {label} {k}/{n} = {k/n:.1%}")

    print("\n  묶음별")
    for 묶음 in dict.fromkeys(r["묶음"] for r in rows):
        g = [r for r in rows if r["묶음"] == 묶음]
        ok = sum(all((r["경로"], r["도구"], r["답변"])) for r in g)
        print(f"    {묶음:<5} {ok}/{len(g)}")

    bad = [r for r in rows if not all((r["경로"], r["도구"], r["답변"]))]
    if not bad:
        print("\n  실패 없음.")
        return
    print(f"\n  실패 {len(bad)}건")
    for r in bad:
        why = []
        if not r["경로"]:
            why.append(f"경로 {r['간']} != 기대 {r['기대경로']}")
        if not r["도구"]:
            why.append(f"도구 {r['호출']} 에 필수 {r['필수']} 없음")
        for x in r["채점"].get("어김", []):
            why.append(f"금지 어김: {x}")
        for x in r["채점"].get("빠뜨림", []):
            why.append(f"빠뜨림: {x}")
        print(f"    {r['id']:<4} [{r['묶음']}] {r['질문'][:32]}\n         {' / '.join(why)}")


def report_repeat(runs: list[list[dict]]) -> None:
    n = len(runs)
    by_id: dict[str, list[dict]] = {}
    for run in runs:
        for r in run:
            by_id.setdefault(r["id"], []).append(r)
    shaky = []
    print(f"\n  문항별 통과 횟수 ({n}회 중)")
    for cid, rs in by_id.items():
        c = sum(all((r["경로"], r["도구"], r["답변"])) for r in rs)
        if 0 < c < n:
            shaky.append(cid)
        print(f"    {'  ' if c == n else ('XX' if c == 0 else '~~')} {cid:<4} {c}/{n}")
    total = len(by_id)
    print(f"\n  **안정성  {total - len(shaky)}/{total} 문항이 {n}회 모두 같은 결과**")
    if shaky:
        print(f"  흔들린 문항: {' '.join(shaky)}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--file", default=str(DEFAULT))
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--save")
    args = p.parse_args()

    path = pathlib.Path(args.file)
    if not path.exists():
        print(f"검증셋이 없다: {path}")
        return 1
    cases = json.loads(path.read_text(encoding="utf-8"))

    runs = []
    for i in range(max(1, args.repeat)):
        if args.repeat > 1:
            print(f"\n  [{i + 1}/{args.repeat}]")
        runs.append(run_set(cases))

    report(runs[-1])
    if args.repeat > 1:
        report_repeat(runs)
    if args.save:
        pathlib.Path(args.save).write_text(
            json.dumps(runs if args.repeat > 1 else runs[0], ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n  저장: {args.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
