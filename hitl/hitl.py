"""사람이 승인하는 답장 발송 — 루트 파이프라인 뒤에 «멈춤 → 사람 → 이어가기» 를 붙인다.

    python hitl/hitl.py        API 없이 도는 자체 점검 (배선·기준·멱등·기한 규칙)

    classify → … → answer → verify → read → risk ─ 안 걸림 ──────→ send → END
                                        └─ 걸림 → review (멈춤) ─ 승인·수정 ─→ send
                                                     │         ├─ 반려 ─────→ reject → END
                                                     │         └─ 기한초과 ─→ send (이관 안내)
                                                     └─ 다시 판정 → redo → answer → … → risk → review
    되묻기·넘김(ask·handoff)은 판정이 없는 정해진 문구라 바로 send 로 간다.

**되돌릴 수 없는 것은 send 하나다.** 고객은 답장대로 물건을 들고 입국한다.
그래서 send 앞에서만 멈추고, review 안에서는 아무것도 내보내지 않는다 —
재개하면 review 가 처음부터 다시 돌기 때문이다.
"""
import datetime as dt
import json
import pathlib
import re
import sqlite3
import sys
from types import SimpleNamespace
from typing import Literal

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))                   # 루트의 agent·context·prompts 를 쓴다

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

import agent
import context

HERE = pathlib.Path(__file__).resolve().parent
RUNS = HERE / "runs"
DB = RUNS / "checkpoints.sqlite"
OUTBOX = RUNS / "outbox.jsonl"                  # 고객 메일함 흉내. 여기 한 줄 = 고객이 읽은 답장

RISKY = {"검역", "멸종위기종"}                   # 틀리면 몰수·과태료·처벌까지 가는 영역
DEADLINE_H = 72                                 # 이만큼 아무도 안 보면 상담사 이관
MAX_REDO = 2                                    # 다시 판정 상한. 넘으면 사람이 직접 고치는 편이 빠르다

EXPIRE = ("문의하신 내용은 규정 확인이 더 필요해 담당 상담사가 직접 답변드리겠습니다.\n"
          "급하시면 관세청 고객지원센터(125)로 문의해 주세요.")


class State(agent.State, total=False):
    received_at: str
    판정: str            # read 가 초안을 읽고 붙인다
    인용: str            # 결론을 받치는 근거 문장 — read 가 옮겨 적고
    인용확인: bool       # 그 문장이 정말 근거에 있는지는 코드가 본다
    reasons: list        # 멈춘 이유 — 사람이 읽는 문장
    human: dict          # 담당자의 마지막 답 {action, text}
    redo: int
    instruction: str
    status: str          # 자동 발송 | 승인 발송 | 수정 발송 | 반려 | 기한초과 이관
    sent: str            # 실제로 나간 글


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ───────────────────────── 초안 읽기 ─────────────────────────
class Reading(BaseModel):
    판정: Literal["가능", "조건부 가능", "불가", "해당 없음", "자료에 없음"]
    인용: str = Field(description="[결론]을 받치는 [근거] 문장 하나를 한 글자도 바꾸지 않고 복사. "
                                 "받치는 문장이 [근거]에 없으면 빈 문자열")


READ = """상담원이 [근거]만 보고 [문의]에 답장을 썼다. 너는 그 답장의 [결론]을 **읽기만** 한다.

[문의]
{query}

[결론]
{conclusion}

[근거]
{evidence}

1. 판정 — [결론]이 고객에게 [문의]의 물건에 대해 무엇이라고 말하는가.
   - 가능: 그냥 들여와도 된다. 「검역·신고 대상이 아니다」, 「면세된다」 도 여기다
   - 조건부 가능: 증명서·허가·신고 같은 조건을 갖추면 들여올 수 있다
   - 불가: 들여올 수 없다
   - 해당 없음: 한도·세금·절차만 말하고 물건을 들여와도 되는지는 말하지 않는다
   - 자료에 없음: 안내할 자료가 없다고 말한다
2. 인용 — [결론]을 받치는 [근거]의 문장 하나를 **그대로 복사**한다. 요약하거나 바꿔 쓰지 않는다.
   받친다는 것은 [결론]이 말하는 **행동과 조건**이 그 문장에 적혀 있다는 뜻이다. 주제만 같은 문장은 아니다.
   [근거] 어디에도 없으면 빈 문자열을 준다."""


