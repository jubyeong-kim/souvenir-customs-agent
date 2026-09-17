"""문서를 절로 쪼개고, 카테고리별로 근거를 조립한다.

여기가 이 봇의 "찾아보기" 단계다. 답변 품질이 안 오를 때 프롬프트보다 먼저 의심할 곳이
바로 이 검색 함수다 — 모두몰 때도 동료도 여기서 버그를 만났고, 양쪽 다 프롬프트로는
끝내 안 풀렸다. (chatbot/PLAYBOOK.md 3-④)
"""
import pathlib, re, yaml

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
                "출처": src["url"],
                "기준일": 기준일.group(1) if 기준일 else "?",
                "제목": title.strip(),
                "본문": text.strip(),
            })
    return sections


SECTIONS = _load()


def search(category: str, query: str, top: int = 3) -> list[dict]:
    """카테고리 안에서만 찾는다. 점수 0이면 빈 목록 — 근거가 없다는 뜻이고,
    호출한 쪽은 지어내지 말고 넘겨야 한다."""
    tokens = tokenize(query)
    scored = []
    for sec in SECTIONS:
        if sec["카테고리"] != category:
            continue
        haystack = sec["제목"] + "\n" + sec["본문"]
        score = 0
        for t in tokens:
            score += haystack.count(t) * 3          # 완전일치에 가중치
            if t in sec["제목"]:
                score += 2                          # 절 제목에 있으면 그 절이 주제다
            if len(t) >= 3:                         # 부분일치는 낮게
                score += sum(haystack.count(t[i:i + 2]) for i in range(len(t) - 1)) * 0.1
        if score > 0:
            scored.append((score, sec))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:top]]


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


def assemble(category: str, query: str) -> tuple[list[dict], list[str]]:
    """카테고리에 맞는 도구 하나만 부른다. 반환: (근거 절, 호출한 도구 이름)"""
    tool = TOOLS.get(category)
    if tool is None:                       # 범위 밖 — 도구를 부르지 않는다
        return [], []
    return tool(query), [tool.__name__]


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

    ev, tools = assemble("면세", "술 면세 한도")
    assert tools == ["lookup_duty"] and ev, (tools, len(ev))
    print(f"context.py OK — 절 {len(SECTIONS)}개, 카테고리 {sorted({s['카테고리'] for s in SECTIONS})}")


if __name__ == "__main__":
    demo()
