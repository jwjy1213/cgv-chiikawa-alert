#!/usr/bin/env python3
import json
import importlib
import os
import random
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
MAX_SHOWTIME_REQUESTS = int(os.environ.get("MAX_SHOWTIME_REQUESTS", "1"))
MAX_OPEN_DATE_REQUESTS = int(os.environ.get("MAX_OPEN_DATE_REQUESTS", "1"))
OPEN_DATE_INTERVAL = int(os.environ.get("OPEN_DATE_INTERVAL_SECONDS", "180"))
SOLD_OUT_INTERVAL = int(os.environ.get("SOLD_OUT_INTERVAL_SECONDS", "60"))
REGULAR_INTERVAL = int(os.environ.get("REGULAR_INTERVAL_SECONDS", "180"))
FAILED_RETRY_INTERVAL = int(os.environ.get("FAILED_RETRY_INTERVAL_SECONDS", "300"))
PRIORITY_INTERVAL = int(os.environ.get("PRIORITY_INTERVAL_SECONDS", "60"))
PRIORITY_OPEN_DATE_INTERVAL = int(
    os.environ.get("PRIORITY_OPEN_DATE_INTERVAL_SECONDS", "120")
)
PRIORITY_FAILED_RETRY_INTERVAL = int(
    os.environ.get("PRIORITY_FAILED_RETRY_INTERVAL_SECONDS", "300")
)
REQUEST_JITTER_MIN = float(os.environ.get("REQUEST_JITTER_MIN_SECONDS", "5"))
REQUEST_JITTER_MAX = float(os.environ.get("REQUEST_JITTER_MAX_SECONDS", "8"))
GLOBAL_BLOCK_BACKOFFS = tuple(
    int(item.strip())
    for item in os.environ.get("GLOBAL_BLOCK_BACKOFF_SECONDS", "300,900,3600").split(",")
    if item.strip()
)
GLOBAL_BLOCK_ALERT_THRESHOLD = int(os.environ.get("GLOBAL_BLOCK_ALERT_THRESHOLD", "2"))
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
PRIORITY_SITE_NOS = {
    item.strip()
    for item in os.environ.get("PRIORITY_SITE_NOS", "0013,0059,0191").split(",")
    if item.strip()
}
PRIORITY_DATES = {
    item.strip().replace("-", "")
    for item in os.environ.get("PRIORITY_DATES", "").split(",")
    if item.strip()
}
STATE_FILE = Path(__file__).with_name("state.json")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
CONFIGURED_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "60"))
INTERVAL = max(30, CONFIGURED_INTERVAL)
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "340"))
_LAST_API_REQUEST_AT = 0.0


class CGVBlockedError(RuntimeError):
    pass


class CGVCooldownActive(RuntimeError):
    pass

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
        "open_date_poll": {},
        "showtime_poll": {},
        "global_block": {
            "count": 0,
            "until": 0,
            "alerted": False,
        },
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


def prepare_runtime_state(state):
    previous = dict(state.get("global_block") or {})
    state["global_block"] = {
        "count": 0,
        "until": 0,
        "alerted": bool(previous.get("alerted", False)),
    }
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def pace_api_request():
    global _LAST_API_REQUEST_AT
    if _LAST_API_REQUEST_AT:
        minimum_gap = random.uniform(REQUEST_JITTER_MIN, REQUEST_JITTER_MAX)
        elapsed = time.monotonic() - _LAST_API_REQUEST_AT
        if elapsed < minimum_gap:
            time.sleep(minimum_gap - elapsed)
    _LAST_API_REQUEST_AT = time.monotonic()