def read(s: State) -> State:
    """초안을 **다 쓴 뒤에** 읽는다. 답을 쓰는 모델에게 판정 칸을 같이 주면 가부를 먼저 정하고
    근거에 없는 절차를 지어내 맞췄다(agent.Reply 주석). 쓰는 일과 읽는 일을 나눈다.

    답장 전체가 아니라 **결론 한 줄만** 준다. 전체를 주었더니 mini 가 인용을 근거가 아니라
    답장 본문에서 베껴 왔고(지어낸 절차까지 «인용» 으로 옮겼다), 망고 「가져올 수 없습니다」 를
    「가능」 으로 읽었다. 판정이 기준을 가르므로 읽기는 큰 모델(GATE_MODEL)로 한다.
    """
    body = "\n\n".join(f"[{e['제목']}]\n{e['본문']}" for e in s["evidence"])
    prompt = READ.format(query=s["query"], conclusion=s["reply"]["결론"], evidence=body)
    r = agent.client().beta.chat.completions.parse(
        model=agent.GATE_MODEL, temperature=0, response_format=Reading,
        messages=[{"role": "user", "content": prompt}])
    got = r.choices[0].message.parsed
    return {"판정": got.판정, "인용": got.인용.strip(), "인용확인": quoted(got.인용, s["evidence"])}


def squash(t: str) -> str:
    return re.sub(r"[\W_]+", "", t)                  # 띄어쓰기·문장부호 차이는 봐준다


def quoted(q: str, evidence: list) -> bool:
    """옮겨 적은 문장이 근거에 **글자 그대로** 있는가. 모델이 인용을 지어내도 여기서 걸린다."""
    q = squash(q)
    return len(q) >= 8 and q in squash(" ".join(e["본문"] for e in evidence))


# ───────────────────────── 숫자를 품목과 짝지어 보기 ─────────────────────────
# 루트의 숫자 검증(agent.verify)은 답의 숫자가 근거 **어딘가에** 있는지만 본다.
# 「술은 2L 이하, 총 가격 800달러 이하」 가 그래서 통과했다 — 800 은 기본 면세범위라 근거에 있다.
# 정답은 400달러다. 별도 한도가 있는 품목은 숫자를 그 품목과 짝지어, 근거에도 같은 짝이 있는지 본다.
ITEMS = {"주류": ("주류", "술", "위스키", "와인", "맥주", "소주", "양주", "보드카"),
         "담배": ("담배", "궐련", "니코틴"),
         "향수": ("향수",),
         # 가짜 품목. 「주류 면세범위는 기본 면세범위(800달러)와 별도」 의 800 을 주류에서 떼어 낸다
         "기본": ("기본",)}
# 한도에 쓰이는 단위만. %(「자진신고 시 관세의 30% 경감」)는 품목과 무관한 공통 세율이라 뺀다
AMOUNT = re.compile(r"(?:US)?\$\s*(\d[\d,]*)|(\d[\d,]*(?:\.\d+)?)\s*(L|ml|병|개비|보루|kg|g|달러|불|원)")


def _amount(m: re.Match) -> str:
    num = (m.group(1) or m.group(2)).replace(",", "")
    return num + ("달러" if m.group(1) else {"불": "달러"}.get(m.group(3), m.group(3)))


