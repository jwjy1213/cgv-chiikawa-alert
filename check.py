#!/usr/bin/env python3
import json
import importlib
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from curl_cffi import requests

KST = timezone(timedelta(hours=9))
BASE = "https://cgv.co.kr/api/v1/booking"
MOVIE_NAME = "극장판 치이카와-인어 섬의 비밀"
START_DATE = date(2026, 9, 30)
DISCOVERY_DAYS = 60
MAX_SHOWTIME_REQUESTS = 25
CANCELLATION_ALERTS = os.environ.get("CANCELLATION_ALERTS", "true").lower() == "true"
CANCEL_WATCH_DATES = {
    item.strip() for item in os.environ.get("CANCEL_WATCH_DATES", "").split(",") if item.strip()
}
CANCEL_WATCH_TIMES = {
    item.strip().replace(":", "")
    for item in os.environ.get("CANCEL_WATCH_TIMES", "").split(",")
    if item.strip()
}
CANCEL_WATCH_SITES = {
    item.strip() for item in os.environ.get("CANCEL_WATCH_SITES", "").split(",") if item.strip()
}
PREFERRED_ZONE_ONLY = os.environ.get("PREFERRED_ZONE_ONLY", "false").lower() == "true"
PREFERRED_ROWS = tuple(
    item.strip().upper()
    for item in os.environ.get("PREFERRED_ROWS", "H,I,J").split(",")
    if item.strip()
)
PREFERRED_CENTER_FRACTION = float(os.environ.get("PREFERRED_CENTER_FRACTION", "0.5"))
SEAT_PROVIDER_MODULE = os.environ.get("SEAT_PROVIDER_MODULE", "").strip()
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


def ymd(value):
    return value.strftime("%Y%m%d")


def display_date(value):
    value = str(value or "")
    if len(value) == 8:
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def default_state():
    return {
        "notified": [],
        "mov_no": "",
        "discovery_offset": 1,
        "refresh_offset": 0,
        "open_dates": {},
        "seat_state": {},
    }


def load_state():
    state = default_state()
    if not STATE_FILE.exists():
        return state
    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            state.update(saved)
        return state
    except Exception:
        return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def api_get(session, path, params, tries=2):
    last_error = None
    for attempt in range(tries):
        try:
            response = session.get(
                f"{BASE}/{path}", params=params, headers=HEADERS, timeout=20
            )
            response.raise_for_status()
            body = response.json()
            if body.get("statusCode") != 0:
                raise RuntimeError(f"CGV 오류: {body.get('statusMessage')}")
            return body.get("data") or []
        except Exception as error:
            last_error = error
            if attempt < tries - 1:
                time.sleep(2)
    raise last_error


def fetch_rows(session, site_no, play_date, mov_no=""):
    rows = api_get(
        session,
        "searchMovScnInfo",
        {"coCd": "A420", "siteNo": site_no, "scnYmd": play_date, "rtctlScopCd": "08"},
    )
    wanted = normalize(MOVIE_NAME)
    matched = []
    for row in rows:
        same_movie = str(row.get("movNo", "")) == str(mov_no) if mov_no else False
        if same_movie or wanted in normalize(row.get("movNm")):
            row.setdefault("scnYmd", play_date)
            matched.append(row)
    return matched


def fetch_open_dates(session, site_no, mov_no):
    rows = api_get(
        session,
        "searchSiteScnscYmdListByMov",
        {"coCd": "A420", "siteNo": site_no, "movNo": mov_no},
    )
    start = ymd(START_DATE)
    return sorted({str(row.get("scnYmd", "")) for row in rows if str(row.get("scnYmd", "")) >= start})


def row_key(site_no, row):
    return ":".join(
        [site_no, str(row.get("movNo", "")), str(row.get("scnYmd", "")),
         str(row.get("scnsrtTm", "")), str(row.get("scnsNo", ""))]
    )


def fmt_time(value):
    value = str(value or "")
    return f"{value[:2]}:{value[2:]}" if len(value) == 4 else value


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cancellation_filter_matches(site_no, row):
    play_date = str(row.get("scnYmd", ""))
    start_time = str(row.get("scnsrtTm", "")).replace(":", "")
    if CANCEL_WATCH_SITES and site_no not in CANCEL_WATCH_SITES:
        return False
    if CANCEL_WATCH_DATES and play_date not in CANCEL_WATCH_DATES:
        return False
    if CANCEL_WATCH_TIMES and start_time not in CANCEL_WATCH_TIMES:
        return False
    return True


