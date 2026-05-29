#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
서울시 공공서비스예약 - 캠핑존 '토요일' 빈자리 감시 + 텔레그램 알림
=================================================================

동작 흐름:
  1) 캠핑장 검색 페이지 접속
  2) "캠핑존" (기본값) 으로 검색
  3) 검색 결과 카드들에 하나씩 진입
  4) 뜨는 팝업창 닫기
  5) 좌측 상단 달력에서 날짜별 '예약자수/전체정원' 숫자를 읽음
       - 예) '35/36' -> 1자리 남음 (빈자리 있음)
       -    '36/36' -> 꽉 참     (빈자리 없음)
       (숫자를 못 읽으면 색상/클래스로 보조 판정)
  6) '토요일'에 빈자리가 있으면 텔레그램으로 푸쉬 알림
  7) GitHub Actions 가 정해진 간격마다 이 스크립트를 --once 로 실행

GitHub Actions(클라우드)에서 돌리는 방법은 함께 드린
'.github/workflows/camping.yml' 과 안내문을 참고하세요.
로컬에서 계속 돌리고 싶다면 옵션 없이 실행하면 반복 모드가 됩니다.
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import urllib.parse
import urllib.request

from playwright.async_api import async_playwright

# ============================================================
# 1. 기본 설정
# ============================================================

SEARCH_KEYWORD = "캠핑존"          # 검색어. "6월 캠핑존" 등으로 바꿔도 됩니다.
CHECK_INTERVAL_MIN = 10            # (로컬 반복 모드에서만 사용) 몇 분마다 확인할지
HEADLESS = True                    # 클라우드/백그라운드에서는 반드시 True
NOTIFY_WEEKDAYS = {5}              # 알림 받을 요일. 5=토요일 (월=0 ... 일=6)

# --- 텔레그램 설정: 값은 환경변수(또는 GitHub Secrets)로 넣는 것을 권장 ---
TELEGRAM_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "여기에_봇_토큰")
TELEGRAM_CHAT_ID = os.environ.get("TG_CHAT_ID", "여기에_본인_chat_id")

LIST_URL = "https://yeyak.seoul.go.kr/web/search/selectPageListDetailSearchImg.do?code=T500&dCode=T502"

# 이미 알림 보낸 (시설+날짜) 를 기록해 중복 알림을 막는 파일
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camping_seen.json")


# ============================================================
# 2. 셀렉터 설정  ──  사이트 구조가 바뀌면 이 부분만 손보면 됩니다
# ============================================================
SEARCH_INPUT_SELECTORS = [
    "input#searchKeyword", "input[name='searchKeyword']",
    "input[placeholder*='검색']", "input[type='search']",
]
SEARCH_BUTTON_SELECTORS = [
    "button:has-text('검색')", "a:has-text('검색')", ".btn_search",
]
RESULT_LINK_SELECTORS = [
    "a:has-text('상세보기')", ".search_result li a", ".list_type li a",
]
POPUP_CLOSE_SELECTORS = [
    "button:has-text('닫기')", "a:has-text('닫기')",
    ".layer_popup .btn_close", ".pop_close", ".modal .close", "button[title='닫기']",
]
CALENDAR_DAY_SELECTORS = [
    "table.calendar td", ".calendar td", ".cal_wrap td", "td.day",
]

# 숫자를 못 읽었을 때만 쓰는 '보조' 색상/클래스 판정 기준
UNAVAILABLE_CLASS_HINTS = ["disabled", "off", "gray", "grey", "soldout", "end", "close"]
AVAILABLE_CLASS_HINTS = ["able", "on", "possible", "open", "available"]


# ============================================================
# 3. 알림 보내기
# ============================================================
def send_telegram(text: str) -> None:
    if "여기에" in TELEGRAM_BOT_TOKEN or "여기에" in TELEGRAM_CHAT_ID:
        print("⚠️  텔레그램 토큰/chat_id 미설정 → 알림 생략. 전송될 내용:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID, "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as resp:
            resp.read()
        print("📨  텔레그램 알림 전송 완료")
    except Exception as e:
        print(f"❌  텔레그램 전송 실패: {e}")


# ============================================================
# 4. 중복 알림 방지 기록
# ============================================================
def load_seen() -> set:
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: set) -> None:
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️  기록 저장 실패: {e}")