def pairs(text: str) -> set:
    """(품목, 숫자+단위) 짝. 숫자는 **같은 문장에서 바로 앞에 나온 품목**에 붙인다.

    ponytail: 가장 가까운 앞 품목에 붙이는 규칙 하나. 「술과 향수를 제외한 물품은 800달러」 처럼
    부정문이면 엉뚱하게 붙는다(근거 쪽 짝이 늘어 느슨해지는 방향). 늘어나면 구문 분석으로 올린다.
    """
    out = set()
    for sent in re.split(r"(?<=[.!?])\s+|\n", text):
        marks = sorted((m.start(), g) for g, ws in ITEMS.items()
                       for w in ws for m in re.finditer(w, sent))
        for m in AMOUNT.finditer(sent):
            before = [g for pos, g in marks if pos < m.start()]
            if before and before[-1] != "기본":
                out.add((before[-1], _amount(m)))
    return out


def mismatched(answer: str, evidence: list, query: str = "") -> list:
    """답에는 있는데 근거에는 없는 (품목, 숫자) 짝.
    고객이 문의에 쓴 숫자(「1L짜리 두 병, 300달러」)는 AI 의 주장이 아니므로 품목과 상관없이 뺀다."""
    known = set().union(*(pairs(e.get("본문", "")) for e in evidence))
    said = {_amount(m) for m in AMOUNT.finditer(query)}
    return sorted(p for p in pairs(answer) - known if p[1] not in said)


# ───────────────────────── 멈춤 기준 ─────────────────────────
def signals(s: dict) -> dict:
    """멈출지 정하는 재료. **전부 코드로 센다** — 모델이 자기 답을 얼마나 확신하는지는 안 쓴다.
    판정·인용은 모델이 읽어 온 것이지만, 인용이 근거에 있는지는 글자 대조로 본다."""
    cats = {e["카테고리"] for e in s.get("evidence") or []}
    words = s.get("query", "") + " " + " ".join(s.get("terms") or [])
    return {
        "답변": bool(s.get("reply")),                         # 되묻기·넘김은 판정이 없다
        "영역": bool(cats & RISKY),                           # 근거에 검역·CITES 절이 붙었다
        "품목": bool(set(context.cross_hits(words, skip="")) & RISKY),  # 물건 이름이 그 영역이다
        "허용": s.get("판정") in ("가능", "조건부 가능"),
        # 물건이 문서 어휘로 특정되지 않았다(「기념품」·「커피 원두」). 규정만 묻는 문의(「한도가 얼마예요?」)도
        # 여기 걸리므로 채택하지 않고 비교표에만 둔다
        "품목없음": not set(s.get("terms") or []) & set(context.ITEM_TERMS),
        "숫자": bool(s.get("violations")),                    # 재작성 후에도 근거에 없는 숫자
        # 숫자가 다른 품목의 것 (「술 … 800달러」)
        "짝": bool(mismatched(s.get("answer", ""), s.get("evidence") or [], s.get("query", ""))),
        # 결론을 받치는 문장이 근거에 없다. «자료에 없다» 고 답한 초안은 받칠 문장이 없는 게 맞다 —
        # 이 면제는 모델의 판정이 아니라 **결론 글자**로 한다. 지어낸 결론(「공항에서 폐기할 수 있다」)을
        # 읽은 모델이 판정을 「자료에 없음」 으로 붙여서, 판정으로 면제하면 바로 그 건이 빠져나갔다.
        "인용": not s.get("인용확인") and "자료에 없" not in (s.get("reply") or {}).get("결론", ""),
    }


def _ans(f):
    """판정이 담긴 답장에만 기준을 건다."""
    return lambda g: g["답변"] and f(g)


def _risky_item(s: dict) -> str:
    cats = sorted({e["카테고리"] for e in s.get("evidence") or []} & RISKY)
    where = f"근거가 {'·'.join(cats)} 규정" if cats else "물건이 검역·멸종위기종 품목"
    return (f"{where}인데 판정이 「{s['판정']}」 — "
            "틀리면 고객이 물건을 몰수당하거나 과태료·처벌을 받는다")


def _pair_reason(s: dict) -> str:
    ev = s.get("evidence") or []
    known = set().union(*(pairs(e.get("본문", "")) for e in ev))
    parts = []
    for g, a in mismatched(s["answer"], ev, s.get("query", "")):
        ok = sorted(x for gg, x in known if gg == g)
        parts.append(f"{g} {a}" + (f" (근거의 {g}: {', '.join(ok)})" if ok else f" (근거에 {g} 한도가 없음)"))
    return "근거에서 같은 품목과 짝지어 나오지 않는 숫자: " + "; ".join(parts)