def api_get(session, path, params, state, tries=2):
    remaining = global_block_remaining(state)
    if remaining > 0:
        raise CGVCooldownActive(f"CGV 전역 휴식 중 ({remaining}초 남음)")

    last_error = None
    for attempt in range(tries):
        try:
            pace_api_request()
            response = session.get(
                f"{BASE}/{path}", params=params, headers=HEADERS, timeout=20
            )
            if response.status_code == 403:
                cooldown = register_global_block(state)
                raise CGVBlockedError(
                    f"HTTP 403: CGV가 요청을 차단했습니다. {cooldown}초 동안 전체 조회를 쉽니다."
                )
            response.raise_for_status()
            body = response.json()
            if body.get("statusCode") != 0:
                raise RuntimeError(f"CGV 오류: {body.get('statusMessage')}")
            register_cgv_success(state)
            return body.get("data") or []
        except (CGVBlockedError, CGVCooldownActive):
            raise
        except Exception as error:
            last_error = error
            if attempt < tries - 1:
                time.sleep(5)
    raise last_error


def fetch_rows(session, site_no, play_date, state, mov_no=""):
    rows = api_get(
        session,
        "searchMovScnInfo",
        {"coCd": "A420", "siteNo": site_no, "scnYmd": play_date, "rtctlScopCd": "08"},
        state,
    )
    wanted = normalize(MOVIE_NAME)
    matched = []
    for row in rows:
        same_movie = str(row.get("movNo", "")) == str(mov_no) if mov_no else False
        if same_movie or wanted in normalize(row.get("movNm")):
            row.setdefault("scnYmd", play_date)
            matched.append(row)
    return matched


def fetch_open_dates(session, site_no, mov_no, state):
    rows = api_get(
        session,
        "searchSiteScnscYmdListByMov",
        {"coCd": "A420", "siteNo": site_no, "movNo": mov_no},
        state,
    )
    start = ymd(max(START_DATE, datetime.now(KST).date()))
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


def poll_record_due(
    record,
    success_interval,
    now,
    failure_interval=FAILED_RETRY_INTERVAL,
):
    last_attempt = float((record or {}).get("last_attempt", 0) or 0)
    if not last_attempt:
        return True
    interval = success_interval if (record or {}).get("last_ok", True) else failure_interval
    return now - last_attempt >= interval


def pair_key(site_no, play_date):
    return f"{site_no}:{play_date}"


def is_priority_pair(site_no, play_date):
    return site_no in PRIORITY_SITE_NOS and play_date in PRIORITY_DATES


def pair_has_sold_out_show(state, site_no, play_date):
    prefix = f"{site_no}:"
    for key, entry in state.get("seat_state", {}).items():
        parts = str(key).split(":")
        if not str(key).startswith(prefix) or len(parts) < 3 or parts[2] != play_date:
            continue
        if as_int((entry or {}).get("last_free")) == 0:
            return True
    return False


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
                "text": "📱 CGV 앱으로 열기",
                "url": "https://cgv.co.kr/mShrtU/fP2Qw",
            }]]},
        },
        timeout=20,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Telegram HTTP {response.status_code}: {response.text}")


def safe_send_telegram(text):
    try:
        send_telegram(text)
        return True
    except Exception as error:
        log(f"Telegram 알림 전송 실패 - {error}")
        return False


def global_block_remaining(state, now=None):
    now = time.time() if now is None else now
    until = float(state.get("global_block", {}).get("until", 0) or 0)
    return max(0, int(until - now))


def register_global_block(state):
    block = dict(state.get("global_block") or {})
    count = int(block.get("count", 0) or 0) + 1
    index = min(count - 1, len(GLOBAL_BLOCK_BACKOFFS) - 1)
    cooldown = GLOBAL_BLOCK_BACKOFFS[index]
    block.update({
        "count": count,
        "until": time.time() + cooldown,
        "last_seen": datetime.now(KST).isoformat(timespec="seconds"),
    })

    if count >= GLOBAL_BLOCK_ALERT_THRESHOLD and not block.get("alerted", False):
        block["alerted"] = safe_send_telegram(
            "🚨 CGV 조회가 차단되어 감시가 일시 중단되었습니다.\n"
            f"HTTP 403이 연속 {count}회 발생했습니다. "
            f"다음 조회는 약 {cooldown // 60}분 후 시도합니다."
        )

    state["global_block"] = block
    save_state(state)
    log(f"CGV 전역 차단 {count}회: 모든 조회를 {cooldown}초 동안 중단")
    return cooldown