def load_seat_provider():
    from seat_zone import NoSeatMapProvider

    if not SEAT_PROVIDER_MODULE:
        return NoSeatMapProvider()
    module = importlib.import_module(SEAT_PROVIDER_MODULE)
    factory = getattr(module, "create_provider", None)
    if not callable(factory):
        raise RuntimeError(
            f"{SEAT_PROVIDER_MODULE}.create_provider() 함수가 필요합니다."
        )
    return factory()


if PREFERRED_ZONE_ONLY:
    from seat_zone import SeatZonePolicy

    SEAT_PROVIDER = load_seat_provider()
    SEAT_ZONE_POLICY = SeatZonePolicy(
        rows=PREFERRED_ROWS,
        center_fraction=PREFERRED_CENTER_FRACTION,
    )
else:
    SEAT_PROVIDER = None
    SEAT_ZONE_POLICY = None


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
    if response.status_code >= 400:
        raise RuntimeError(f"Telegram HTTP {response.status_code}: {response.text}")


def cancellation_message(site_name, row, previous_free, current_free, zone_seats=None):
    play_date = display_date(row.get("scnYmd", ""))
    screen = row.get("expoScnsNm") or row.get("scnsNm") or "상영관"
    start = fmt_time(row.get("scnsrtTm"))
    end = fmt_time(row.get("scnendTm"))
    lines = [
        "♻️ CGV 취소표/빈자리 발생",
        "",
        MOVIE_NAME,
        f"CGV {site_name}",
        play_date,
        f"• {start}–{end} · {screen}",
    ]
    if zone_seats is None:
        lines.append(f"잔여석 {previous_free}석 → {current_free}석")
    else:
        labels = ", ".join(seat.label for seat in zone_seats)
        lines.append(f"선호 구역 빈자리 {len(zone_seats)}석: {labels}")
    lines.extend(["", "좌석은 다른 사람이 먼저 선택할 수 있으니 바로 확인하세요."])
    return "\n".join(lines)


def track_cancellation_availability(session, site_no, site_name, rows, state):
    if not CANCELLATION_ALERTS:
        return 0

    seat_state = state.setdefault("seat_state", {})
    alerts = 0
    for row in rows:
        if not cancellation_filter_matches(site_no, row):
            continue
        free = as_int(row.get("frSeatCnt"))
        if free is None:
            continue

        key = row_key(site_no, row)
        previous = seat_state.get(key)
        entry = dict(previous or {})
        entry["last_free"] = free
        entry["ever_sold_out"] = bool(entry.get("ever_sold_out")) or free == 0
        entry["last_seen"] = datetime.now(KST).isoformat(timespec="seconds")

        if PREFERRED_ZONE_ONLY:
            seats = SEAT_PROVIDER.fetch_seats(session, site_no, row)
            if seats is None:
                entry["zone_status"] = "provider_unavailable"
                seat_state[key] = entry
                continue
            preferred = SEAT_ZONE_POLICY.available_seats(seats)
            previous_zone_free = entry.get("last_zone_free")
            current_zone_free = len(preferred)
            entry["last_zone_free"] = current_zone_free
            entry["zone_status"] = "ready"
            if previous_zone_free == 0 and current_zone_free > 0:
                send_telegram(
                    cancellation_message(
                        site_name,
                        row,
                        0,
                        current_zone_free,
                        zone_seats=preferred,
                    )
                )
                alerts += 1
                log(f"{site_name}: 선호 좌석 구역 빈자리 {current_zone_free}석 알림 완료")
        elif previous is not None:
            previous_free = as_int(previous.get("last_free"))
            if previous_free == 0 and free > 0 and previous.get("ever_sold_out"):
                send_telegram(
                    cancellation_message(site_name, row, previous_free, free)
                )
                alerts += 1
                log(f"{site_name}: 취소표 {free}석 알림 완료")

        seat_state[key] = entry

    state["seat_state"] = seat_state
    return alerts


