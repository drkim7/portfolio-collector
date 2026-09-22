# Work 인수인계 — 장중 현재가/확정일봉 분리 (2026-09-22)

## 이미 GitHub에서 수정한 것
저장소: drkim7/portfolio-collector

portfolio-quotes.gs를 다음 원칙으로 수정했다.
- Yahoo query1 실패 시 query2 미러 재시도.
- 장중으로 추정되는 종목은 5m / 1d 차트를 우선 조회.
- 5분봉이 실패하면 1d / 5d로 보조 조회.
- chart의 마지막 유효 close와 regularMarketPrice/time 중 더 최신 시각을 채택.
- 장중 또는 crypto 시세가 30분 넘게 오래되면 앱 미전달 상태로 표시.
- 장외/휴장에서는 마지막 시세를 최대 96시간 허용.
- stale 시세는 시트에는 마지막 관측값으로 보이지만 앱 doPost에는 보내지 않음.
- portfolio-spot-v1 계약은 유지하며 source, status, failure reason만 추가.
- testFirstQuote()를 추가해 첫 데이터 행의 시세 모드/시각/신선도를 확인 가능.

## Google Apps Script에서 할 일
1. 현재 Apps Script 프로젝트의 Code.gs를 GitHub 최신 portfolio-quotes.gs 내용으로 전체 교체한다.
2. Script Properties의 SPREADSHEET_ID, FEED_KEY, Telegram 값은 삭제/변경하지 않는다.
3. 저장 후 testFirstQuote()를 1회 실행해 권한과 Yahoo 응답을 확인한다.
4. checkStockAlerts()를 수동 1회 실행한다.
5. 시트에서 장중 한국 종목의 시세 기준 시각이 오늘 시각으로 움직이는지 확인한다.
6. 웹앱 배포는 기존 deployment를 새 버전으로 업데이트한다. URL을 바꾸지 않는다.
7. 기존 15분 trigger가 있으면 유지한다. 필요하면 installQuoteTrigger()를 한 번 실행해 중복 trigger를 정리한다.

## dh-portfolio-desk 앱에서 할 일
현재 배포된 최신 소스를 기준으로 수정한다. 오래된 백업/예전 desk.tsx를 덮어쓰지 않는다.

목표는 현재가와 기술분석용 확정 종가를 분리하는 것이다.

### 데이터 모델
- spotPrice: Apps Script에서 받은 장중/최근 시세
- spotAsOf: spot 시각
- spotSource: spot 출처
- closePrice: histories 마지막 완결 일봉의 close
- closeDate: histories 마지막 일봉 날짜
- 기존 price는 하위 호환을 위해 남겨도 되지만, 화면 큰 가격/평가액은 fresh spot이 있으면 spotPrice, 아니면 확정 종가/기존 price를 사용한다.

### 절대 지켜야 할 규칙
- Apps Script spot 데이터로 histories를 만들거나 덮어쓰지 않는다.
- RSI/MACD/이평선/ATR/거래량/복구 단계의 확정 판정은 histories의 완결 일봉만 사용한다.
- stale failure가 오면 기존 spot 값을 유지하되 현재 시세 갱신 지연으로 표시하고 새 가격처럼 취급하지 않는다.
- quoteAsOf가 기존 spotAsOf보다 오래되면 덮어쓰지 않는다.
- collector의 일봉 패치가 더 늦게 도착해도 fresh spot을 전일 종가로 덮어쓰지 않는다.

### 화면 예시
- 현재가: 7,1xx원 · 12:xx · Yahoo/Apps Script · 지연 가능
- 최근 확정종가: 6,200원 · 9/21
- 손실복구 코치 확정 단계: 1단계 · 하락 지속 (완결 일봉 기준)
- 장중 참고: 전일 종가 대비 +xx% 반등 중 · 아직 오늘 일봉 미확정
- 복구 코치의 단계 자체는 장중 가격만으로 승격시키지 않는다. spot이 20일선/60일선 등을 장중 돌파했으면 장중 참고 신호로 별도 표시할 수 있다.

### 검증 시나리오
1. 오가노이드사이언스 476040의 장중 시세가 시트에서 오늘 시각으로 갱신된다.
2. 앱 큰 가격과 평가손익은 fresh spot을 사용한다.
3. 차트와 RSI/MACD/복구단계는 전일까지의 완결 일봉을 사용한다.
4. Yahoo가 전일값만 반환하면 앱에 현재가로 전달되지 않고 시세 갱신 지연이 표시된다.
5. Apps Script feed 장애가 나도 기존 histories/계획/메모/59개 보유자료는 유지된다.
6. 기존 비공개 접근, 백업/복원, collector 암호화 연결은 회귀하지 않는다.