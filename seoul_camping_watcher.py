#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
서울시 공공서비스예약 - 캠핑존 '토요일' 빈자리(취소분 포함) 감시 + 텔레그램 알림
=================================================================================

지속 가능한 방식 (매달 바뀌는 고유번호를 하드코딩하지 않음):
  1) 캠핑장 목록 페이지들을 넘기며 모든 서비스 카드를 수집
  2) 제목에 키워드("캠핑존")가 들어간 것만 골라냄
       → 난지 일반캠핑존 A~D형, 프리캠핑존 등이 잡히고
         캠프파이어존/타지역 캠핑장 등은 자연스럽게 제외됨
  3) 각 카드에서 고유번호(rsv_svc_id)를 자동 추출
  4) 그 예약 페이지로 직접 들어가 팝업을 닫고, 달력의
       '예약자수/정원'(예: 35/36) 숫자를 읽음  -> 앞<뒤 면 빈자리
  5) '토요일'에 빈자리가 있으면 텔레그램으로 알림 (이미 보낸 날짜는 생략)

이 스크립트는 실제 브라우저(Playwright)를 띄워 사람이 보는 화면 그대로를
읽기 때문에, 자바스크립트로 그려지는 달력도 정상적으로 읽힙니다.

※ 처음 1회는 '--debug' 로 돌려, 달력을 제대로 읽는지 로그로 확인하세요.
   달력 구조가 예상과 다르면 로그에 찍힌 HTML을 보고 셀렉터만 맞추면 됩니다.
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
SEARCH_KEYWORD = "캠핑존"          # 제목에 이 단어가 들어간 서비스만 감시
CHECK_INTERVAL_MIN = 10            # (로컬 반복 모드 전용) 확인 주기
HEADLESS = True
NOTIFY_WEEKDAYS = {5}              # 5=토요일 (월=0 ... 일=6)
MAX_LIST_PAGES = 10                # 목록 페이지 넘기기 최대 횟수(안전장치)

TELEGRAM_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "여기에_봇_토큰")
TELEGRAM_CHAT_ID = os.environ.get("TG_CHAT_ID", "여기에_본인_chat_id")

LIST_URL = "https://yeyak.seoul.go.kr/web/search/selectPageListDetailSearchImg.do?code=T500&dCode=T502"
RESERV_URL = "https://yeyak.seoul.go.kr/web/reservation/selectReservView.do?rsv_svc_id={sid}"

SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camping_seen.json")

# ============================================================
# 2. 셀렉터 (사이트 구조가 바뀌면 여기만 손보면 됨)
# ============================================================
RESULT_CARD_SELECTORS = ["#list_data li", ".result_list li", ".search_result li", "ul li"]
NEXT_PAGE_SELECTORS = ["a.next", "a:has-text('다음')", ".paging a.next"]
POPUP_CLOSE_SELECTORS = [
    "button:has-text('팝업닫기')", "a:has-text('팝업닫기')",
    "button:has-text('닫기')", "a:has-text('닫기')",
    ".layer_popup .btn_close", ".pop_close", ".modal .close", "button[title='닫기']",
]
# 달력 한 칸 후보. 첫 시도가 비면 다음 것을 시도.
CALENDAR_DAY_SELECTORS = ["table.calendar td", ".calendar td", ".cal_wrap td", "td"]

SVC_ID_RE = re.compile(r"S\d{15,}")          # 예: S260428171250440855
FRACTION_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

