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


def fetch(src: dict) -> str:
    r = requests.get(src["url"], headers=UA, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    text = trafilatura.extract(r.text, include_tables=True, favor_recall=True)
    if not text or len(text) < 500:
        raise RuntimeError(f"본문이 너무 짧다 ({len(text or '')}자). "
                           "페이지가 자바스크립트로 그려지거나 차단된 것이다.")
    body = sectionize(clean(text))
    head = (f"# {src['제목']}\n\n"
            f"- 출처: {src['url']}\n"
            f"- 소관기관: {src['소관기관']}\n"
            f"- 받은 날짜: {date.today()}\n"
            f"- 갱신주기: {src['갱신주기']}\n\n"
            f"> 이 문서는 fetch_docs.py 가 위 출처에서 그대로 받아온 것이다. 손으로 고치지 않는다.\n\n"
            f"---\n\n")
    return head + body


def main(only: str | None = None) -> int:
    DOCS.mkdir(exist_ok=True)
    sources = yaml.safe_load(SOURCES.read_text(encoding="utf-8"))
    failed = 0
    for src in sources:
        if only and only not in (src["카테고리"], src["파일"]):
            continue
        path = DOCS / src["파일"]
        try:
            path.write_text(fetch(src), encoding="utf-8")
            print(f"  OK  {src['파일']:<18} {len(path.read_text(encoding='utf-8')):>6}자  {src['카테고리']}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {src['파일']:<18} {e}")
            print(f"       -> 같은 기관의 다른 정적 페이지로 sources.yaml 의 url 을 바꾼다. 기억으로 채우지 않는다.")
    return failed


if __name__ == "__main__":
    sys.exit(min(main(sys.argv[1] if len(sys.argv) > 1 else None), 1))
