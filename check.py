#!/usr/bin/env python3
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from curl_cffi import requests

KST = timezone(timedelta(hours=9))
BASE = "https://cgv.co.kr/api/v1/booking"
MOVIE_NAME = "극장판 치이카와-인어 섬의 비밀"
PLAY_DATE = "20260930"
SITES = [
    ("0013", "용산아이파크몰"),
    ("0010", "구로"),
    ("0059", "영등포타임스퀘어"),
    ("0056", "강남"),
    ("0191", "홍대"),
]
STATE_FILE = Path(__file__).with_name("state.json")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "60"))
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "340"))

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://cgv.co.kr/cnm/movieBook/cinema",
    "Origin": "https://cgv.co.kr",
}


def log(message):
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def normalize(value):
    return re.sub(r"[\s:·\-–—_]", "", str(value or "")).lower()


def load_state():
    if not STATE_FILE.exists():
        return {"notified": []}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data.setdefault("notified", [])
        return data
    except Exception:
        return {"notified": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_rows(session, site_no):
    response = session.get(
        f"{BASE}/searchMovScnInfo",
        params={"coCd": "A420", "siteNo": site_no, "scnYmd": PLAY_DATE, "rtctlScopCd": "08"},
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("statusCode") != 0:
        raise RuntimeError(f"CGV 오류: {body.get('statusMessage')}")
    wanted = normalize(MOVIE_NAME)
    return [row for row in (body.get("data") or []) if wanted in normalize(row.get("movNm"))]


def row_key(site_no, row):
    return ":".join(
        [site_no, str(row.get("movNo", "")), str(row.get("scnYmd", "")),
         str(row.get("scnsrtTm", "")), str(row.get("scnsNo", ""))]
    )


def fmt_time(value):
    value = str(value or "")
    return f"{value[:2]}:{value[2:]}" if len(value) == 4 else value


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("GitHub Secrets에 Telegram 토큰 또는 Chat ID가 없습니다.")
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": [[{
                "text": "🎟 CGV에서 예매하기",
                "url": "https://cgv.co.kr/cnm/movieBook/cinema",
            }]]},
        },
        timeout=20,
    )
    response.raise_for_status()


def check_once(session, state):
    notified = set(state["notified"])
    found = 0
    for site_no, site_name in SITES:
        try:
            rows = fetch_rows(session, site_no)
            new_rows = [row for row in rows if row_key(site_no, row) not in notified]
            found += len(rows)
            if not new_rows:
                log(f"{site_name}: 대상 회차 {len(rows)}개, 신규 0개")
                continue

            new_rows.sort(key=lambda row: (row.get("scnsrtTm", ""), row.get("scnsNo", "")))
            lines = ["🎟️ CGV 새 예매 회차 오픈", "", MOVIE_NAME, f"CGV {site_name}", "2026-09-30"]
            for row in new_rows:
                screen = row.get("expoScnsNm") or row.get("scnsNm") or "상영관"
                start = fmt_time(row.get("scnsrtTm"))
                end = fmt_time(row.get("scnendTm"))
                free = row.get("frSeatCnt")
                total = row.get("cpSeatCnt") or row.get("stcnt")
                seat_text = "" if free is None else f" · {free}/{total}석"
                lines.append(f"• {start}–{end} · {screen}{seat_text}")

            send_telegram("\n".join(lines))
            for row in new_rows:
                notified.add(row_key(site_no, row))
            state["notified"] = sorted(notified)
            save_state(state)
            log(f"{site_name}: 신규 {len(new_rows)}개 알림 완료")
        except Exception as error:
            log(f"{site_name}: 조회 실패 - {error}")
    return found


def main():
    state = load_state()
    session = requests.Session(impersonate="chrome")
    if "--test-alert" in sys.argv:
        send_telegram("✅ GitHub Actions CGV 알림 연결 테스트 성공")
        log("테스트 알림 전송 완료")
        return

    once = "--once" in sys.argv
    deadline = time.monotonic() + LOOP_MINUTES * 60
    while True:
        check_once(session, state)
        if once or time.monotonic() + INTERVAL >= deadline:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