# ============================================================
# 3. 텔레그램
# ============================================================
def send_telegram(text: str) -> None:
    if "여기에" in TELEGRAM_BOT_TOKEN or "여기에" in TELEGRAM_CHAT_ID:
        print("⚠️  텔레그램 미설정 → 알림 생략. 전송 예정 내용:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as r:
            r.read()
        print("📨  텔레그램 알림 전송 완료")
    except Exception as e:
        print(f"❌  텔레그램 전송 실패: {e}")

# ============================================================
# 4. 중복 알림 방지
# ============================================================
def load_seen() -> set:
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
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
# 5. 공통 유틸
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
    for _ in range(5):
        el, sel = await first_existing(page, POPUP_CLOSE_SELECTORS)
        if not el:
            break
        try:
            await el.click(timeout=1500)
            if debug:
                print(f"     팝업 닫음: {sel}")
            await page.wait_for_timeout(300)
        except Exception:
            break

def year_for_month(month: int, now: dt.date) -> int:
    """제목의 월 숫자로 연도 추정 (지난 달이면 내년으로 간주)."""
    return now.year if month >= now.month else now.year + 1

# ============================================================
# 6. 목록 페이지들에서 (제목, 고유번호) 수집
# ============================================================
async def collect_services(page, debug=False):
    print("   캠핑장 목록 수집 중...")
    await page.goto(LIST_URL, wait_until="networkidle", timeout=60000)
    await close_popups(page, debug)

    found = {}   # svc_id -> title
    seen_titles_dbg = []

    for pageno in range(1, MAX_LIST_PAGES + 1):
        cards = []
        for sel in RESULT_CARD_SELECTORS:
            cards = await page.query_selector_all(sel)
            if len(cards) >= 3:
                break
        for c in cards:
            try:
                html = await c.evaluate("(el) => el.outerHTML")
            except Exception:
                continue
            title = ""
            # 카드 제목: title 속성 또는 제목 텍스트
            mt = re.search(r'title="([^"]+)"', html)
            if mt:
                title = mt.group(1)
            else:
                try:
                    title = (await c.inner_text()).strip().replace("\n", " ")
                except Exception:
                    title = ""
            sid_m = SVC_ID_RE.search(html)
            if not sid_m:
                continue
            sid = sid_m.group(0)
            if title:
                seen_titles_dbg.append(title[:50])
            if SEARCH_KEYWORD in title:
                found[sid] = title.strip()[:60]

        if debug:
            print(f"     [목록 {pageno}p] 카드 {len(cards)}개")

        # 다음 페이지로
        nxt, _ = await first_existing(page, NEXT_PAGE_SELECTORS)
        if not nxt:
            break
        try:
            await nxt.click(timeout=2000)
            await page.wait_for_load_state("networkidle", timeout=20000)
            await page.wait_for_timeout(500)
        except Exception:
            break

    if debug:
        print("     [목록에서 본 제목 일부] " + " | ".join(seen_titles_dbg[:20]))
    print(f"   '{SEARCH_KEYWORD}' 포함 서비스 {len(found)}건 발견")
    return found  # {svc_id: title}

# ============================================================
# 7. 예약 페이지에서 토요일 빈자리 읽기
# ============================================================
CELL_JS = """
(cell) => ({
  text: (cell.innerText || '').trim(),
  cls: (cell.className || '').toLowerCase(),
  title: cell.getAttribute('title') || '',
  data: JSON.stringify(cell.dataset || {}),
  html: cell.outerHTML.slice(0, 200)
})
"""

async def scan_reservation(page, sid, title, debug=False):
    await page.goto(RESERV_URL.format(sid=sid), wait_until="networkidle", timeout=60000)
    await close_popups(page, debug)
    await page.wait_for_timeout(1200)  # 달력 AJAX 로딩 대기
    await close_popups(page, debug)

    # 제목에서 월 추출 → 연/월 결정
    mm = re.search(r"(\d{1,2})\s*월", title)
    today = dt.date.today()
    month = int(mm.group(1)) if mm else (today.month % 12 + 1)
    year = year_for_month(month, today)

    cells, used = [], None
    for sel in CALENDAR_DAY_SELECTORS:
        c = await page.query_selector_all(sel)
        if c:
            cells, used = c, sel
            # td 전체로 너무 많으면 그대로 두되 표시
            break
    if debug:
        print(f"     달력 후보 셀 {len(cells)}개 (selector: {used}), 기준={year}-{month:02d}")

    dumped = 0
    hits = []
    for cell in cells:
        try:
            info = await cell.evaluate(CELL_JS)
        except Exception:
            continue
        blob = " ".join([info["text"], info["title"], info["data"]])
        frac = FRACTION_RE.search(blob)
        # 날짜 숫자(분수 제거 후 첫 1~2자리)
        text_wo = blob
        if frac:
            text_wo = blob[:frac.start()] + " " + blob[frac.end():]
        dm = re.search(r"\b(\d{1,2})\b", text_wo)
        day = int(dm.group(1)) if dm else None
        if not day or not (1 <= day <= 31):
            continue
        try:
            date = dt.date(year, month, day)
        except ValueError:
            continue
        # 디버그: 분수가 보이는 칸 몇 개를 HTML째 출력 (셀렉터 보정용)
        if debug and frac and dumped < 4:
            print(f"        예시칸 day={day} '{info['text']}' -> {info['html']}")
            dumped += 1
        if date.weekday() not in NOTIFY_WEEKDAYS:
            continue
        if frac:
            reserved, total = int(frac.group(1)), int(frac.group(2))
            if reserved < total:
                hits.append((date, reserved, total))
                if debug:
                    print(f"     ✅ {date} 빈자리 {reserved}/{total}")
            elif debug:
                print(f"     ⬜ {date} 마감 {reserved}/{total}")
    return hits

# ============================================================
# 8. 메인
# ============================================================
async def run_once(debug=False):
    seen = load_seen()
    now = dt.datetime.now()
    print(f"[{now:%Y-%m-%d %H:%M}] 점검 시작")

    new_lines = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        ctx = await browser.new_context(locale="ko-KR")
        page = await ctx.new_page()

        services = await collect_services(page, debug)
        for sid, title in services.items():
            print(f"   → '{title}' 확인")
            try:
                hits = await scan_reservation(page, sid, title, debug)
            except Exception as e:
                print(f"      오류(건너뜀): {e}")
                continue
            for date, reserved, total in hits:
                key = f"{sid}|{date.isoformat()}"
                if key not in seen:
                    seen.add(key)
                    new_lines.append(f"• {title}\n   {date:%m월 %d일}(토) {reserved}/{total}")

        await browser.close()

    if new_lines:
        msg = ("🏕️ 캠핑존 토요일 빈자리 발견!\n\n" + "\n".join(new_lines)
               + "\n\n예약 👉 " + LIST_URL)
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
        print(f"   {CHECK_INTERVAL_MIN}분 후 재확인...\n")
        await asyncio.sleep(CHECK_INTERVAL_MIN * 60)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="서울 캠핑존 토요일 빈자리 감시기")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--once", action="store_true", help="한 번만 실행(클라우드용)")
    args = ap.parse_args()
    try:
        asyncio.run(main_loop(debug=args.debug, once=args.once))
    except KeyboardInterrupt:
        print("\n종료합니다.")
