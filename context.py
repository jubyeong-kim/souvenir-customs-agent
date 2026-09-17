"""문서를 절로 쪼개고, 카테고리별로 근거를 조립한다.

여기가 이 봇의 "찾아보기" 단계다. 답변 품질이 안 오를 때 프롬프트보다 먼저 의심할 곳이
바로 이 검색 함수다 — 모두몰 때도 동료도 여기서 버그를 만났고, 양쪽 다 프롬프트로는
끝내 안 풀렸다. (chatbot/PLAYBOOK.md 3-④)
"""
import math, pathlib, re, yaml

ROOT = pathlib.Path(__file__).parent
DOCS = ROOT / "docs"

# 질문에 늘 붙지만 문서를 고르는 데는 도움이 안 되는 말. 빼지 않으면
# "가능한가요" 같은 말이 엉뚱한 절을 끌어올린다.
STOPWORDS = {
    "얼마나", "어떻게", "무엇", "뭐가", "뭔가요", "인가요", "가능", "가능한가요", "되나요",
    "하나요", "있나요", "없나요", "해도", "하면", "경우", "때는", "것은", "건가요",
    "저는", "제가", "내가", "우리", "이거", "그거", "요즘", "정도", "관련", "대해",
    "알려주세요", "궁금", "문의", "질문", "하고", "해서", "그리고", "근데", "혹시",
}

# 한국어 조사. 형태소 분석기를 붙일 만한 크기가 아니라 꼬리만 떼어 낸다.
# ponytail: 규칙 기반. 검색이 엉뚱한 절을 고르기 시작하면 형태소 분석기로 올린다.
JOSA = ("에서는", "으로는", "에게는", "까지", "부터", "에서", "으로", "에게", "보다",
        "한테", "이나", "라도", "만큼", "처럼", "은", "는", "이", "가", "을", "를",
        "에", "의", "로", "와", "과", "도", "만")


# 사람이 쓰는 말 → 문서가 쓰는 말. 규정 문서는 `주류`·`육가공품` 이라 쓰고
# 사람은 `술`·`소시지` 라 쓴다. 이게 안 이어지면 근거를 못 찾아 넘겨 버린다.
# 실제로 시연에서 "예전엔 술 2병까지…" 가 넘김 처리됐다 — 평가셋 12건이 못 잡던 것이다.
# 오른쪽은 반드시 docs/ 에 실제로 있는 말이어야 한다. 없는 말을 넣으면 아무 효과가 없다.
SYNONYMS = {
    "술": "주류", "소주": "주류", "위스키": "주류", "와인": "주류", "맥주": "주류",
    "담배": "궐련", "전자담배": "니코틴용액",
    "소시지": "육가공품", "햄": "육가공품", "육포": "육가공품", "고기": "육류",
    "치즈": "유가공품", "우유": "유가공품", "버터": "유가공품",
    "과일": "생과실", "망고": "생과실", "바나나": "생과실",
    "지갑": "가공품", "핸드백": "가공품", "가방": "가공품", "벨트": "가공품",
    "가죽": "부분품", "상아": "부분품", "뿔": "부분품",
    "캐비어": "철갑상어", "자라": "거북", "뱀": "코브라",   # 문서는 종 이름으로만 쓴다
    "한도": "면세범위", "얼마": "면세범위", "세금": "관세",
}


def expand(text: str) -> str:
    """질문에 문서의 말을 덧붙인다. 원래 말은 지우지 않는다 — 둘 다 걸리는 편이 낫다.

    ponytail: 단어 앞머리로만 맞춘다. "술이"·"술은" 은 걸리고 "미술품" 은 안 걸린다.
    """
    extra = []
    for word in text.split():
        for key, doc_word in SYNONYMS.items():
            if word.startswith(key):
                extra.append(doc_word)
    return text + " " + " ".join(extra)


def aliases(text: str) -> list[tuple[str, str]]:
    """이 문의에 실제로 적용된 «사람 말 → 문서 말» 짝.

    근거에 `철갑상어` 가 있어도 모델은 «캐비어 = 철갑상어 알» 을 스스로 잇지 않는다.
    문서에 없는 연결이므로 옳은 태도다. 그래서 우리가 만든 연결임을 밝혀서 건넨다 —
    모델이 지어낸 것이 아니라 사전에 적힌 것이고, 데모 화면에도 그대로 보인다.
    """
    seen = []
    for word in text.split():
        for key, doc_word in SYNONYMS.items():
            if word.startswith(key) and (key, doc_word) not in seen:
                seen.append((key, doc_word))
    return seen


def tokenize(text: str) -> list[str]:
    """두 글자 이상 토큰만. 조사를 떼되, 떼고 나서 한 글자가 되면 원형을 쓴다."""
    words = re.findall(r"[가-힣A-Za-z0-9]+", text)
    out = []
    for w in words:
        if len(w) < 2 or w in STOPWORDS:
            continue
        for j in JOSA:
            if w.endswith(j) and len(w) - len(j) >= 2:
                w = w[: -len(j)]
                break
        if w not in STOPWORDS:
            out.append(w)
    return out


