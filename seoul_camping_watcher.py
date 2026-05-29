#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
서울시 공공서비스예약 - 캠핑존 '토요일' 빈자리(취소분 포함) 감시 + 텔레그램 알림
=================================================================================
이 사이트는 검색/페이지넘김을 모두 내부 자바스크립트(POST)로 처리하므로,
실제 브라우저(Playwright)로 페이지를 띄운 뒤:
  1) 목록 페이지를 넘기며 페이지 전체 HTML에서 고유번호(S+숫자)를 정규식으로 수집
  2) 각 고유번호의 예약 페이지로 들어가 제목을 읽고, 제목에 '캠핑존'이 있으면
  3) 달력의 '예약자수/정원'(예 35/36)을 읽어 토요일 빈자리를 찾음
  4) 새 빈자리가 있으면 텔레그램 알림 (이미 보낸 것은 생략)

★ 처음엔 반드시 --debug 로 실행하세요. 아래 [진단] 줄들을 보고
  사이트가 러너에서 제대로 열리는지 / 번호가 잡히는지 확인합니다.
"""

import argparse, asyncio, datetime as dt, json, os, re, urllib.parse, urllib.request
from playwright.async_api import async_playwright

# ── 설정 ───────────────────────────────────────────────
SEARCH_KEYWORD = "캠핑존"
CHECK_INTERVAL_MIN = 10
HEADLESS = True
NOTIFY_WEEKDAYS = {5}          # 5=토
MAX_LIST_PAGES = 12

TELEGRAM_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "여기에_봇_토큰")
TELEGRAM_CHAT_ID = os.environ.get("TG_CHAT_ID", "여기에_본인_chat_id")

LIST_URL = "https://yeyak.seoul.go.kr/web/search/selectPageListDetailSearchImg.do?code=T500&dCode=T502"
RESERV_URL = "https://yeyak.seoul.go.kr/web/reservation/selectReservView.do?rsv_svc_id={sid}"
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camping_seen.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

POPUP_CLOSE_SELECTORS = [
    "button:has-text('팝업닫기')", "a:has-text('팝업닫기')",
    "button:has-text('닫기')", "a:has-text('닫기')",
    ".layer_popup .btn_close", ".pop_close", ".modal .close", "button[title='닫기']",
]
CALENDAR_DAY_SELECTORS = ["table.calendar td", ".calendar td", ".cal_wrap td", "td"]

SVC_ID_RE = re.compile(r"S\d{15,}")
FRACTION_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

# ── 텔레그램 ───────────────────────────────────────────
def send_telegram(text):
    if "여기에" in TELEGRAM_BOT_TOKEN or "여기에" in TELEGRAM_CHAT_ID:
        print("⚠️  텔레그램 미설정 → 전송 예정:\n" + text); return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as r: r.read()
        print("📨  알림 전송 완료")
    except Exception as e:
        print(f"❌  전송 실패: {e}")

# ── 기록 ───────────────────────────────────────────────
def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f: return set(json.load(f))
    except Exception: return set()

def save_seen(seen):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f, ensure_ascii=False, indent=2)
    except Exception as e: print(f"⚠️  기록 저장 실패: {e}")

# ── 공통 ───────────────────────────────────────────────
async def first_existing(scope, selectors):
    for sel in selectors:
        try:
            el = await scope.query_selector(sel)
            if el: return el, sel
        except Exception: pass
    return None, None

async def close_popups(page, debug=False):
    for _ in range(5):
        el, sel = await first_existing(page, POPUP_CLOSE_SELECTORS)
        if not el: break
        try:
            await el.click(timeout=1500)
            if debug: print(f"     팝업 닫음: {sel}")
            await page.wait_for_timeout(300)
        except Exception: break

def year_for_month(month, today):
    return today.year if month >= today.month else today.year + 1

# ── 목록에서 고유번호 수집 ──────────────────────────────
async def collect_ids(page, debug=False):
    print("   목록 페이지 수집 중...")
    await page.goto(LIST_URL, wait_until="networkidle", timeout=60000)
    await close_popups(page, debug)
    # 결과가 채워질 때까지(고유번호가 본문에 나타날 때까지) 최대 15초 대기
    try:
        await page.wait_for_function(
            "() => /S\\d{15,}/.test(document.body.innerText + document.body.innerHTML)",
            timeout=15000)
    except Exception:
        pass

    if debug:
        try:
            content0 = await page.content()
            print(f"     [진단] 최종URL: {page.url}")
            print(f"     [진단] 제목: {await page.title()}")
            print(f"     [진단] 본문 HTML 길이: {len(content0)}")
            print(f"     [진단] 첫 고유번호들: {SVC_ID_RE.findall(content0)[:5]}")
        except Exception as e:
            print(f"     [진단] 페이지 상태 읽기 실패: {e}")

    ids = set()
    for pageno in range(1, MAX_LIST_PAGES + 1):
        try:
            content = await page.content()
        except Exception:
            break
        page_ids = set(SVC_ID_RE.findall(content))
        ids |= page_ids
        if debug:
            print(f"     [목록 {pageno}p] 번호 {len(page_ids)}개 (누적 {len(ids)})")
        # 다음 페이지 숫자 링크 클릭
        nextnum = pageno + 1
        clicked = False
        for getter in (
            lambda: page.get_by_title(f"{nextnum}페이지로"),
            lambda: page.get_by_role("link", name=str(nextnum), exact=True),
            lambda: page.get_by_title("다음 5페이지로 이동"),
        ):
            try:
                loc = getter()
                if await loc.count() > 0:
                    await loc.first.click(timeout=2500)
                    await page.wait_for_timeout(900)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            if debug: print(f"     다음 페이지 링크 없음 → 종료")
            break
    print(f"   고유번호 총 {len(ids)}개 수집")
    return ids

# ── 예약 페이지에서 토요일 빈자리 ──────────────────────
async def get_title(page):
    for sel in ["h3", "h4", ".tit", ".view_tit", "title"]:
        el = await page.query_selector(sel)
        if el:
            t = (await el.inner_text()).strip()
            if t and ("캠핑" in t or "월" in t):
                return t
    return (await page.title()) or ""

CELL_JS = """(c)=>({text:(c.innerText||'').trim(),title:c.getAttribute('title')||'',
data:JSON.stringify(c.dataset||{}),html:c.outerHTML.slice(0,220)})"""

async def scan_reservation(page, sid, debug=False):
    await page.goto(RESERV_URL.format(sid=sid), wait_until="networkidle", timeout=60000)
    await close_popups(page, debug)
    title = await get_title(page)
    if SEARCH_KEYWORD not in title:
        if debug: print(f"     (건너뜀: 제목에 '{SEARCH_KEYWORD}' 없음) {title[:40]}")
        return title, []
    print(f"   → '{title[:45]}' 달력 확인")
    # 달력(분수 표기)이 그려질 때까지 대기
    try:
        await page.wait_for_function(
            "() => /\\d+\\s*\\/\\s*\\d+/.test(document.body.innerText)", timeout=8000)
    except Exception:
        pass
    await close_popups(page, debug)

    mm = re.search(r"(\d{1,2})\s*월", title)
    today = dt.date.today()
    month = int(mm.group(1)) if mm else (today.month % 12 + 1)
    year = year_for_month(month, today)

    cells = []
    for sel in CALENDAR_DAY_SELECTORS:
        cells = await page.query_selector_all(sel)
        if cells: break
    if debug: print(f"     달력 후보 셀 {len(cells)}개, 기준={year}-{month:02d}")

    hits, dumped = [], 0
    for cell in cells:
        try: info = await cell.evaluate(CELL_JS)
        except Exception: continue
        blob = " ".join([info["text"], info["title"], info["data"]])
        frac = FRACTION_RE.search(blob)
        text_wo = (blob[:frac.start()] + " " + blob[frac.end():]) if frac else blob
        dm = re.search(r"\b(\d{1,2})\b", text_wo)
        day = int(dm.group(1)) if dm else None
        if not day or not (1 <= day <= 31): continue
        if debug and frac and dumped < 4:
            print(f"        예시칸 day={day} '{info['text']}' html={info['html']}")
            dumped += 1
        try: date = dt.date(year, month, day)
        except ValueError: continue
        if date.weekday() not in NOTIFY_WEEKDAYS: continue
        if frac:
            res, tot = int(frac.group(1)), int(frac.group(2))
            if res < tot:
                hits.append((date, res, tot))
                if debug: print(f"     ✅ {date} 빈자리 {res}/{tot}")
            elif debug: print(f"     ⬜ {date} 마감 {res}/{tot}")
    return title, hits

# ── 메인 ───────────────────────────────────────────────
async def run_once(debug=False):
    seen = load_seen(); now = dt.datetime.now()
    print(f"[{now:%Y-%m-%d %H:%M}] 점검 시작")
    new_lines = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        ctx = await browser.new_context(locale="ko-KR", user_agent=UA,
                                        viewport={"width": 1280, "height": 1800})
        page = await ctx.new_page()
        ids = await collect_ids(page, debug)
        for sid in ids:
            try:
                title, hits = await scan_reservation(page, sid, debug)
            except Exception as e:
                print(f"      오류(건너뜀) {sid}: {e}"); continue
            for date, res, tot in hits:
                key = f"{sid}|{date.isoformat()}"
                if key not in seen:
                    seen.add(key)
                    new_lines.append(f"• {title}\n   {date:%m월 %d일}(토) {res}/{tot}")
        await browser.close()
    if new_lines:
        send_telegram("🏕️ 캠핑존 토요일 빈자리 발견!\n\n" + "\n".join(new_lines)
                      + "\n\n예약 👉 " + LIST_URL)
    else:
        print("   새로운 토요일 빈자리 없음")
    save_seen(seen)

async def main_loop(debug=False, once=False):
    while True:
        try: await run_once(debug=debug)
        except Exception as e: print(f"❌ 실행 오류: {e}")
        if once: break
        print(f"   {CHECK_INTERVAL_MIN}분 후 재확인...\n")
        await asyncio.sleep(CHECK_INTERVAL_MIN * 60)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    try: asyncio.run(main_loop(debug=args.debug, once=args.once))
    except KeyboardInterrupt: print("\n종료합니다.")