def register_cgv_success(state):
    block = dict(state.get("global_block") or {})
    count = int(block.get("count", 0) or 0)
    was_alerted = bool(block.get("alerted", False))
    if not count and not was_alerted:
        return

    state["global_block"] = {
        "count": 0,
        "until": 0,
        "alerted": False,
        "recovered_at": datetime.now(KST).isoformat(timespec="seconds"),
    }
    save_state(state)
    if was_alerted:
        safe_send_telegram("✅ CGV 조회가 복구되어 예매 감시를 다시 시작합니다.")
    log("CGV 조회 정상: 전역 차단 상태 해제")


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
    base_date = max(START_DATE, datetime.now(KST).date())
    dates = [ymd(base_date)]
    rotating = ymd(base_date + timedelta(days=offset))
    if rotating not in dates:
        dates.append(rotating)

    for play_date in dates:
        for site_no, site_name in SITES:
            try:
                rows = fetch_rows(session, site_no, play_date, state)
                if rows and not state.get("mov_no"):
                    state["mov_no"] = str(rows[0].get("movNo", ""))
                    log(f"영화 코드 자동 발견: {state['mov_no']}")
                notify_new_rows(site_no, site_name, rows, state)
                track_cancellation_availability(session, site_no, site_name, rows, state)
                log(f"{site_name} {display_date(play_date)}: 대상 회차 {len(rows)}개")
            except Exception as error:
                log(f"{site_name} {display_date(play_date)}: 조회 실패 - {error}")
                if isinstance(error, (CGVBlockedError, CGVCooldownActive)):
                    save_state(state)
                    return

    state["discovery_offset"] = 1 if offset >= DISCOVERY_DAYS else offset + 1
    save_state(state)
    if not state.get("mov_no"):
        log(f"영화 코드 탐색 중: {display_date(dates[0])} 및 {display_date(dates[-1])}")


