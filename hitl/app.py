"""승인 데스크 — 위험한 답장만 사람이 보고 보낸다.

    streamlit run hitl/app.py

문의가 들어오면 에이전트가 답장 초안을 쓴다. 멈춤 기준에 안 걸리면 바로 발송되고,
걸리면 «승인 대기» 에 쌓인다. 대기 건은 runs/checkpoints.sqlite 에 있어서 앱을 껐다 켜도 남는다.
"""
import datetime as dt
import html
import json
import os

import streamlit as st

try:                                            # 클라우드에서는 키가 secrets 로 들어온다
    if "OPENAI_API_KEY" in st.secrets:
        os.environ.setdefault("OPENAI_API_KEY", st.secrets["OPENAI_API_KEY"])
except Exception:
    pass

import hitl

st.set_page_config(page_title="기념품 반입 답장 승인", page_icon="🛃", layout="wide")

# 화면에서 크게 튀는 것은 «통과시키면» 하나다. 승인 버튼의 무게는 이 줄이 정한다.
st.markdown("""<style>
.stamp{border:2px solid #b3261e;color:#b3261e;border-radius:4px;padding:.8rem 1rem;
       font-weight:600;line-height:1.55;margin:.25rem 0 1rem}
.quote{border-left:3px solid #8a8f98;padding:.2rem 0 .2rem .9rem;font-size:1.1rem;margin:.2rem 0 1rem}
</style>""", unsafe_allow_html=True)


@st.cache_resource
def get_app():
    return hitl.open_app()


app = get_app()
hitl.expire_overdue(app)                        # 72시간 넘은 대기 건은 열 때마다 정리한다
all_threads = hitl.threads(app)
pending = [t for t in all_threads if t["waiting"]]


def age(iso: str) -> str:
    if not iso:
        return ""
    h = (dt.datetime.now() - dt.datetime.fromisoformat(iso)).total_seconds() / 3600
    left = hitl.DEADLINE_H - h
    got = f"{int(h * 60)}분 전" if h < 1 else f"{int(h)}시간 전"
    return f"{got} 접수, 이관까지 {max(int(left), 0)}시간"


with st.sidebar:
    st.subheader("사람에게 오는 건")
    st.markdown(
        "- 검역·멸종위기종 품목인데 AI가 **가져와도 된다**(가능·조건부 가능)고 답한 초안\n"
        "- 재작성 후에도 **근거에 없는 숫자**가 남은 초안\n"
        "- 결론을 받치는 **문장이 근거 문서에 없는** 초안\n"
        "- 술·담배·향수에 붙인 숫자가 **근거에서 그 품목의 숫자가 아닌** 초안")
    st.subheader("바로 나가는 건")
    st.markdown("- 면세 한도처럼 숫자로 답하고 검증을 통과한 초안\n"
                "- 검역 품목이라도 **불가**라고 답한 초안\n"
                "- 되묻기·넘김 같은 정해진 문구")
    st.caption(f"대기 건은 {hitl.DEADLINE_H}시간 안에 처리하지 않으면 자동 승인하지 않고 "
               "상담사 이관 안내를 보냅니다.")
    st.divider()
    st.caption(f"대기 {len(pending)}건 · 전체 {len(all_threads)}건. "
               "대기 건은 파일에 저장돼 앱을 다시 켜도 남습니다.")

st.title("기념품 반입 답장 승인")

tab_in, tab_wait, tab_log, tab_rule = st.tabs(
    ["문의 받기", f"승인 대기 {len(pending)}", "처리 내역", "기준 비교"])

# ───────────────────────── 문의 받기 ─────────────────────────
EXAMPLES = [
    ("비첸향 육포", "홍콩에서 비첸향 사왔는데 괜찮을까요?"),
    ("술 몇 병", "면세로 술 몇 병까지 가져올 수 있나요?"),
    ("두리안", "태국에서 두리안 사왔는데 되나요?"),
    ("악어가죽 지갑", "악어가죽 지갑 200달러짜리 샀는데요"),
    ("수하물 무게", "비행기 수하물은 몇 kg까지 실을 수 있어요?"),
]

with tab_in:
    st.caption("여행자가 보낸 문의라고 생각하고 넣어 보세요. 에이전트가 답장 초안을 쓰고, "
               "보낼지 멈출지를 기준대로 정합니다.")
    cols = st.columns(len(EXAMPLES))
    for col, (label, q) in zip(cols, EXAMPLES):
        if col.button(label, help=q, width="stretch"):
            st.session_state.draft_q = q
    q = st.text_area("문의 내용", key="draft_q", height=90,
                     placeholder="예: 스페인에서 하몽 한 덩어리 사왔어요.")
    if st.button("접수", type="primary", disabled=not q.strip()):
        tid = "W" + dt.datetime.now().strftime("%m%d-%H%M%S")
        with st.spinner("근거를 찾고 초안을 쓰는 중…"):
            st.session_state.last = hitl.submit(app, tid, q.strip())
        st.rerun()

    if last := st.session_state.get("last"):
        v = last["values"]
        if last["waiting"]:
            st.warning(f"**{last['id']}** 는 발송하지 않고 승인 대기로 보냈습니다.")
            for r in last["payload"]["멈춘 이유"]:
                st.markdown(f"- {r}")
            st.caption("«승인 대기» 탭에서 처리하세요.")
        else:
            st.success(f"**{last['id']}** 는 기준에 걸리지 않아 바로 발송했습니다 ({v.get('status')}).")
            with st.expander("발송된 답장"):
                st.markdown(v.get("sent", ""))