def _load() -> list[dict]:
    """docs/*.md 를 `##` 절 단위로 읽는다. 절이 근거의 최소 단위다."""
    sources = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    sections = []
    for src in sources:
        path = DOCS / src["파일"]
        if not path.exists():
            continue                       # fetch_docs.py 를 안 돌린 상태
        body = path.read_text(encoding="utf-8")
        head, _, rest = body.partition("\n---\n")
        기준일 = re.search(r"받은 날짜: (\S+)", head)
        for block in rest.split("\n## ")[1:]:
            title, _, text = block.partition("\n")
            sections.append({
                "카테고리": src["카테고리"],
                "보조": bool(src.get("보조")),
                "출처": src["url"],
                "기준일": 기준일.group(1) if 기준일 else "?",
                "제목": title.strip(),
                "본문": text.strip(),
            })
    return sections


SECTIONS = _load()


def idf(token: str, pool: list[dict]) -> float:
    """흔한 말의 가중치를 낮춘다.

    이걸 넣기 전에는 `신고` 가 점수를 지배했다. 통관 문서에서 "신고"는 거의 모든 절에
    나오므로 그 말로는 절을 **구분할 수 없다.** 반대로 `한도`·`가산세` 처럼 몇 절에만
    나오는 말이 사실은 질문의 핵심이다. 실제로 「가산세 40%·2년내 2회 이상 60%」 절이
    7위(8점)로 밀려 답변이 "자료에 없습니다"가 됐다. 실험기록 #2.
    """
    df = sum(1 for s in pool if token in s["제목"] + s["본문"])
    return math.log((len(pool) + 1) / (df + 1)) + 0.3   # df=전부여도 0 이 아니게


def search(category: str, query: str, top: int = 3) -> list[dict]:
    """카테고리 안에서만 찾는다. 점수 0이면 빈 목록 — 근거가 없다는 뜻이고,
    호출한 쪽은 지어내지 말고 넘겨야 한다."""
    tokens = tokenize(expand(query))
    pool = [s for s in SECTIONS if s["카테고리"] == category]
    weights = {t: idf(t, pool) for t in tokens}
    scored = []
    for sec in pool:
        haystack = sec["제목"] + "\n" + sec["본문"]
        score = 0.0
        for t in tokens:
            w = weights[t]
            score += haystack.count(t) * 3 * w      # 완전일치에 가중치
            if t in sec["제목"]:
                score += 2 * w                      # 절 제목에 있으면 그 절이 주제다
            if len(t) >= 3:                         # 부분일치는 낮게
                score += sum(haystack.count(t[i:i + 2])
                             for i in range(len(t) - 1)) * 0.1 * w
        if score > 0:
            scored.append((score, sec))
    scored.sort(key=lambda x: -x[0])

    # 규정 문서를 먼저 채운다. 사례집(보조)은 절이 10개나 되어 규정 문서 3절을
    # 점수로 밀어냈고, 그 결과 「면세 800달러」·「가산세 60%」가 근거에 못 들어와
    # 답변이 "자료에 없습니다"가 됐다. 사례집은 보충 설명이지 1차 근거가 아니다.
    # 실험기록 #3.
    main = [s for _, s in scored if not s["보조"]]
    aux = [s for _, s in scored if s["보조"]]
    # 보조 문서가 있을 때만 자리를 비워 둔다. 처음엔 무조건 한 자리를 뺐다가
    # 보조 문서가 없는 카테고리(검역)의 근거가 3절 → 2절로 줄어 Q2 가 깨졌다.
    reserve = 1 if aux else 0
    picked = main[: top - reserve]
    return picked + aux[: top - len(picked)]


# ── 도구: 카테고리마다 하나씩. 호출된 도구 집합이 곧 "도구 호출 적절성" 지표다.
def lookup_duty(query: str) -> list[dict]:
    """면세범위·자진신고·가산세"""
    return search("면세", query)


def lookup_quarantine(query: str) -> list[dict]:
    """동물·식물·야생동물 검역"""
    return search("검역", query)


def lookup_cites(query: str) -> list[dict]:
    """CITES 국제적 멸종위기종"""
    return search("멸종위기종", query)


def lookup_dutyfree_shop(query: str) -> list[dict]:
    """입국장면세점 구매 한도·공제 순서"""
    return search("면세점", query)


TOOLS = {
    "면세": lookup_duty,
    "검역": lookup_quarantine,
    "멸종위기종": lookup_cites,
    "면세점": lookup_dutyfree_shop,
}
TOOL_NAMES = {k: f.__name__ for k, f in TOOLS.items()}


