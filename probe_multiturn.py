"""되묻고 나서 이어지는 대화를 돌려 본다. 채점하지 않는다.

    python probe_multiturn.py

**2번째 턴이 진짜 전장이다.** 모두몰 때 1턴 78% 인데 2턴은 47% 였고,
1턴만 재면 그걸 못 본다. (chatbot/PLAYBOOK.md 4-④)

보는 것 셋:
  1. 되물은 것을 사용자가 답하면 **이어서 판정**하는가
  2. 이미 받은 정보를 **다시 묻지 않는가**
  3. **이번 발화**에 답하는가 (이전 질문에 답하지 않는가)
"""
import agent

CONVERSATIONS = [
    (["가방 하나 샀어요. 신고해야 되나요?",
      "악어가죽 가방이고 500달러예요."],
     "되묻기 → 재질·금액을 받아 CITES 로 판정해야 한다"),

    (["이거 세금 내야 하나요?",
      "면세점에서 산 위스키요. 1L짜리 두 병이고 300달러 줬어요."],
     "되묻기 → 품목·용량·금액을 한 번에 받았다. 1L×2 = 2L 로 한도 안이라는 판정이 나와야 한다"),

    (["한약재 가져와도 돼요?",
      "웅담이요."],
     "되묻기 → CITES 대상. 허가·처벌까지 답해야 한다"),

    (["동물 가죽으로 만든 제품인데 문제 있을까요?",
      "그냥 소가죽이에요."],
     "되묻기 → 소가죽은 CITES 대상이 아니다. '대상 아님' 을 분명히 말해야 한다"),

    (["면세 한도가 얼마예요?",
      "그럼 술은요?"],
     "1턴에 답했고 2턴은 **이어지는 질문**이다. 되묻지 말고 주류 한도를 답해야 한다"),
]


def main():
    for i, (turns, why) in enumerate(CONVERSATIONS, 1):
        print(f"\n{'=' * 78}\n[{i}] {why}")
        for turn, s in zip(turns, agent.converse(turns)):
            mark = "되묻기" if s["missing"] else ("넘김" if not s["tools_called"] else "답변")
            print(f"\n  사용자 › {turn}")
            print(f"  [{mark}] 분류 {s['category']} · 도구 {s['tools_called'] or '없음'}"
                  f" · 어휘 {s.get('terms') or '없음'}"
                  f" · 검증 {'위반 ' + str(s['violations']) if s['violations'] else '통과'}")
            for line in s["answer"].splitlines():
                if line.strip():
                    print(f"  상담원 › {line}")


if __name__ == "__main__":
    main()
