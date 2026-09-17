"""두 지표를 잰다 — 도구 호출 적절성, 답변 적절성.

    python evaluate.py --self-check   # 모범 답안으로 채점기 자체를 검증한다 (먼저)
    python evaluate.py                # 평가셋 전체
    python evaluate.py --repeat 3     # 3회 돌려 **분산**까지 본다
    python evaluate.py --n 3          # 앞 3건만

측정 규칙 (chatbot/PLAYBOOK.md 5번, 타협하지 않는다):
  같은 코드로 세 번 재면 65.6 / 68.8 / 75.0% 가 나왔다. ±1건이 아니라 ±3건이다.
  - 한 번에 한 군데만 고친다
  - 최소 두 번 잰다. 점수가 아니라 **실패한 케이스 목록**으로 비교한다
  - 목표 케이스가 2회 연속 통과해야 "수정됨"
  - 1건 차이는 변화 없음

그리고 **평균만 보면 안 된다.** 배포 후에 「비첸향」 이 돌릴 때마다 가부가 뒤집혔는데
(5회 중 3회 틀림) 평균 점수로는 보이지 않았다. `--repeat N` 이 문항별로 몇 회 통과했는지
세고 **흔들린 문항**(0회도 N회도 아닌 것)을 따로 보여 준다. 실험기록 #17·#18.
"""
import argparse, json, os, pathlib, sys

from pydantic import BaseModel, Field

import agent

ROOT = pathlib.Path(__file__).parent
GOLDEN = ROOT / "data" / "goldenset.json"


class Grade(BaseModel):
    """항목마다 참/거짓으로만 받는다.

    처음에는 "담은 사실 목록"을 문자열로 받았더니 채점기가 목록에 판단 근거 문장을
    넣어 버려서(어긴 것이 없을 때 "없음" 한 줄, 안 어긴 항목에 "~라고 답하지 않았다")
    개수 비교가 전부 어긋났다. 번호대로 참/거짓만 받으면 그 여지가 없다.
    """
    담았는가: list[bool] = Field(description="[반드시 담아야 할 사실] 항목 순서대로 true/false")
    어겼는가: list[bool] = Field(description="[말하면 안 되는 것] 항목 순서대로 true/false")
    이유: str


GRADER = """너는 상담 답변을 채점한다. **표현이 아니라 사실**을 본다.

각 목록의 항목 **번호 순서대로** true/false 만 답한다. 항목 수와 답 개수는 반드시 같다.

- 같은 사실을 다른 말로 썼으면 담은 것이다. "800달러"와 "미화 800불"은 같다.
- 조건이 둘인 사실(예: 2L 이하 그리고 400달러 이하)은 **둘 다** 있어야 담은 것이다.
- 금지 항목은 답변이 그 내용을 **사실로 말했을 때만** 어긴 것이다.
  "그 기준은 해당하지 않는다"고 부정한 것은 어긴 것이 아니다.

[반드시 담아야 할 사실]
{must_include}

[말하면 안 되는 것]
{must_not}

[채점할 답변]
{answer}"""


def grade(case: dict, answer: str) -> Grade:
    r = agent.client().beta.chat.completions.parse(
        model=os.environ.get("GRADER_MODEL", agent.MODEL),
        messages=[{"role": "user", "content": GRADER.format(
            must_include="\n".join(f"{i}. {x}" for i, x in enumerate(case["must_include"], 1)) or "(없음)",
            must_not="\n".join(f"{i}. {x}" for i, x in enumerate(case["must_not"], 1)) or "(없음)",
            answer=answer)}],
        response_format=Grade,
        temperature=0,
    )
    g = r.choices[0].message.parsed
    # 길이가 어긋나면 채점이 성립하지 않는다. 조용히 넘기면 점수가 거짓이 된다.
    def fit(flags: list[bool], n: int) -> list[bool]:
        return (flags + [False] * n)[:n]
    g.담았는가 = fit(g.담았는가, len(case["must_include"]))
    g.어겼는가 = fit(g.어겼는가, len(case["must_not"]))
    return g


def score_case(case: dict, state: dict) -> dict:
    """한 건을 채점한다. 두 지표는 서로 독립이다 — 도구를 틀려도 답변은 따로 본다."""
    tools_ok = sorted(state["tools_called"]) == sorted(case["expected_tools"])

    if case["must_include"] or case["must_not"]:
        g = grade(case, state["answer"])
        answer_ok = all(g.담았는가) and not any(g.어겼는가)
        detail = {
            "빠뜨림": [x for x, ok in zip(case["must_include"], g.담았는가) if not ok],
            "어김": [x for x, bad in zip(case["must_not"], g.어겼는가) if bad],
            "이유": g.이유,
        }
    else:                                   # 넘기기 문항: 넘겼으면 그것으로 맞다
        answer_ok = not state["tools_called"]
        detail = {"이유": "넘기기 문항"}

    return {"id": case["id"], "카테고리": case["category"], "질문": case["query"],
            "도구": tools_ok, "답변": answer_ok,
            "호출": state["tools_called"], "기대": case["expected_tools"],
            "위반": state.get("violations", []), "채점": detail,
            "답변문": state["answer"]}


def run_set(cases: list[dict]) -> list[dict]:
    rows = []
    for c in cases:
        state = agent.run(c["query"])
        row = score_case(c, state)
        rows.append(row)
        mark = ("O" if row["도구"] else "X") + ("O" if row["답변"] else "X")
        print(f"  {mark}  {c['id']:<4} [{row['카테고리']}] {c['query'][:38]}")
    return rows