def monitor_all_open_dates(session, state):
    mov_no = str(state["mov_no"])
    previous = state.get("open_dates", {})
    current = dict(previous)
    urgent_pairs = []
    now = time.time()
    open_date_poll = state.setdefault("open_date_poll", {})

    date_candidates = []
    for site_no, site_name in SITES:
        poll = open_date_poll.get(site_no, {})
        priority_site = site_no in PRIORITY_SITE_NOS
        success_interval = (
            PRIORITY_OPEN_DATE_INTERVAL if priority_site else OPEN_DATE_INTERVAL
        )
        failure_interval = (
            PRIORITY_FAILED_RETRY_INTERVAL if priority_site else FAILED_RETRY_INTERVAL
        )
        if poll_record_due(poll, success_interval, now, failure_interval):
            last_attempt = float(poll.get("last_attempt", 0) or 0)
            date_candidates.append(
                (0 if priority_site else 1, last_attempt, site_no, site_name)
            )

    date_candidates.sort()
    for _, _, site_no, site_name in date_candidates[:MAX_OPEN_DATE_REQUESTS]:
        try:
            dates = fetch_open_dates(session, site_no, mov_no, state)
            current[site_no] = dates
            old_dates = set(previous.get(site_no, []))
            urgent_pairs.extend((site_no, site_name, day) for day in dates if day not in old_dates)
            log(f"{site_name}: 예매 가능 날짜 {len(dates)}개")
            open_date_poll[site_no] = {
                "last_attempt": time.time(),
                "last_ok": True,
            }
        except Exception as error:
            current[site_no] = previous.get(site_no, [])
            log(f"{site_name}: 날짜 목록 조회 실패 - {error}")
            open_date_poll[site_no] = {
                "last_attempt": time.time(),
                "last_ok": False,
            }
            if isinstance(error, (CGVBlockedError, CGVCooldownActive)):
                state["open_dates"] = current
                state["open_date_poll"] = open_date_poll
                save_state(state)
                return

    candidates = []
    site_names = dict(SITES)
    showtime_poll = state.setdefault("showtime_poll", {})
    urgent_keys = {pair_key(site_no, day) for site_no, _, day in urgent_pairs}
    for site_no, dates in current.items():
        for play_date in dates:
            key = pair_key(site_no, play_date)
            sold_out = pair_has_sold_out_show(state, site_no, play_date)
            priority_pair = is_priority_pair(site_no, play_date)
            if priority_pair:
                interval = PRIORITY_INTERVAL
                failure_interval = PRIORITY_FAILED_RETRY_INTERVAL
                rank = 0
            elif sold_out:
                interval = SOLD_OUT_INTERVAL
                failure_interval = FAILED_RETRY_INTERVAL
                rank = 1
            else:
                interval = REGULAR_INTERVAL
                failure_interval = FAILED_RETRY_INTERVAL
                rank = 2
            poll = showtime_poll.get(key, {})
            if key in urgent_keys or poll_record_due(
                poll, interval, now, failure_interval
            ):
                last_attempt = float(poll.get("last_attempt", 0) or 0)
                candidates.append(
                    (rank, last_attempt, play_date, site_no, site_names[site_no])
                )

    candidates.sort()
    chosen = [
        (site_no, site_name, play_date)
        for _, _, play_date, site_no, site_name in candidates[:MAX_SHOWTIME_REQUESTS]
    ]

    for site_no, site_name, play_date in chosen:
        key = pair_key(site_no, play_date)
        try:
            rows = fetch_rows(session, site_no, play_date, state, mov_no)
            notify_new_rows(site_no, site_name, rows, state)
            track_cancellation_availability(session, site_no, site_name, rows, state)
            log(f"{site_name} {display_date(play_date)}: 대상 회차 {len(rows)}개")
            showtime_poll[key] = {
                "last_attempt": time.time(),
                "last_ok": True,
            }
        except Exception as error:
            log(f"{site_name} {display_date(play_date)}: 시간표 조회 실패 - {error}")
            showtime_poll[key] = {
                "last_attempt": time.time(),
                "last_ok": False,
            }
            if isinstance(error, (CGVBlockedError, CGVCooldownActive)):
                state["open_dates"] = current
                state["open_date_poll"] = open_date_poll
                state["showtime_poll"] = showtime_poll
                save_state(state)
                return

    state["open_dates"] = current
    state["open_date_poll"] = open_date_poll
    state["showtime_poll"] = showtime_poll
    save_state(state)


def check_once(session, state):
    remaining = global_block_remaining(state)
    if remaining > 0:
        return remaining
    if state.get("mov_no"):
        monitor_all_open_dates(session, state)
    else:
        discover_movie(session, state)
    return global_block_remaining(state)


def main():
    state = prepare_runtime_state(load_state())
    session = requests.Session(impersonate="chrome")
    if "--test-alert" in sys.argv:
        send_telegram("✅ GitHub Actions CGV 알림 연결 테스트 성공")
        log("테스트 알림 전송 완료")
        return

    log(
        "스마트 감시 시작: "
        f"매진 회차 {SOLD_OUT_INTERVAL}초, 일반 회차 {REGULAR_INTERVAL}초, "
        f"새 날짜 탐색 {OPEN_DATE_INTERVAL}초 간격"
    )
    if PRIORITY_DATES:
        priority_names = [name for site_no, name in SITES if site_no in PRIORITY_SITE_NOS]
        log(
            "최우선 감시: "
            f"{', '.join(priority_names)} · "
            f"{', '.join(display_date(day) for day in sorted(PRIORITY_DATES))} · "
            f"약 {PRIORITY_INTERVAL}초 간격"
        )
    else:
        log("최우선 감시 날짜 없음: 현재 예매 가능한 날짜를 일반 주기로 확인")
    once = "--once" in sys.argv
    deadline = time.monotonic() + LOOP_MINUTES * 60
    while True:
        cooldown_remaining = check_once(session, state)
        if once or time.monotonic() + INTERVAL >= deadline:
            break
        if cooldown_remaining > 0:
            sleep_for = min(60, cooldown_remaining)
            log(f"CGV 전역 휴식 중: {cooldown_remaining}초 남음")
        else:
            sleep_for = INTERVAL
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