# 채택 기준을 이루는 조각. 이름 → (걸리는가, 멈춘 이유 문장)
RULES = {
    "위험 품목 허용": (lambda g: (g["영역"] or g["품목"]) and g["허용"], _risky_item),
    "숫자": (lambda g: g["숫자"],
             lambda s: "재작성 후에도 근거에 없는 숫자가 남음: " + ", ".join(s["violations"])),
    "인용": (lambda g: g["인용"],
             lambda s: "결론을 받치는 문장을 근거에서 찾지 못함 — 근거에 없는 내용을 지어냈을 수 있다"),
    "숫자 짝": (lambda g: g["짝"], _pair_reason),
}


def either(*names):
    return _ans(lambda g: any(RULES[n][0](g) for n in names))


# sweep.py 가 같은 초안으로 전부 재 보고, 그중 하나(CHOSEN)를 그래프가 쓴다.
CRITERIA = {
    "기준 없음": lambda g: False,
    "숫자만": either("숫자"),
    "인용만": either("인용"),
    "영역만": _ans(lambda g: g["영역"]),
    "영역·품목": _ans(lambda g: g["영역"] or g["품목"]),
    "영역·품목 × 허용": either("위험 품목 허용"),
    "영역·품목 × 허용 + 숫자": either("위험 품목 허용", "숫자"),
    "영역·품목 × 허용 + 숫자 + 인용": either("위험 품목 허용", "숫자", "인용"),
    "숫자 짝만": either("숫자 짝"),
    "영역·품목 × 허용 + 숫자 + 인용 + 숫자 짝": either("위험 품목 허용", "숫자", "인용", "숫자 짝"),
    "위 + 품목 미상 × 허용": _ans(lambda g: CRITERIA["영역·품목 × 허용 + 숫자 + 인용 + 숫자 짝"](g)
                                 or (g["품목없음"] and g["허용"])),
    "전부 멈춤": lambda g: True,
}
CHOSEN = "영역·품목 × 허용 + 숫자 + 인용 + 숫자 짝"
CHOSEN_RULES = ("위험 품목 허용", "숫자", "인용", "숫자 짝")    # CHOSEN 과 같은 조각 (자체 점검이 맞춰 본다)


def stop_reasons(s: dict) -> list[str]:
    """CHOSEN 을 사람이 읽는 문장으로. 비어 있으면 자동 발송."""
    g = signals(s)
    if not g["답변"]:
        return []
    return [RULES[n][1](s) for n in CHOSEN_RULES if RULES[n][0](g)]


# ───────────────────────── 노드 ─────────────────────────
def risk(s: State) -> State:
    return {"reasons": stop_reasons(s)}


def payload(s: State) -> dict:
    """승인 화면에 뜨는 것. 담당자가 다른 시스템을 열지 않고 10초 안에 판단할 수 있어야 한다."""
    reasons = list(s.get("reasons") or [])
    if s.get("redo"):
        reasons.append(f"담당자가 다시 판정을 요청함 ({s['redo']}/{MAX_REDO}회) — 고친 초안 재확인")
    rep = s.get("reply") or {}
    return {
        "요청": s["query"],
        "받은 시각": s.get("received_at", ""),
        "판정": s.get("판정", ""),
        "초안": s["answer"],
        "근거 문장": s.get("인용", "") if s.get("인용확인") else "",   # 결론을 받치는 규정 원문 한 줄
        "근거": [{k: e[k] for k in ("제목", "카테고리", "기준일", "출처", "본문")}
                 for e in s.get("evidence") or []],
        "멈춘 이유": reasons,
        "통과시키면": f"이 답장이 고객에게 그대로 발송됩니다. 고객은 「{rep.get('결론', '')}」 를 "
                      "믿고 물건을 들고 입국합니다. 발송은 취소할 수 없습니다.",
        "다시 판정 남은 횟수": MAX_REDO - s.get("redo", 0),
    }