def notify_new_rows(site_no, site_name, rows, state):
    notified = set(state["notified"])
    new_rows = [row for row in rows if row_key(site_no, row) not in notified]
    if not new_rows:
        return 0

    grouped = defaultdict(list)
    for row in new_rows:
        grouped[str(row.get("scnYmd", ""))].append(row)

    sent = 0
    for play_date, date_rows in sorted(grouped.items()):
        date_rows.sort(key=lambda row: (row.get("scnsrtTm", ""), row.get("scnsNo", "")))
        lines = [
            "🎟️ CGV 새 예매 회차 오픈",
            "",
            MOVIE_NAME,
            f"CGV {site_name}",
            display_date(play_date),
        ]
        for row in date_rows:
            screen = row.get("expoScnsNm") or row.get("scnsNm") or "상영관"
            start = fmt_time(row.get("scnsrtTm"))
            end = fmt_time(row.get("scnendTm"))
            free = row.get("frSeatCnt")
            total = row.get("cpSeatCnt") or row.get("stcnt")
            seat_text = "" if free is None else f" · {free}/{total}석"
            lines.append(f"• {start}–{end} · {screen}{seat_text}")

        send_telegram("\n".join(lines))
        for row in date_rows:
            notified.add(row_key(site_no, row))
        state["notified"] = sorted(notified)
        save_state(state)
        sent += len(date_rows)
        log(f"{site_name} {display_date(play_date)}: 신규 {len(date_rows)}개 알림 완료")
    return sent


def discover_movie(session, state):
    offset = int(state.get("discovery_offset", 1)) % (DISCOVERY_DAYS + 1)
    dates = [ymd(START_DATE)]
    rotating = ymd(START_DATE + timedelta(days=offset))
    if rotating not in dates:
        dates.append(rotating)

    for play_date in dates:
        for site_no, site_name in SITES:
            try:
                rows = fetch_rows(session, site_no, play_date)
                if rows and not state.get("mov_no"):
                    state["mov_no"] = str(rows[0].get("movNo", ""))
                    log(f"영화 코드 자동 발견: {state['mov_no']}")
                notify_new_rows(site_no, site_name, rows, state)
                track_cancellation_availability(session, site_no, site_name, rows, state)
                log(f"{site_name} {display_date(play_date)}: 대상 회차 {len(rows)}개")
            except Exception as error:
                log(f"{site_name} {display_date(play_date)}: 조회 실패 - {error}")

    state["discovery_offset"] = 1 if offset >= DISCOVERY_DAYS else offset + 1
    save_state(state)
    if not state.get("mov_no"):
        log(f"영화 코드 탐색 중: {display_date(dates[0])} 및 {display_date(dates[-1])}")


def monitor_all_open_dates(session, state):
    mov_no = str(state["mov_no"])
    previous = state.get("open_dates", {})
    current = {}
    urgent_pairs = []

    for site_no, site_name in SITES:
        try:
            dates = fetch_open_dates(session, site_no, mov_no)
            current[site_no] = dates
            old_dates = set(previous.get(site_no, []))
            urgent_pairs.extend((site_no, site_name, day) for day in dates if day not in old_dates)
            log(f"{site_name}: 예매 가능 날짜 {len(dates)}개")
        except Exception as error:
            current[site_no] = previous.get(site_no, [])
            log(f"{site_name}: 날짜 목록 조회 실패 - {error}")

    all_pairs = []
    site_names = dict(SITES)
    for site_no, dates in current.items():
        all_pairs.extend((site_no, site_names[site_no], day) for day in dates)
    all_pairs.sort(key=lambda item: (item[2], item[0]))

    chosen = list(dict.fromkeys(urgent_pairs))
    if all_pairs:
        cursor = int(state.get("refresh_offset", 0)) % len(all_pairs)
        remaining = max(0, MAX_SHOWTIME_REQUESTS - len(chosen))
        for index in range(min(remaining, len(all_pairs))):
            pair = all_pairs[(cursor + index) % len(all_pairs)]
            if pair not in chosen:
                chosen.append(pair)
        state["refresh_offset"] = (cursor + max(1, remaining)) % len(all_pairs)

    for site_no, site_name, play_date in chosen:
        try:
            rows = fetch_rows(session, site_no, play_date, mov_no)
            notify_new_rows(site_no, site_name, rows, state)
            track_cancellation_availability(session, site_no, site_name, rows, state)
            log(f"{site_name} {display_date(play_date)}: 대상 회차 {len(rows)}개")
        except Exception as error:
            log(f"{site_name} {display_date(play_date)}: 시간표 조회 실패 - {error}")

    state["open_dates"] = current
    save_state(state)


def check_once(session, state):
    if state.get("mov_no"):
        monitor_all_open_dates(session, state)
    else:
        discover_movie(session, state)


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