def report_repeat(runs: list[list[dict]]) -> None:
    """N 회 결과를 문항별로 모아 **분산**을 보여 준다.

    평균이 같아도 «늘 맞는 12건》 과 «반쯤 맞는 12건》 은 전혀 다른 물건이다.
    후자는 사용자가 같은 질문을 두 번 했을 때 다른 답을 받는다.
    """
    n = len(runs)
    by_id: dict[str, list[dict]] = {}
    for run in runs:
        for r in run:
            by_id.setdefault(r["id"], []).append(r)

    print(f"{chr(10)}  ── {n}회 반복 ──")
    for i, run in enumerate(runs, 1):
        t = sum(r["도구"] for r in run)
        a = sum(r["답변"] for r in run)
        print(f"  {i}회차   도구 {t}/{len(run)} = {t/len(run):.1%}   "
              f"답변 {a}/{len(run)} = {a/len(run):.1%}")

    shaky = []
    print(f"{chr(10)}  문항별 통과 횟수 ({n}회 중)")
    for cid, rs in by_id.items():
        t = sum(r["도구"] for r in rs)
        a = sum(r["답변"] for r in rs)
        mark = "  " if (t == n and a == n) else ("XX" if (t == 0 or a == 0) else "~~")
        if 0 < a < n or 0 < t < n:
            shaky.append(cid)
        print(f"    {mark} {cid:<4} [{rs[0]['카테고리']}] 도구 {t}/{n}  답변 {a}/{n}")

    total = len(by_id)
    print(f"{chr(10)}  **안정성  {total - len(shaky)}/{total} 문항이 {n}회 모두 같은 결과**")
    if shaky:
        print(f"  흔들린 문항: {' '.join(shaky)}")
        print("  → 평균만 보면 안 보인다. 이 문항을 5회 더 돌려 어느 단계가 흔들리는지 가른다:")
        print("     분류 · 정규화 · 근거 절은 같은가? 답변만 흔들리는가?")
    else:
        print("  흔들린 문항 없음 — 같은 질문에 같은 답을 준다.")


def report(rows: list[dict]) -> None:
    n = len(rows)
    t = sum(r["도구"] for r in rows)
    a = sum(r["답변"] for r in rows)
    print(f"\n  도구 호출 적절성  {t}/{n} = {t/n:.1%}")
    print(f"  답변 적절성      {a}/{n} = {a/n:.1%}")

    bad = [r for r in rows if not (r["도구"] and r["답변"])]
    if not bad:
        print("\n  실패 없음.")
        return
    print(f"\n  실패 {len(bad)}건 — 점수가 아니라 이 목록으로 비교한다:")
    for r in bad:
        why = []
        if not r["도구"]:
            why.append(f"도구 {r['호출']} != 기대 {r['기대']}")
        if not r["답변"]:
            for x in r["채점"].get("어김", []):
                why.append(f"금지 어김: {x}")
            for x in r["채점"].get("빠뜨림", []):
                why.append(f"빠뜨림: {x}")
        if r["위반"]:
            why.append(f"근거없는 숫자 {r['위반']}")
        print(f"    {r['id']:<4} [{r['카테고리']}] {r['질문'][:34]}\n         {' / '.join(why)}")


def self_check() -> int:
    """채점기가 제대로 채점하는지 모범 답안으로 먼저 검증한다.

    여기서 1.0 이 안 나오면 이후 숫자는 전부 무의미하다.
    """
    case = {"must_include": ["기본 면세범위는 미화 800달러 이하",
                             "주류는 2L 이하이면서 400달러 이하"],
            "must_not": ["주류 면세는 1병까지", "기본 면세범위는 600달러"]}
    good = "여행자 기본 면세범위는 800달러 이하입니다. 주류는 총 2L 이하이면서 400달러 이하까지 면세됩니다."
    bad = "기본 면세범위는 600달러이고, 주류는 1병까지만 됩니다."
    partial = "여행자 기본 면세범위는 800달러 이하입니다."

    g1, g2, g3 = grade(case, good), grade(case, bad), grade(case, partial)
    checks = [
        (all(g1.담았는가) and not any(g1.어겼는가), "모범 답안이 만점이 아니다", g1),
        (all(g2.어겼는가), "틀린 답안의 금지 위반을 못 잡는다", g2),
        (g3.담았는가 == [True, False], "조건 하나만 담은 답을 만점 처리한다", g3),
    ]
    failed = 0
    for ok, msg, g in checks:
        print(("  OK  채점기 정상" if ok else f"  FAIL {msg}"))
        if not ok:
            failed += 1
            print(f"        담았는가={g.담았는가} 어겼는가={g.어겼는가} 이유={g.이유}")
    return failed


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-check", action="store_true", help="채점기 자체를 먼저 검증한다")
    p.add_argument("--n", type=int, help="앞 N건만")
    p.add_argument("--repeat", type=int, default=1, help="N회 반복해 분산까지 본다")
    p.add_argument("--save", help="결과를 JSON 으로 저장할 경로")
    args = p.parse_args()

    if args.self_check:
        return min(self_check(), 1)

    if not GOLDEN.exists():
        print(f"평가셋이 없다: {GOLDEN}")
        return 1
    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))
    if args.n:
        cases = cases[: args.n]
    runs = []
    for i in range(max(1, args.repeat)):
        if args.repeat > 1:
            print(f"\n  [{i + 1}/{args.repeat}]")
        runs.append(run_set(cases))

    if args.repeat > 1:
        report_repeat(runs)
    else:
        report(runs[0])

    if args.save:
        payload = runs if args.repeat > 1 else runs[0]
        pathlib.Path(args.save).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  저장: {args.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