# ============================================================
# 5. 달력 한 칸 추출/판정
# ============================================================
# 각 td 의 텍스트/클래스/배경색/클릭가능 여부를 통째로 가져온다
CALENDAR_EXTRACT_JS = """
(cell) => {
  const cls = (cell.className || '').toLowerCase();
  const text = (cell.innerText || '').trim();
  const bg = window.getComputedStyle(cell).backgroundColor || '';
  const clickable = !!cell.querySelector('a, button');
  return { cls, text, bg, clickable };
}
"""

FRACTION_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def parse_cell(info: dict):
    """칸 텍스트에서 (날짜, 예약자수, 정원) 추출. 못 찾으면 None 포함."""
    text = info.get("text", "")
    frac = FRACTION_RE.search(text)
    reserved = total = None
    if frac:
        reserved, total = int(frac.group(1)), int(frac.group(2))
        # 날짜 숫자가 분수와 섞이지 않게, 분수 부분을 지우고 날짜를 찾는다
        text_wo = text[:frac.start()] + " " + text[frac.end():]
    else:
        text_wo = text
    dm = re.search(r"\b(\d{1,2})\b", text_wo)
    day = int(dm.group(1)) if dm else None
    return day, reserved, total


def is_available(info: dict, reserved, total) -> bool:
    """1순위: 예약자수<정원 이면 빈자리. 숫자가 없으면 색상/클래스 보조 판정."""
    if reserved is not None and total is not None:
        return reserved < total
    cls = info.get("cls", "")
    if any(h in cls for h in UNAVAILABLE_CLASS_HINTS):
        return False
    if any(h in cls for h in AVAILABLE_CLASS_HINTS):
        return True
    bg = info.get("bg", "")
    nums = [int(n) for n in re.findall(r"\d+", bg)][:3]
    whitish = len(nums) == 3 and all(v >= 230 for v in nums)
    return bool(info.get("clickable")) and whitish


# ============================================================
# 6. 페이지 조작
# ============================================================
async def first_existing(scope, selectors):
    for sel in selectors:
        try:
            el = await scope.query_selector(sel)
            if el:
                return el, sel
        except Exception:
            pass
    return None, None


async def close_popups(page, debug=False):
    for _ in range(4):
        el, sel = await first_existing(page, POPUP_CLOSE_SELECTORS)
        if not el:
            break
        try:
            await el.click(timeout=2000)
            if debug:
                print(f"   팝업 닫음: {sel}")
            await page.wait_for_timeout(400)
        except Exception:
            break


async def scan_facility(page, year, month, debug=False):
    """현재 상세 페이지 달력에서 '토요일 빈자리' 날짜 리스트 반환."""
    await close_popups(page, debug)

    cells, used_sel = [], None
    for sel in CALENDAR_DAY_SELECTORS:
        found = await page.query_selector_all(sel)
        if found:
            cells, used_sel = found, sel
            break
    if debug:
        print(f"   달력 셀 {len(cells)}개 발견 (selector: {used_sel})")

    weekend_open = []
    for cell in cells:
        try:
            info = await cell.evaluate(CALENDAR_EXTRACT_JS)
        except Exception:
            continue
        day, reserved, total = parse_cell(info)
        if not day or day < 1 or day > 31:
            continue
        try:
            date = dt.date(year, month, day)
        except ValueError:
            continue
        if date.weekday() not in NOTIFY_WEEKDAYS:
            continue
        if is_available(info, reserved, total):
            weekend_open.append((date, reserved, total))
            if debug:
                cap = f"{reserved}/{total}" if total is not None else "색상판정"
                print(f"   ✅ {date} 빈자리 ({cap})")
        elif debug:
            cap = f"{reserved}/{total}" if total is not None else f"cls='{info['cls']}'"
            print(f"   ⬜ {date} 마감 ({cap})")
    return weekend_open