def review(s: State) -> State:
    """사람에게 묻고 답이 올 때까지 멈춘다. **이 안에서는 아무것도 내보내지 않는다.**"""
    return {"human": interrupt(payload(s))}


def redo(s: State) -> State:
    return {"instruction": s["human"]["text"], "redo": s.get("redo", 0) + 1,
            "retried": False, "violations": []}


def reject(s: State) -> State:
    return {"status": "반려"}                    # 사유는 human.text 에 남는다. 아무것도 안 나간다


def sent_ids() -> set:
    if not OUTBOX.exists():
        return set()
    return {json.loads(l)["thread_id"] for l in OUTBOX.read_text(encoding="utf-8").splitlines() if l}


def send(s: State, config) -> State:
    """바깥으로 나가는 유일한 곳. **같은 건을 두 번 보내지 않는다** — 재시작 뒤 이 노드가 다시 돌아도."""
    tid = config["configurable"]["thread_id"]
    action = (s.get("human") or {}).get("action")
    text, status = {
        "approve": (s["answer"], "승인 발송"),
        "edit": ((s.get("human") or {}).get("text"), "수정 발송"),
        "expire": (EXPIRE, "기한초과 이관"),
    }.get(action, (s["answer"], "자동 발송"))
    if tid not in sent_ids():
        OUTBOX.parent.mkdir(exist_ok=True)
        with OUTBOX.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"thread_id": tid, "sent_at": now(), "how": status, "text": text},
                               ensure_ascii=False) + "\n")
    return {"status": status, "sent": text}


# ───────────────────────── 흐름 ─────────────────────────
def route_after_risk(s: State) -> str:
    # 한 번 사람 손을 탄 건은 다시 판정 뒤에도 사람에게 돌아온다
    return "review" if s["reasons"] or s.get("human") else "send"


def route_after_review(s: State) -> str:
    a = s["human"]["action"]
    if a == "redo":
        return "redo" if s.get("redo", 0) < MAX_REDO else "review"
    return {"approve": "send", "edit": "send", "expire": "send"}.get(a, "reject")


LLM_NODES = ("classify", "gate", "normalize", "assemble", "answer", "read")
LLM = SimpleNamespace(**{n: getattr(agent, n) for n in LLM_NODES[:-1]}, read=read)


def build(saver, llm=LLM):
    """`llm` 은 모델을 부르는 노드(LLM_NODES)의 출처. 자체 점검에서만 가짜로 바꾼다."""
    g = StateGraph(State)
    nodes = {n: getattr(llm, n) for n in LLM_NODES}
    nodes.update(ask=agent.ask, verify=agent.verify, handoff=agent.handoff,
                 mark_retry=agent.mark_retry,
                 risk=risk, review=review, redo=redo, reject=reject, send=send)
    for n, f in nodes.items():
        g.add_node(n, f)
    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", agent.route_after_classify,
                            {"gate": "gate", "handoff": "handoff"})
    g.add_conditional_edges("gate", agent.route_after_gate, {"ask": "ask", "normalize": "normalize"})
    g.add_edge("normalize", "assemble")
    g.add_conditional_edges("assemble", agent.route_after_assemble,
                            {"answer": "answer", "handoff": "handoff"})
    g.add_edge("answer", "verify")
    g.add_conditional_edges("verify", agent.route_after_verify, {"retry": "mark_retry", END: "read"})
    g.add_edge("read", "risk")
    g.add_edge("mark_retry", "answer")
    g.add_edge("ask", "send")
    g.add_edge("handoff", "send")
    g.add_conditional_edges("risk", route_after_risk, {"review": "review", "send": "send"})
    g.add_conditional_edges("review", route_after_review,
                            {"send": "send", "reject": "reject", "redo": "redo", "review": "review"})
    g.add_edge("redo", "answer")
    g.add_edge("send", END)
    g.add_edge("reject", END)
    return g.compile(checkpointer=saver)


