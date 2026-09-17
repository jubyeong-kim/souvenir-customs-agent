"""데모 화면 — 답변만 보여주면 봇을 믿을 근거가 없다.

    streamlit run app.py

답변 아래 세 칸을 반드시 같이 보여준다: 어느 도구를 불렀는지, 어느 절을 근거로 썼는지,
검증이 무엇을 잡았는지. 이게 없으면 사용자는 맞는 답과 그럴듯한 답을 구분할 수 없다.
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
    f"**실습용 데모입니다.** 답변은 아래 '근거' 칸의 관세청·검역 안내 자료에서만 나옵니다 "
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

EXAMPLES = [
    "면세 한도 넘으면 세금 얼마나 더 내나요?",
    "파리에서 소시지 사왔는데 들고 들어와도 되나요?",
    "악어가죽 지갑 기념품으로 샀는데 괜찮을까요?",
    "입국장면세점에서 얼마까지 살 수 있나요?",
    "비행기 수하물 몇 kg까지 실을 수 있어요?",
]
cols = st.columns(len(EXAMPLES))
for col, ex in zip(cols, EXAMPLES):
    if col.button(ex[:9] + "…", help=ex, use_container_width=True):
        st.session_state["query"] = ex

query = st.text_input("문의 내용", key="query", placeholder="예: 술 몇 병까지 면세되나요?")

if query:
    with st.spinner("근거를 찾는 중…"):
        s = agent.run(query)

    st.markdown("### 답변")
    st.markdown(s["answer"])

    st.markdown("---")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**① 분류**")
        st.info(s["category"])
    with c2:
        st.markdown("**② 호출한 도구**")
        if s["tools_called"]:
            for t in s["tools_called"]:
                st.success(f"`{t}`")
        else:
            st.warning("호출 없음 — 넘김")

    st.markdown("**③ 근거로 쓴 문서 부분**")
    if not s["evidence"]:
        st.caption("근거를 찾지 못해 답하지 않았습니다. 지어내는 대신 넘겼습니다.")
    for sec in s["evidence"]:
        with st.expander(f"{sec['제목']}  ·  {sec['카테고리']}  ·  {sec['기준일']} 기준"):
            st.text(sec["본문"])
            st.caption(f"출처: {sec['출처']}")

    st.markdown("**④ 검증** — 답변의 숫자가 근거에 있는지 역추적")
    if s["violations"]:
        st.error(f"근거에 없는 숫자: {', '.join(s['violations'])}"
                 + ("  (재작성했으나 남음)" if s.get("retried") else ""))
    else:
        st.success("통과 — 답변의 모든 숫자가 근거에 있습니다"
                   + ("  (1회 재작성 후)" if s.get("retried") else ""))
