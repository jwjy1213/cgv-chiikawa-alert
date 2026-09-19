# CGV 치이카와 예매 알림

GitHub Actions에서 1분마다 CGV의 2026-09-30 시간표를 확인하고 새 회차를 Telegram으로 알립니다.

- 영화: 극장판 치이카와-인어 섬의 비밀
- 지점: 용산아이파크몰, 구로, 영등포타임스퀘어, 강남, 홍대
- 모든 상영관
- 새 회차만 알림

민감한 값은 저장소의 Settings → Secrets and variables → Actions에 등록합니다.

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Actions 탭에서 `CGV 치이카와 예매 감시`를 열고 `Run workflow`의 `test_alert`를 체크하면 Telegram 연결만 시험할 수 있습니다.