def open_app(db: pathlib.Path = DB, llm=LLM):
    """체크포인트를 **파일**에 둔다. 담당자가 내일 아침에 봐도, 그 사이 앱을 껐다 켜도 대기 건이 남는다."""
    db.parent.mkdir(exist_ok=True)
    return build(SqliteSaver(sqlite3.connect(db, check_same_thread=False)), llm)


# ───────────────────────── 앱·sweep 이 쓰는 손잡이 ─────────────────────────
def cfg(tid: str) -> dict:
    return {"configurable": {"thread_id": tid}}


def submit(app, tid: str, query: str, received_at: str | None = None) -> dict:
    """문의 한 통을 접수한다. 자동 발송되거나 review 에서 멈춘 채로 돌아온다."""
    app.invoke({**agent.initial(query), "received_at": received_at or now()}, cfg(tid))
    return info(app, tid)


def respond(app, tid: str, action: str, text: str = "") -> dict:
    """담당자의 답을 넣는다. action = approve | edit | reject | redo | expire"""
    app.invoke(Command(resume={"action": action, "text": text}), cfg(tid))
    return info(app, tid)


def info(app, tid: str) -> dict:
    snap = app.get_state(cfg(tid))
    waiting = "review" in snap.next             # 다음 단계가 남아 있으면 대기 중이다
    ints = [i for t in snap.tasks for i in t.interrupts]
    return {"id": tid, "values": snap.values, "waiting": waiting,
            "payload": ints[0].value if waiting and ints else None,
            "status": "승인 대기" if waiting else snap.values.get("status", "처리 중")}


def threads(app) -> list[dict]:
    # ponytail: 체크포인트 전체를 훑는다. 문의가 수천 건이면 thread 목록 테이블을 따로 둔다.
    ids = {c.config["configurable"]["thread_id"] for c in app.checkpointer.list(None)}
    return sorted((info(app, t) for t in ids), key=lambda x: x["values"].get("received_at", ""))


def expire_overdue(app, at: dt.datetime | None = None) -> list[str]:
    """72시간 넘게 아무도 안 본 대기 건을 닫는다. **자동 승인은 하지 않는다** —
    기준에 걸린 건을 시간이 지났다고 내보내면 기준이 없는 것과 같다."""
    at = at or dt.datetime.now()
    out = []
    for t in threads(app):
        got = t["values"].get("received_at")
        if t["waiting"] and got and at - dt.datetime.fromisoformat(got) > dt.timedelta(hours=DEADLINE_H):
            respond(app, t["id"], "expire")
            out.append(t["id"])
    return out


# ───────────────────────── 자체 점검 ─────────────────────────
def _fake_llm():
    """모델 대신 정해진 값을 돌려주는 노드들. 멈춤·재개·보관 배선만 본다."""
    def classify(s):
        q = s["query"]
        return {"category": "범위밖" if "수하물" in q else "검역" if "육포" in q else "면세"}

    def assemble(s):
        return {"evidence": [{"제목": "t", "카테고리": s["category"], "기준일": "2026-09-17",
                              "출처": "x", "본문": "육가공품 검역증명서 미화 800달러"}],
                "tools_called": [context.TOOL_NAMES[s["category"]]]}

    def answer(s):
        risky = s["category"] == "검역"
        verdict = "불가" if s.get("instruction") else "조건부 가능" if risky else "해당 없음"
        return {"answer": f"초안({verdict})", "reply": {"결론": f"결론({verdict})",
                                                         "준비물": [], "자세히": ""}}

    def read(s):
        return {"판정": re.search(r"\((.+)\)", s["answer"]).group(1),
                "인용": "육가공품 검역증명서", "인용확인": True}

    return SimpleNamespace(classify=classify, assemble=assemble, answer=answer, read=read,
                           gate=lambda s: {"missing": []},
                           normalize=lambda s: {"terms": ["육가공품"] if "육포" in s["query"] else [],
                                                "normalized": True})