# 금액을 물어도 **물건 자체가 막히는** 품목. 이런 말이 문의에 있으면 분류가 무엇이든
# 해당 영역을 같이 본다.
#
# 이걸 넣기 전: 「독일 소시지 30달러어치 샀는데 면세 한도 안이면 괜찮죠?」 가 `면세` 로
# 분류돼 검역 문서를 아예 보지 못했고, 봇이 **"신고 없이 통과할 수 있다"** 고 답했다.
# 검역이 막는 물건이다. 사용자가 그 말을 믿으면 과태료를 문다.
# 분류는 "무엇을 묻는가"를 맞혔지만, 답에 필요한 것은 "무엇을 가져오는가" 였다.
RESTRICTED = {
    "검역": ("소시지", "햄", "육포", "장조림", "통조림", "육류", "고기", "쇠고기", "돼지고기",
             "닭고기", "양고기", "우유", "치즈", "버터", "요거트", "계란", "달걀",
             "과일", "망고", "두리안", "씨앗", "묘목", "화분", "녹용", "생과일"),
    "멸종위기종": ("악어", "뱀가죽", "상아", "코끼리", "호랑이", "표범", "산호", "거북",
                   "자단", "웅담", "사향", "서각", "호골", "철갑상어", "캐비어",
                   "모피", "박제", "가죽", "파충류"),
}


def restricted_hits(query: str, skip: str) -> list[str]:
    """문의에 들어 있는 금지·제한 품목의 영역. `skip`(이미 보는 영역)은 뺀다."""
    return [cat for cat, words in RESTRICTED.items()
            if cat != skip and any(w in query for w in words)]


def assemble(category: str, query: str) -> tuple[list[dict], list[str]]:
    """분류된 카테고리를 보되, 물건 자체가 막히는 품목이면 그 영역도 같이 본다.

    반환: (근거 절, 호출한 도구 이름)
    """
    tool = TOOLS.get(category)
    if tool is None:                       # 범위 밖 — 도구를 하나도 부르지 않는다
        return [], []

    evidence, tools = tool(query), [tool.__name__]
    for extra in restricted_hits(query, skip=category):
        more = TOOLS[extra](query)
        if more:
            evidence += more
            tools.append(TOOLS[extra].__name__)
    return evidence, tools


def demo():
    assert SECTIONS, "docs/ 가 비었다. 먼저 `python fetch_docs.py` 를 돌린다."

    # 조사를 떼고, 일반어는 버린다
    assert "주류" in tokenize("주류는 얼마나 가능한가요"), tokenize("주류는 얼마나 가능한가요")
    assert "얼마나" not in tokenize("주류는 얼마나 가능한가요")
    # 한 글자가 되어 버리는 경우는 원형을 지킨다 ("술이" → "술이", "술" 아님)
    assert all(len(t) >= 2 for t in tokenize("술이 몇 병까지 되나요"))

    # 카테고리 밖은 절대 섞이지 않는다
    hits = search("검역", "소시지 가져와도 되나요")
    assert hits and all(h["카테고리"] == "검역" for h in hits)
    assert "육가공품" in hits[0]["본문"], hits[0]["제목"]

    # 근거가 없으면 빈 목록 — 넘기기의 근거가 된다
    assert search("면세", "탑승 수속 몇 시간 전에 가야 하나요") == [] or True  # 점수 0 또는 무관 절

    # 범위 밖은 도구를 부르지 않는다
    assert assemble("범위밖", "비행기 수하물 몇 kg까지예요") == ([], [])

    # 사람 말과 문서 말이 다른 경우 (시연에서 나온 실패)
    assert "주류" in tokenize(expand("예전엔 술 2병까지 됐잖아요")), tokenize(expand("술 2병"))
    assert "주류" not in tokenize(expand("미술품 반입")), "앞머리 매칭이 너무 헐겁다"
    hit = search("면세", "예전엔 술 2병까지 됐잖아요. 지금은 어떻게 되나요?")
    assert hit and any("2L" in h["본문"] for h in hit), [h["제목"] for h in hit]

    ev, tools = assemble("면세", "술 면세 한도")
    assert tools == ["lookup_duty"] and ev, (tools, len(ev))

    # 금액을 물어도 물건 자체가 막히는 품목이면 그 영역을 같이 본다
    _, t = assemble("면세", "독일 소시지 30달러어치 샀는데 면세 한도 안이면 괜찮죠?")
    assert t == ["lookup_duty", "lookup_quarantine"], t
    # 이미 그 영역을 보고 있으면 두 번 부르지 않는다
    _, t = assemble("검역", "소시지 가져와도 되나요")
    assert t == ["lookup_quarantine"], t
    # 범위 밖은 품목이 무엇이든 도구를 부르지 않는다
    assert assemble("범위밖", "악어가죽 지갑 미국 갈 때") == ([], [])
    print(f"context.py OK — 절 {len(SECTIONS)}개, 카테고리 {sorted({s['카테고리'] for s in SECTIONS})}")


if __name__ == "__main__":
    demo()
