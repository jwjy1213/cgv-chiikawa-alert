# CGV 치이카와 예매 알림

GitHub Actions에서 CGV 시간표를 확인하고 새 회차와 취소표를 Telegram으로 알립니다.

- 영화: 극장판 치이카와-인어 섬의 비밀
- 지점: 용산아이파크몰, 구로, 영등포타임스퀘어, 강남, 홍대
- 모든 상영관
- 새 회차와 매진 후 다시 생긴 좌석 알림
- 알림 버튼은 치이카와 영화의 CGV 공유 링크를 통해 CGV 앱 실행을 시도
- CGV가 조회를 차단하면 전체 요청을 5분, 15분, 1시간 순서로 쉬고 장애와 복구를 Telegram으로 알림
- 일반 회차와 새 날짜는 최대 3분, 매진 회차는 1분 간격으로 확인
- 한 주기에는 시간표 1건과 날짜 목록 1건만 조회하고 요청 사이를 5~8초 띄워 부하를 분산
- 15분마다 새 GitHub 실행 서버에서 12분간 감시해 차단된 IP에 오래 머물지 않음
- 예정된 실행 교체에는 중단 알림을 보내지 않고 실제 오류에만 실패 알림

민감한 값은 저장소의 Settings → Secrets and variables → Actions에 등록합니다.

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Actions 탭에서 `CGV 치이카와 예매 감시`를 열고 `Run workflow`의 `test_alert`를 체크하면 Telegram 연결만 시험할 수 있습니다.

`PRIORITY_DATES`는 기본적으로 비어 있습니다. 특정 날짜를 더 자주 확인하려면
`.github/workflows/watch.yml`에 `YYYYMMDD` 형식으로 쉼표로 구분해 설정합니다.
