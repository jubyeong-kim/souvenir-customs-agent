"""데모 화면을 잡아 docs/demo.png 로 저장한다. REPORT 6번에 쓴다.

    streamlit run app.py        # 다른 터미널에서 먼저 띄운다
    python capture_demo.py

설치된 Edge 를 쓴다(`channel="msedge"`) — 브라우저를 따로 내려받지 않는다.
되묻고 이어서 판정하는 **두 턴**을 잡는다. 한 턴만 잡으면 되묻기가 고장처럼 보인다.
"""
import pathlib

from playwright.sync_api import sync_playwright

URL = "http://localhost:8501"
OUT = pathlib.Path(__file__).parent / "docs" / "demo.png"

TURN1 = "가방"                              # 예시 버튼 이름표의 일부
TURN2 = "악어가죽 가방이고 500달러예요."


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1180, "height": 1400},
                                device_scale_factor=2)
        page.goto(URL, wait_until="networkidle")

        page.get_by_role("button").filter(has_text=TURN1).first.click()
        page.get_by_text("말씀만으로는 판단이 어렵습니다.").wait_for(timeout=90_000)

        page.get_by_placeholder("문의 내용", exact=False).fill(TURN2)
        page.keyboard.press("Enter")
        # 「CITES」 로 기다리면 사이드바 문구에 먼저 걸려 로딩 중에 찍힌다.
        # 스피너가 사라지는 것을 기다린다.
        spinner = page.get_by_text("근거를 찾는 중")
        spinner.first.wait_for(timeout=30_000)
        spinner.first.wait_for(state="detached", timeout=120_000)
        page.wait_for_timeout(3_000)          # 마지막 칸이 그려질 시간

        OUT.parent.mkdir(exist_ok=True)
        page.screenshot(path=str(OUT), full_page=True)
        browser.close()
    print(f"저장: {OUT}  ({OUT.stat().st_size // 1024}KB)")


if __name__ == "__main__":
    main()
