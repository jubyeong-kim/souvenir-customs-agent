"""데모 화면 — 답변만 보여주면 봇을 믿을 근거가 없다.

    streamlit run app.py

답변마다 다섯 칸을 같이 보여준다: 분류 · 호출한 도구 · 용어 연결 · 근거 절 원문 · 검증 결과.
이게 없으면 사용자는 맞는 답과 그럴듯한 답을 구분할 수 없다.

되묻기가 있으므로 **대화형**이다. 이어서 답하면 앞 턴의 정보를 합쳐 판정한다.
"""
import os

import streamlit as st

# Streamlit Community Cloud 에서는 키가 환경변수가 아니라 secrets 로 들어온다.
# agent.py 는 환경변수만 보므로 여기서 한 번 옮겨 준다. 로컬에서는 .env 가 그대로 쓰인다.
try:
    if "OPENAI_API_KEY" in st.secrets:
        os.environ.setdefault("OPENAI_API_KEY", st.secrets["OPENAI_API_KEY"])
except Exception:
    pass

import agent, context

st.set_page_config(page_title="기념품 반입 상담", page_icon="🧳", layout="centered")

st.title("🧳 기념품 반입 상담")
st.caption("해외에서 산 기념품을 한국으로 들여올 때의 면세·검역·CITES 문의에 답합니다.")

ASOF = max((s["기준일"] for s in context.SECTIONS), default="?")
st.warning(
    f"**실습용 데모입니다.** 답변은 '근거' 칸의 관세청·검역 안내 자료에서만 나옵니다 "
    f"({ASOF} 기준). 실제 통관은 관세청 고객지원센터(125)에 확인하세요.",
    icon="⚠️",
)

with st.sidebar:
    st.subheader("근거 문서")
    for cat in sorted({s["카테고리"] for s in context.SECTIONS}):
        secs = [s for s in context.SECTIONS if s["카테고리"] == cat]
        st.write(f"**{cat}** — 절 {len(secs)}개")
        st.caption(secs[0]["출처"])
    st.divider()
    st.caption("입국 시 반입 기준만 다룹니다. 출국 반출·상대국 규정은 범위 밖입니다.")
    st.divider()
    if st.button("대화 새로 시작", use_container_width=True):
        st.session_state.turns = []
        st.rerun()

# turns: [(사용자 발화, State)] — State 에 답변·근거·검증이 다 들어 있다
st.session_state.setdefault("turns", [])


def ask_bot(text: str) -> None:
    """한 턴 돌린다. 이전 턴들을 history 로 넘긴다 — 이어 붙이지 않고 따로."""
    history = [(u, s["answer"]) for u, s in st.session_state.turns]
    with st.spinner("근거를 찾는 중…"):
        st.session_state.turns.append((text, agent.run(text, history)))


# ── 예시: 버튼에는 짧은 이름표만. 긴 문장을 label 로 쓰면 좁은 칸에서 잘린다.
EXAMPLES = [
    ("면세 한도 초과", "면세 한도 넘으면 세금 얼마나 더 내나요?"),
    ("비첸향", "홍콩에서 비첸향 사왔는데 괜찮을까요?"),
    ("악어가죽 지갑", "악어가죽 지갑 기념품으로 샀는데 괜찮을까요?"),
    ("가방? 💬", "가방 하나 샀어요. 신고해야 되나요?"),
    ("예전엔 2병?", "예전엔 술 2병까지 됐잖아요. 지금은 어떻게 되나요?"),
    ("수하물 무게 ↩", "비행기 수하물 몇 kg까지 실을 수 있어요?"),
]
if not st.session_state.turns:
    st.caption("예시를 눌러 보세요.  💬 는 되물어서 **대화가 이어지는** 문의, "
               "↩ 는 근거 자료에 답이 없어 **넘기는** 문의입니다.")
    for row in range(0, len(EXAMPLES), 3):
        for col, (label, q) in zip(st.columns(3), EXAMPLES[row:row + 3]):
            if col.button(label, help=q, use_container_width=True):
                ask_bot(q)
                st.rerun()

# ── 대화
for i, (user_text, s) in enumerate(st.session_state.turns):
    with st.chat_message("user"):
        st.write(user_text)
    with st.chat_message("assistant"):
        rep = s.get("reply") or {}
        if rep.get("결론"):
            # 결론을 먼저, 크게. 긴 줄글은 그 아래로 내린다.
            st.markdown(f"### {rep['결론']}")
            if rep.get("준비물"):
                st.markdown("**입국할 때 챙기세요**")
                for item in rep["준비물"]:
                    st.markdown(f"- {item}")
            if rep.get("자세히"):
                st.markdown(rep["자세히"])
        else:
            st.markdown(s["answer"])       # 되묻기·넘김은 토막이 없다

        label = ("되묻기" if s.get("missing")
                 else "넘김" if not s["tools_called"] else "답변")
        with st.expander(f"근거와 검증 보기  ·  {label}", expanded=(i == len(st.session_state.turns) - 1)):
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**① 분류**")
                st.info(s["category"])
            with c2:
                st.markdown("**② 호출한 도구**")
                if s["tools_called"]:
                    for t in s["tools_called"]:
                        st.success(f"`{t}`")
                elif s.get("missing"):
                    st.warning("호출 없음 — 되묻기")
                else:
                    st.warning("호출 없음 — 넘김")

            al = [f"{k} → {v}" for k, v in context.aliases(user_text)]
            if s.get("terms"):
                al.append("물건 → " + ", ".join(s["terms"]))
            if al:
                st.markdown("**③ 용어 연결** — 질문의 말을 문서의 말로 이은 것 "
                            "(사전과 닫힌 목록에서 나온 것, 모델 추측 아님)")
                st.info(" · ".join(al))

            st.markdown("**④ 근거로 쓴 문서 부분**")
            if not s["evidence"]:
                st.caption("근거로 답하지 않았습니다. 지어내는 대신 되묻거나 넘겼습니다.")
            for sec in s["evidence"]:
                with st.expander(f"{sec['제목']}  ·  {sec['카테고리']}  ·  {sec['기준일']} 기준"):
                    st.text(sec["본문"])
                    st.caption(f"출처: {sec['출처']}")

            st.markdown("**⑤ 검증** — 답변의 숫자가 근거에 있는지 역추적")
            if s["violations"]:
                st.error(f"근거에 없는 숫자: {', '.join(s['violations'])}"
                         + ("  (재작성했으나 남음)" if s.get("retried") else ""))
            else:
                st.success("통과 — 답변의 모든 숫자가 근거에 있습니다"
                           + ("  (1회 재작성 후)" if s.get("retried") else ""))

if q := st.chat_input("문의 내용 (예: 술 몇 병까지 면세되나요?)"):
    ask_bot(q)
    st.rerun()
