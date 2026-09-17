"""sources.yaml 에 적힌 기관 페이지를 받아 docs/*.md 로 저장한다.

이 봇의 답변 근거는 전부 여기를 거쳐서 들어온다. 사람이 기억으로 문서를 쓰지 않는다 —
면세 한도나 검역 금지 품목은 바뀌고, 틀린 숫자로 만든 평가셋은 정답부터 틀린다.

    python fetch_docs.py          # 전부 다시 받는다
    python fetch_docs.py 검역      # 카테고리 하나만
"""
import re, sys, pathlib
from datetime import date

import requests, trafilatura, yaml

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
DOCS = pathlib.Path(__file__).parent / "docs"
SOURCES = pathlib.Path(__file__).parent / "sources.yaml"

# 본문에 섞여 들어오는 사이트 공통 문구. 근거로 쓰이면 안 된다.
NOISE = re.compile(r"만족하셨습니까|바로가기|메인메뉴|본문으로|페이지에서 제공하는")


def clean(text: str) -> list[str]:
    """추출 결과를 줄 단위로 훑어 잡음을 걷어낸다."""
    return [ln.rstrip() for ln in text.splitlines()
            if ln.strip() and not NOISE.search(ln)]


def sectionize(lines: list[str]) -> str:
    """`##` 절로 나눈다. 절이 근거 조립의 최소 단위이자 데모에 보여줄 단위다.

    관공서 페이지는 제목이 <h> 태그가 아니라 굵은 한 줄인 경우가 많아,
    목록(`-`)도 표(`|`)도 아닌 짧은 줄을 절 제목으로 본다.
    ponytail: 휴리스틱. 절이 이상하게 잘리면 여기만 손보면 된다.
    """
    MAX = 600          # 절 하나가 이보다 길면 쪼갠다. 근거로 통째로 보여주기엔 길다.
    out, buf, title = [], [], None

    def flush():
        """제목이 없는 관공서 페이지는 절이 통으로 나온다. 길면 줄 경계에서 자른다."""
        if not buf:
            return
        def emit(lines):
            # 제목이 통째로 같으면 절을 구분할 수 없다. 첫 줄 앞머리를 붙여 준다.
            gist = re.sub(r"^[-*|\s]+", "", lines[0])[:28]
            out.append(f"## {title or '개요'} — {gist}\n\n" + "\n".join(lines))

        chunk, size = [], 0
        for ln in buf:
            if size + len(ln) > MAX and chunk:
                emit(chunk)
                chunk, size = [], 0
            chunk.append(ln)
            size += len(ln)
        if chunk:
            emit(chunk)

    for ln in lines:
        is_head = (not ln.startswith(("-", "|", "*", "\t", " "))
                   and len(ln) <= 40 and not ln.endswith("."))
        if is_head:
            flush()
            buf, title = [], ln.strip()
        else:
            buf.append(ln)
    flush()
    return "\n\n".join(out)


TOC = re.compile(r"^\s*\d+\s*$")          # 목차 페이지는 쪽번호만 있는 줄이 많다


def fetch_pdf(src: dict) -> str:
    """상담 사례집 같은 PDF 에서 주제에 맞는 쪽만 뽑는다.

    전체를 다 넣지 않는 이유: 1,000쪽을 통째로 절로 쪼개면 검색이 엉뚱한 쪽을 물어 온다.
    ponytail: 키워드로 쪽을 고른다. 사례가 모자라면 `쪽키워드` 만 넓히면 된다.
    """
    import pymupdf                        # PDF 출처가 있을 때만 필요하다

    r = requests.get(src["url"], headers=UA, timeout=120)
    r.raise_for_status()
    # 이 사례집은 띄어쓰기 글리프가 한자로 추출된다. 두 글자뿐이라 그것만 되돌린다.
    # ponytail: 다른 PDF 를 넣었는데 글자가 깨지면 여기에 추가한다.
    SPACE_GLYPH = str.maketrans({"堺": " ", "埑": " "})
    kw = re.compile("|".join(src["쪽키워드"]))
    # 주제는 맞지만 **다른 제도**를 다루는 쪽을 뺀다. 이 봇은 여행자가 직접 들고 오는
    # 경우만 다루는데, 사례집에는 「해외에서 발송되는」 해외직구 기준(150달러·1병)이
    # 같은 "술 면세" 제목 아래 실려 있다. 근거에 들어오면 모델이 그쪽을 인용한다.
    # 실험기록 #7.
    skip = re.compile("|".join(src["쪽제외"])) if src.get("쪽제외") else None
    blocks = []
    with pymupdf.open(stream=r.content, filetype="pdf") as doc:
        for i, page in enumerate(doc):
            text = page.get_text().translate(SPACE_GLYPH).strip()
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if not kw.search(text) or len(lines) < 5:
                continue
            if skip and skip.search(text):
                continue               # 주제는 맞지만 다른 제도를 다루는 쪽
            if sum(bool(TOC.match(ln)) for ln in lines) > len(lines) / 3:
                continue                   # 목차·색인 쪽
            title = next((ln.strip() for ln in lines if kw.search(ln)), f"{i}쪽")
            blocks.append(f"## 상담사례 — {title[:40]}\n\n" + "\n".join(lines))
    if not blocks:
        raise RuntimeError("주제에 맞는 쪽을 못 찾았다. 쪽키워드를 넓힌다.")
    return "\n\n".join(blocks[: src.get("최대쪽", 12)])


def fetch(src: dict) -> str:
    r = requests.get(src["url"], headers=UA, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    text = trafilatura.extract(r.text, include_tables=True, favor_recall=True)
    if not text or len(text) < 500:
        raise RuntimeError(f"본문이 너무 짧다 ({len(text or '')}자). "
                           "페이지가 자바스크립트로 그려지거나 차단된 것이다.")
    return sectionize(clean(text))


def header(src: dict) -> str:
    return (f"# {src['제목']}\n\n"
            f"- 출처: {src['url']}\n"
            f"- 소관기관: {src['소관기관']}\n"
            f"- 받은 날짜: {date.today()}\n"
            f"- 갱신주기: {src['갱신주기']}\n\n"
            f"> 이 문서는 fetch_docs.py 가 위 출처에서 그대로 받아온 것이다. 손으로 고치지 않는다.\n\n"
            f"---\n\n")


def main(only: str | None = None) -> int:
    DOCS.mkdir(exist_ok=True)
    sources = yaml.safe_load(SOURCES.read_text(encoding="utf-8"))
    failed = 0
    for src in sources:
        if only and only not in (src["카테고리"], src["파일"]):
            continue
        path = DOCS / src["파일"]
        try:
            body = fetch_pdf(src) if src.get("종류") == "pdf" else fetch(src)
            path.write_text(header(src) + body, encoding="utf-8")
            print(f"  OK  {src['파일']:<18} {len(path.read_text(encoding='utf-8')):>6}자  {src['카테고리']}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {src['파일']:<18} {e}")
            print(f"       -> 같은 기관의 다른 정적 페이지로 sources.yaml 의 url 을 바꾼다. 기억으로 채우지 않는다.")
    return failed


if __name__ == "__main__":
    sys.exit(min(main(sys.argv[1] if len(sys.argv) > 1 else None), 1))
