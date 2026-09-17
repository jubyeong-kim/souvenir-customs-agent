"""라우팅 파이프라인 — 판정 → 근거 조립 → 답변 → 검증.

    classify ──→ assemble ──→ answer ──→ verify ──→ END
        │            │           ↑          │
        └─ 범위밖 ───┴─ 근거 없음 │          └─ 위반 → answer (1회만)
                     └─→ handoff ┘

분류와 "넘기기" 판단은 일부러 떼어 놓았다. classify 는 카테고리만 고르고,
넘길지 말지는 assemble 이 근거를 실제로 못 찾았을 때 결정한다.
모델의 확신이 아니라 **근거의 존재**가 넘기기의 기준이다.
"""
import os, re, pathlib
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from pydantic import BaseModel, Field

import context, prompts

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

MODEL = os.environ.get("MODEL", "gpt-4.1-mini")
_client = None


def client():
    """지연 생성. 모듈 수준에서 만들면 키 없는 사람은 import 조차 못 한다."""
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI()
    return _client


class State(TypedDict, total=False):
    query: str
    category: str
    evidence: list
    tools_called: list
    answer: str
    violations: list
    retried: bool


class Category(BaseModel):
    카테고리: str = Field(description="면세 | 검역 | 멸종위기종 | 면세점 | 범위밖")


# ───────────────────────── ① 판정 ─────────────────────────
def classify(state: State) -> State:
    cats = "\n".join(f"- {k}: {v}" for k, v in prompts.CATEGORIES.items())
    r = client().beta.chat.completions.parse(
        model=MODEL,
        messages=[{"role": "system", "content": prompts.CLASSIFY.format(categories=cats)},
                  {"role": "user", "content": state["query"]}],
        response_format=Category,
    )
    cat = r.choices[0].message.parsed.카테고리.strip()
    return {"category": cat if cat in prompts.CATEGORIES else "범위밖"}


# ──────────────────── ② 근거 조립 (도구) ────────────────────
def assemble(state: State) -> State:
    evidence, tools = context.assemble(state["category"], state["query"])
    return {"evidence": evidence, "tools_called": tools}


# ───────────────────────── ③ 답변 ─────────────────────────
def answer(state: State) -> State:
    ev = state["evidence"]
    asof = max((s["기준일"] for s in ev), default="기준일 미상")
    body = "\n\n".join(f"[{s['제목']}]\n{s['본문']}" for s in ev)
    system = prompts.ANSWER.format(evidence=body, asof=asof)
    system += prompts.CATEGORY_NOTE.get(state["category"], "")

    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": state["query"]}]
    if state.get("violations"):                     # 재작성: 무엇이 틀렸는지 알려 준다
        msgs.append({"role": "assistant", "content": state["answer"]})
        msgs.append({"role": "user", "content":
                     "위 답변에 [근거]에 없는 값이 있습니다: "
                     + ", ".join(state["violations"])
                     + ". 그 값을 빼거나 [근거]에 있는 값으로 바꿔 다시 쓰세요."})
    r = client().chat.completions.create(model=MODEL, messages=msgs, temperature=0)
    return {"answer": r.choices[0].message.content.strip()}


# ───────────────────────── ④ 검증 ─────────────────────────
NUM = re.compile(r"\d[\d,]*")


def verify(state: State) -> State:
    """답변에 나온 숫자가 근거에 있는지 역추적한다. 모델은 산수와 인용을 자주 틀린다.

    근거 전체를 한 덩어리로 놓고 대조한다. 쉼표는 표기 규칙 때문에 붙으므로 떼고 본다.
    """
    haystack = " ".join(s["본문"] for s in state["evidence"])
    haystack_nums = {n.replace(",", "") for n in NUM.findall(haystack)}
    bad = []
    for n in NUM.findall(state["answer"]):
        plain = n.replace(",", "")
        if plain in haystack_nums or len(plain) <= 1:
            continue
        bad.append(n)
    return {"violations": sorted(set(bad))}


def handoff(state: State) -> State:
    return {"answer": prompts.HANDOFF.format(query=state["query"]),
            "evidence": [], "tools_called": [], "violations": []}


# ───────────────────────── 흐름 ─────────────────────────
def route_after_classify(state: State) -> str:
    return "handoff" if state["category"] == "범위밖" else "assemble"


def route_after_assemble(state: State) -> str:
    # 넘기기의 기준은 모델의 확신이 아니라 근거의 존재다.
    return "answer" if state["evidence"] else "handoff"


def route_after_verify(state: State) -> str:
    if state["violations"] and not state.get("retried"):
        return "retry"
    return END


def mark_retry(state: State) -> State:
    return {"retried": True}


def build():
    g = StateGraph(State)
    for name, fn in [("classify", classify), ("assemble", assemble), ("answer", answer),
                     ("verify", verify), ("handoff", handoff), ("mark_retry", mark_retry)]:
        g.add_node(name, fn)
    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", route_after_classify,
                            {"assemble": "assemble", "handoff": "handoff"})
    g.add_conditional_edges("assemble", route_after_assemble,
                            {"answer": "answer", "handoff": "handoff"})
    g.add_edge("answer", "verify")
    g.add_conditional_edges("verify", route_after_verify, {"retry": "mark_retry", END: END})
    g.add_edge("mark_retry", "answer")
    g.add_edge("handoff", END)
    return g.compile()


GRAPH = build()


def run(query: str) -> State:
    return GRAPH.invoke({"query": query, "evidence": [], "tools_called": [],
                         "violations": [], "retried": False})


def demo():
    """API 키 없이 도는 자체 점검. 그래프 배선과 검증 규칙만 본다."""
    assert set(prompts.CATEGORIES) - {"범위밖"} == set(context.TOOLS), "카테고리와 도구가 어긋난다"

    ev = [{"제목": "t", "본문": "미화 800달러 이하, 주류 2L", "기준일": "2026-09-17"}]
    assert verify({"evidence": ev, "answer": "800달러까지 됩니다"})["violations"] == []
    assert verify({"evidence": ev, "answer": "600달러까지 됩니다"})["violations"] == ["600"]
    assert verify({"evidence": ev, "answer": "800,000원"})["violations"] == ["800,000"]

    assert route_after_classify({"category": "범위밖"}) == "handoff"
    assert route_after_assemble({"evidence": []}) == "handoff"
    assert route_after_verify({"violations": ["600"], "retried": True}) == END
    assert handoff({"query": "수하물 몇 kg"})["tools_called"] == []
    print("agent.py OK — 그래프 배선과 검증 규칙 통과 (API 호출 없음)")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        s = run(" ".join(sys.argv[1:]))
        print(f"[{s['category']}] 도구={s['tools_called']} 위반={s['violations']}\n\n{s['answer']}")
    else:
        demo()