# ============================================================
# 7. 메인
# ============================================================
async def run_once(debug=False):
    seen = load_seen()
    now = dt.datetime.now()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        ctx = await browser.new_context(locale="ko-KR")
        page = await ctx.new_page()

        print(f"[{now:%Y-%m-%d %H:%M}] 캠핑존 목록 접속...")
        await page.goto(LIST_URL, wait_until="networkidle", timeout=60000)
        await close_popups(page, debug)

        inp, _ = await first_existing(page, SEARCH_INPUT_SELECTORS)
        if inp:
            await inp.fill(SEARCH_KEYWORD)
            btn, _ = await first_existing(page, SEARCH_BUTTON_SELECTORS)
            if btn:
                await btn.click()
            else:
                await inp.press("Enter")
            await page.wait_for_load_state("networkidle", timeout=30000)
            if debug:
                print(f"   '{SEARCH_KEYWORD}' 검색 완료")
        elif debug:
            print("   검색창 못 찾음 → 전체 목록 사용")

        links = []
        for sel in RESULT_LINK_SELECTORS:
            found = await page.query_selector_all(sel)
            if found:
                links = found
                break
        result_count = len(links)
        print(f"   검색 결과 {result_count}건")

        all_hits = []
        for i in range(result_count):
            try:
                cur = []
                for sel in RESULT_LINK_SELECTORS:
                    found = await page.query_selector_all(sel)
                    if found:
                        cur = found
                        break
                if i >= len(cur):
                    break
                link = cur[i]
                name = (await link.get_attribute("title")) or (await link.inner_text()) or f"시설{i+1}"
                name = name.strip()[:40]
                if SEARCH_KEYWORD not in name and SEARCH_KEYWORD != "캠핑존":
                    continue

                print(f"   → ({i+1}/{result_count}) '{name}' 진입")
                await link.click()
                await page.wait_for_load_state("networkidle", timeout=30000)

                target = (now.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
                hits = await scan_facility(page, target.year, target.month, debug)
                if hits:
                    all_hits.append((name, hits))

                await page.go_back(wait_until="networkidle", timeout=30000)
                await close_popups(page, debug)
            except Exception as e:
                print(f"      처리 중 오류(건너뜀): {e}")
                try:
                    await page.goto(LIST_URL, wait_until="networkidle", timeout=30000)
                    await close_popups(page, debug)
                except Exception:
                    pass

        await browser.close()

    new_lines = []
    for name, hits in all_hits:
        for date, reserved, total in hits:
            key = f"{name}|{date.isoformat()}"
            if key not in seen:
                seen.add(key)
                cap = f" ({reserved}/{total})" if total is not None else ""
                new_lines.append(f"• {name}  →  {date:%m월 %d일}(토){cap}")

    if new_lines:
        msg = ("🏕️ 캠핑존 토요일 빈자리 발견!\n\n"
               + "\n".join(new_lines)
               + f"\n\n바로 예약 👉 {LIST_URL}")
        send_telegram(msg)
    else:
        print("   새로운 토요일 빈자리 없음")

    save_seen(seen)


async def main_loop(debug=False, once=False):
    while True:
        try:
            await run_once(debug=debug)
        except Exception as e:
            print(f"❌ 실행 오류: {e}")
        if once:
            break
        print(f"   {CHECK_INTERVAL_MIN}분 후 다시 확인...\n")
        await asyncio.sleep(CHECK_INTERVAL_MIN * 60)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="서울 캠핑존 토요일 빈자리 감시기")
    ap.add_argument("--debug", action="store_true", help="무엇을 읽는지 상세 출력")
    ap.add_argument("--once", action="store_true", help="한 번만 확인하고 종료 (클라우드용)")
    args = ap.parse_args()
    try:
        asyncio.run(main_loop(debug=args.debug, once=args.once))
    except KeyboardInterrupt:
        print("\n종료합니다.")