def demo():
    global OUTBOX
    import tempfile

    # ① 인용 대조: 글자 그대로 있어야 한다. 띄어쓰기·문장부호만 봐준다
    ev = [{"본문": "육가공품(햄, 소시지)은 수출국 검역증명서를 휴대한 경우에 한하여 반입 가능하다."}]
    assert quoted("육가공품(햄,소시지)은 수출국 검역증명서를 휴대한 경우", ev)
    assert not quoted("면세 한도를 넘은 물품은 공항에서 폐기할 수 있다", ev)    # 지어낸 인용
    assert not quoted("육가공품", ev)                                           # 너무 짧으면 인용이 아니다

    # ①-b 숫자 짝: 근거에 있는 숫자라도 **다른 품목의 것**이면 걸린다
    duty = [{"카테고리": "면세", "본문": "- 여행자 휴대품 미화 800달러 이하\n"
                     "- 위항과 별도로 주류(전체 용량이 2L이하이고 총 가격이 미화 400달러 이하), "
                     "필터담배 200개비, 향수 100ml\n"
                     "●담배는 여행자 휴대품 기본면세범위(US$800)에서 제외되어 별도로 면세처리 됩니다."}]
    assert mismatched("술은 2L 이하, 총 가격 800달러 이하입니다.", duty) == [("주류", "800달러")]
    assert mismatched("술은 2L 이하, 총 가격 400달러 이하입니다.", duty) == []
    assert mismatched("술은 2병까지 됩니다.", duty) == [("주류", "2병")]           # 병 수 제한은 옛 기준
    assert mismatched("주류 면세범위는 기본 면세범위(800달러)와 별도입니다.", duty) == []
    assert mismatched("주류를 초과하면 관세의 30%를 경감받습니다.", duty) == []     # 공통 세율은 품목과 무관
    assert mismatched("위스키 1L 두 병은 총 2L 입니다.", duty, "1L짜리 두 병이에요") == []  # 고객이 말한 숫자
    assert mismatched("기본 면세범위는 800달러입니다.", duty) == []                 # 품목이 없으면 짝도 없다

    # ② 기준: 검역 영역의 «가능» 은 멈추고, «불가» 와 면세 답변과 되묻기는 자동
    ev_q = [{"카테고리": "검역"}]
    ev_d = [{"카테고리": "면세"}]
    r = {"결론": "…"}
    ok = {"reply": r, "인용확인": True}
    cases = [
        ({**ok, "evidence": ev_q, "판정": "조건부 가능", "query": "비첸향"}, True),
        ({**ok, "evidence": ev_q, "판정": "불가", "query": "망고"}, False),
        ({**ok, "evidence": ev_d, "판정": "해당 없음", "query": "술 몇 병"}, False),
        ({**ok, "evidence": ev_d, "판정": "해당 없음", "query": "술", "violations": ["3"]}, True),
        # 검색이 면세로 빗나가도 물건 이름(소시지)이 검역이면 멈춘다
        ({**ok, "evidence": ev_d, "판정": "가능", "query": "소시지 30달러 괜찮죠?"}, True),
        # 결론을 받치는 문장이 근거에 없다 — 숫자가 아닌 지어낸 절차. 판정이 «자료에 없음» 이어도 멈춘다
        ({"reply": {"결론": "공항에서 폐기할 수 있습니다."}, "evidence": ev_d, "판정": "자료에 없음",
          "인용확인": False, "query": "버리고 나와도"}, True),
        # 결론이 스스로 «자료에 없다» 고 말하면 받칠 문장이 없는 게 맞다
        ({"reply": {"결론": "그 내용은 안내된 자료에 없다."}, "evidence": ev_d, "판정": "자료에 없음",
          "인용확인": False, "query": "버리고"}, False),
        ({"evidence": [], "reply": {}, "query": "고기 좀 사왔는데"}, False),     # 되묻기는 판정이 없다
        # 면세 답변이라도 술 한도에 기본 면세범위 숫자를 붙이면 멈춘다
        ({**ok, "evidence": duty, "판정": "해당 없음", "query": "술도 신고해야 하나요?",
          "answer": "술은 2L 이하, 총 가격 800달러 이하면 됩니다."}, True),
        ({**ok, "evidence": duty, "판정": "해당 없음", "query": "술도 신고해야 하나요?",
          "answer": "술은 2L 이하, 총 가격 400달러 이하면 됩니다."}, False),
    ]
    for s, want in cases:
        assert bool(stop_reasons(s)) == want == CRITERIA[CHOSEN](signals(s)), s

    assert route_after_review({"human": {"action": "redo"}, "redo": MAX_REDO}) == "review"
    assert route_after_review({"human": {"action": "redo"}, "redo": 0}) == "redo"
    assert route_after_review({"human": {"action": "reject"}}) == "reject"
    assert route_after_risk({"reasons": [], "human": {"action": "redo"}}) == "review"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:   # 윈도는 열린 sqlite 를 못 지운다
        OUTBOX = pathlib.Path(d) / "outbox.jsonl"
        db = pathlib.Path(d) / "c.sqlite"
        app = open_app(db, _fake_llm())

        # ② 위험한 건은 발송 전에 멈춘다. 아무것도 나가지 않았다
        t = submit(app, "T1", "홍콩 육포 사왔어요")
        assert t["waiting"] and t["payload"]["멈춘 이유"] and "T1" not in sent_ids(), t
        # ③ 자동 발송 — 면세 답변과 넘김 문구는 사람을 거치지 않는다
        assert submit(app, "T2", "술 몇 병까지 되나요")["status"] == "자동 발송"
        assert submit(app, "T3", "수하물 몇 kg")["status"] == "자동 발송"

        # ④ 보관 — 앱을 껐다 켠 것처럼 연결을 새로 열어도 T1 이 기다리고 있다
        app = open_app(db, _fake_llm())
        assert [x["id"] for x in threads(app) if x["waiting"]] == ["T1"]

        # ⑤ 다시 판정 → 고친 초안으로 **다시 사람에게** 온다
        t = respond(app, "T1", "redo", "증명서 없으면 불가로")
        assert t["waiting"] and t["payload"]["판정"] == "불가" and t["payload"]["다시 판정 남은 횟수"] == 1
        # ⑥ 승인 → 멈춘 자리부터 이어져 한 번만 나간다
        t = respond(app, "T1", "approve")
        assert t["status"] == "승인 발송" and "T1" in sent_ids()
        send(t["values"], cfg("T1"))                                   # 한 번 더 불러도
        lines = OUTBOX.read_text(encoding="utf-8").splitlines()
        assert sum('"T1"' in l for l in lines) == 1, "같은 답장이 두 번 나갔다"

        # ⑦ 수정 후 승인 — 담당자가 고친 글이 나간다
        submit(app, "T4", "육포 사왔어요")
        assert respond(app, "T4", "edit", "고친 답장")["values"]["sent"] == "고친 답장"
        # ⑧ 반려 — 아무것도 나가지 않고 사유가 남는다
        submit(app, "T5", "육포 사왔어요")
        t = respond(app, "T5", "reject", "원산지 확인 필요")
        assert t["status"] == "반려" and "T5" not in sent_ids() and t["values"]["human"]["text"]
        # ⑨ 72시간 방치 → 자동 승인이 아니라 이관 안내가 나간다
        old = (dt.datetime.now() - dt.timedelta(hours=DEADLINE_H + 1)).isoformat(timespec="seconds")
        submit(app, "T6", "육포 사왔어요", received_at=old)
        submit(app, "T7", "육포 사왔어요")                             # 방금 온 건은 그대로 둔다
        assert expire_overdue(app) == ["T6"]
        assert info(app, "T6")["values"]["sent"] == EXPIRE and info(app, "T7")["waiting"]
    print("hitl.py OK — 기준·멈춤·보관·재개·멱등·기한 규칙 통과 (API 호출 없음)")


if __name__ == "__main__":
    demo()
