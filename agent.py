"""라우팅 파이프라인 — 판정 → 근거 조립 → 답변 → 검증.

    classify ──→ gate ──→ assemble ──→ answer ──→ verify ──→ END
        │           │          │           ↑          │
        │           └─→ ask    └─ 근거 0 ──┤          └─ 위반 → answer (1회만)
        └─ 범위밖 ──────────────→ handoff ─┘

**나가는 길이 셋이다. 셋을 가르는 판단을 서로 다른 곳에서 한다.**

    답변    근거로 답할 수 있다
    되묻기  규정을 고를 수 없다   ← gate  (품목이 특정되지 않아 적용 규정이 갈린다)
    넘김    근거가 없다           ← classify(범위밖) 또는 assemble(검색 점수 0)

classify 는 카테고리만 고른다. 넘길지는 근거를 실제로 못 찾았을 때 정하고,
되물을지는 규정을 고를 수 있는지로 정한다. **모델의 확신이 기준인 곳은 한 곳도 없다.**
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
# 되묻기 판단만 큰 모델을 쓴다. mini 는 "품목이 특정되면 조건부로 답한다" 를 못 지켜
# 망고·고기까지 되물었다(12건 100% → 91.7%). 프롬프트를 네 번 고쳐도 안 됐다.
# 한 문의에 한 번 부르는 호출이라 비용 차이가 작다.
GATE_MODEL = os.environ.get("GATE_MODEL", "gpt-4.1")
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
    missing: list
    evidence: list
    tools_called: list
    answer: str
    violations: list
    retried: bool


class Category(BaseModel):
    카테고리: str = Field(description="면세 | 검역 | 멸종위기종 | 면세점 | 범위밖")


class Gate(BaseModel):
    되물어야_한다: bool = Field(description="판정을 뒤집는 사실이 빠졌는가")
    빠진_정보: list[str] = Field(description="물어볼 것 1~3개. 짧은 명사구로")
    이유: str


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


# ───────────────── ①-b 되묻기 판단 (분류와 분리) ─────────────────
def gate(state: State) -> State:
    """답할 수 있는가, 한 가지를 더 물어야 하는가.

    분류(무슨 영역인가)와도, 넘기기(근거가 있는가)와도 다른 판단이다.
    여기서 넓게 잡으면 답할 수 있는 것까지 되물어 자동화율을 갉아먹는다 —
    모두몰 때 이관 게이트를 넓게 잡아 맞게 답하던 5건을 잃었다. (PLAYBOOK 4-⑤)
    """
    r = client().beta.chat.completions.parse(
        model=GATE_MODEL,
        messages=[{"role": "system",
                   "content": prompts.GATE.format(category=state["category"],
                                                  query=state["query"])}],
        response_format=Gate,
        temperature=0,
    )
    g = r.choices[0].message.parsed
    return {"missing": g.빠진_정보[:3] if g.되물어야_한다 else []}


def ask(state: State) -> State:
    """되묻는다. **도구를 부르지 않는다** — 식별자 없이 조회해서 답하면 단정이 된다.

    문구를 모델에게 다시 쓰게 하지 않는다. 틀릴 여지만 늘고 비용도 는다.
    """
    lead = "말씀만으로는 판단이 어렵습니다."
    return {"answer": prompts.ASK.format(
                lead=lead,
                missing="\n".join(f"- {m}" for m in state["missing"])),
            "evidence": [], "tools_called": [], "violations": []}


# ──────────────────── ② 근거 조립 (도구) ────────────────────
def assemble(state: State) -> State:
    evidence, tools = context.assemble(state["category"], state["query"])
    return {"evidence": evidence, "tools_called": tools}


# ───────────────────────── ③ 답변 ─────────────────────────
def answer(state: State) -> State:
    ev = state["evidence"]
    asof = max((s["기준일"] for s in ev), default="기준일 미상")
    body = "\n\n".join(f"[{s['제목']}]\n{s['본문']}" for s in ev)
    al = context.aliases(state["query"])
    system = prompts.ANSWER.format(
        evidence=body, asof=asof,
        aliases="\n".join(f"- 질문의 '{k}' = 근거의 '{v}'" for k, v in al) or "- (없음)")
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
    # 기준일은 우리가 붙이라고 지시한 것이므로 근거에 있는 값으로 센다.
    # 처음엔 빼먹어서 "(2026-09-17 기준)" 의 09·17 을 위반으로 잡고 재작성까지 했다.
    haystack = " ".join([s["본문"] for s in state["evidence"]]
                        + [s["기준일"] for s in state["evidence"]])
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
    return "handoff" if state["category"] == "범위밖" else "gate"


def route_after_gate(state: State) -> str:
    return "ask" if state["missing"] else "assemble"


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
    for name, fn in [("classify", classify), ("gate", gate), ("ask", ask),
                     ("assemble", assemble), ("answer", answer),
                     ("verify", verify), ("handoff", handoff), ("mark_retry", mark_retry)]:
        g.add_node(name, fn)
    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", route_after_classify,
                            {"gate": "gate", "handoff": "handoff"})
    g.add_conditional_edges("gate", route_after_gate,
                            {"ask": "ask", "assemble": "assemble"})
    g.add_conditional_edges("assemble", route_after_assemble,
                            {"answer": "answer", "handoff": "handoff"})
    g.add_edge("answer", "verify")
    g.add_conditional_edges("verify", route_after_verify, {"retry": "mark_retry", END: END})
    g.add_edge("mark_retry", "answer")
    g.add_edge("handoff", END)
    g.add_edge("ask", END)
    return g.compile()


GRAPH = build()


def run(query: str) -> State:
    return GRAPH.invoke({"query": query, "evidence": [], "tools_called": [],
                         "violations": [], "retried": False, "missing": []})


def demo():
    """API 키 없이 도는 자체 점검. 그래프 배선과 검증 규칙만 본다."""
    assert set(prompts.CATEGORIES) - {"범위밖"} == set(context.TOOLS), "카테고리와 도구가 어긋난다"

    ev = [{"제목": "t", "본문": "미화 800달러 이하, 주류 2L", "기준일": "2026-09-17"}]
    assert verify({"evidence": ev, "answer": "800달러까지 됩니다"})["violations"] == []
    assert verify({"evidence": ev, "answer": "600달러까지 됩니다"})["violations"] == ["600"]
    assert verify({"evidence": ev, "answer": "800,000원"})["violations"] == ["800,000"]

    assert route_after_classify({"category": "범위밖"}) == "handoff"
    assert route_after_classify({"category": "면세"}) == "gate"
    assert route_after_gate({"missing": ["용량"]}) == "ask"
    assert route_after_gate({"missing": []}) == "assemble"
    # 되물을 때는 도구를 하나도 부르지 않는다
    assert ask({"query": "술 사왔어요", "missing": ["용량", "금액"]})["tools_called"] == []
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