# ───────────────────────── 승인 대기 ─────────────────────────
ACTIONS = {
    "approve": ("그대로 승인", "이 답장 발송"),
    "edit": ("고쳐서 승인", "고친 답장 발송"),
    "reject": ("반려", "반려하고 발송하지 않기"),
    "redo": ("다시 판정", "다시 판정 요청"),
}

with tab_wait:
    if not pending:
        st.info("기다리는 건이 없습니다. «문의 받기» 에서 비첸향이나 악어가죽 지갑을 넣어 보세요.")
    else:
        left, right = st.columns([1, 2.4], gap="large")
        with left:
            pick = st.radio(
                "대기 중", [t["id"] for t in pending], label_visibility="collapsed",
                format_func=lambda i: next(t["payload"]["요청"][:26] for t in pending if t["id"] == i))
        t = next(x for x in pending if x["id"] == pick)
        p = t["payload"]
        with right:
            st.caption(f"{t['id']}, {age(p['받은 시각'])}")
            st.markdown(f"<div class='quote'>{html.escape(p['요청'])}</div>", unsafe_allow_html=True)

            st.markdown("**멈춘 이유**")
            for r in p["멈춘 이유"]:
                st.error(r)

            st.markdown("**AI 초안**")
            st.badge(f"판정: {p['판정']}", color="orange")
            with st.container(border=True):
                st.markdown(p["초안"])

            st.markdown("**결론을 받치는 규정 원문**")
            if p["근거 문장"]:
                st.markdown(f"<div class='quote'>{html.escape(p['근거 문장'])}</div>",
                            unsafe_allow_html=True)
            else:
                st.warning("근거 문서에서 결론을 받치는 문장을 찾지 못했습니다.")

            st.markdown(f"**근거로 쓴 규정 전체** {len(p['근거'])}개")
            for e in p["근거"]:
                with st.expander(f"{e['제목']}  ({e['카테고리']}, {e['기준일']} 기준)"):
                    st.text(e["본문"])
                    st.caption(f"출처: {e['출처']}")

            st.markdown("**통과시키면**")
            st.markdown(f"<div class='stamp'>{html.escape(p['통과시키면'])}</div>",
                        unsafe_allow_html=True)

            # 다시 판정 뒤에는 고친 초안이 새로 오므로 입력칸도 새로 시작한다
            k = f"{pick}-{p['다시 판정 남은 횟수']}"
            choices = [a for a in ACTIONS if a != "redo" or p["다시 판정 남은 횟수"] > 0]
            act = st.segmented_control("어떻게 할까요", choices, key=f"act-{k}",
                                       format_func=lambda a: ACTIONS[a][0])
            text, ok = "", act is not None
            if act == "edit":
                text = st.text_area("고친 답장 (이대로 발송됩니다)", value=p["초안"], height=260,
                                    key=f"edit-{k}")
            elif act == "reject":
                text = st.text_input("반려 사유 (기준을 고칠 때 씁니다)", key=f"why-{k}",
                                     placeholder="예: 원산지가 수입 가능국인지 확인 필요")
                ok = bool(text.strip())
            elif act == "redo":
                text = st.text_input(f"AI에게 줄 지시 (남은 횟수 {p['다시 판정 남은 횟수']})",
                                     key=f"how-{k}",
                                     placeholder="예: 증명서가 없으면 반입 불가라는 점을 결론에 먼저 쓰세요")
                st.caption("AI는 근거 문서 안에서만 고쳐 씁니다. 근거에 없는 사실을 더해야 하면 "
                           "«고쳐서 승인» 으로 직접 고치세요.")
                ok = bool(text.strip())
            if act and st.button(ACTIONS[act][1], type="primary", disabled=not ok):
                with st.spinner("처리 중…" if act != "redo" else "다시 쓰는 중…"):
                    res = hitl.respond(app, pick, act, text.strip())
                st.toast(f"{pick}: {res['status']}")
                st.rerun()

# ───────────────────────── 처리 내역 ─────────────────────────
with tab_log:
    rows = [{"건": t["id"], "접수": t["values"].get("received_at", ""), "상태": t["status"],
             "요청": t["values"].get("query", ""),
             # 반려 사유, 또는 다시 판정 때 준 지시
             "담당자 메모": (t["values"].get("human") or {}).get("text", "") if t["status"] == "반려"
             else t["values"].get("instruction", "")}
            for t in reversed(all_threads)]
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.info("아직 접수된 문의가 없습니다.")

    st.subheader("발송함")
    st.caption("고객에게 실제로 나간 답장입니다. 여기 들어간 것은 되돌릴 수 없습니다.")
    sent = [json.loads(l) for l in hitl.OUTBOX.read_text(encoding="utf-8").splitlines()
            if l] if hitl.OUTBOX.exists() else []
    for s in reversed(sent[-30:]):
        with st.expander(f"{s['thread_id']}  {s['how']}  {s['sent_at']}"):
            st.markdown(s["text"])

# ───────────────────────── 기준 비교 ─────────────────────────
with tab_rule:
    table = hitl.HERE / "criteria.md"
    if table.exists():
        st.markdown(table.read_text(encoding="utf-8").split("\n## 건별")[0].split("\n", 2)[2])
        st.caption("같은 51건의 초안에 기준만 바꿔 적용한 결과입니다. `python hitl/sweep.py --table` 로 다시 만듭니다.")
    else:
        st.info("`python hitl/sweep.py` 를 먼저 돌리면 기준별 비교표가 여기 나옵니다.")
