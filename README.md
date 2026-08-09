# NEXTRADE 시장운영 DASHBOARD

한국투자증권 KIS Open API로 관심종목의 KRX/NXT 실시간 가격·거래량·거래대금을 비교하고, 기존 KIND 시장조치와 넥스트레이드 종목 현황도 함께 조회하는 Streamlit 앱입니다.

## 웹 접속

- [NXT 시장조치 모니터](https://nxt-market-monitor.streamlit.app/)

## 화면

1. **NXT DASHBOARD**
   - 최신 NXT 대상 전종목의 KRX/NXT 누적 시세 조회
   - 과거일은 NXT 공식 종가와 KRX OPEN API 종목·지수 종가를 결합해 동일한 비교표 제공
   - KOSPI, KOSDAQ, KOSPI200, KOSDAQ150 현재지수 표시
   - KOSPI200 선물·KOSPI200 야간선물 최근월물 현재가 표시
   - KIS 멀티종목 REST API로 15종목의 KRX/NXT 30개 항목을 한 번에 처리
   - 전체 갱신 목표 주기 10초로 고정
   - NXT 거래대금 내림차순 정렬과 NXT 거래불가사유 표시
   - 종목별 KRX 현재가와 시가총액 표시(장중은 현재가×상장주식수, 과거는 KRX MKTCAP)
   - 종목표 위 NXT·KRX 시장 거래량·거래대금 합계와 NXT/KRX 비율 표시
   - 전체 진행률, 실제 완료시간, 검색 및 KOSPI/KOSDAQ 필터 제공
   - NXT 가중 등락률을 기준가 대비·KRX 현재가 대비 두 값으로 한 카드에 표시
   - 과거일은 `data/history.db`에 저장된 상세 데이터를 우선 사용
2. **NXT·KRX 일별 거래 추이**
   - 2025-03-04 이후 NXT·KRX 거래량·거래대금과 NXT/KRX 비율을 SQLite에서 조회
   - 종료일 기본값은 DB의 직전 확정 거래일
   - 직전 거래일 기준 최근 6개 달의 확정 거래일 일평균 거래량·거래대금 표시
   - 거래량비율·거래대금비율을 각각 별도 차트로 제공
   - 시장별 거래량·거래대금 차트는 일별 상세표 아래에 표시
   - 오늘 값은 기본 대시보드의 전종목 갱신 완료 때 당일 누적으로 저장
3. **NXT 상·하한가 종목 현황**
   - 당일은 KIS NXT 시세의 시가·고가·저가·현재가와 상·하한가를 순환 조회
   - 과거일은 `history.db`에 저장된 NXT 공식 확정 OHLC를 조회
   - 거래세션 선택 없이 하루 전체 상·하한가 도달 종목을 한 표로 표시
   - 일별 OHLC로 후보 종목을 선별한 뒤 해당 종목의 KIS NXT 분봉만 조회해 최초 도달시각 확인
   - `기록시점`은 장 시작 가격이 상·하한가이면 `시가`, 이후 기록이면 `HH:MM`으로 표시
   - 상한가·하한가·시가·종가는 기준가격 대비 등락률과 함께 표시하고 거래량·거래대금은 제외
   - 확인한 최초 도달시각은 `nxt_limit_hit_times`에 저장하고 이후에는 DB에서 조회
4. **NXT 상·하한가 근접 종목 현황**
   - 당일 장중 시세와 과거 확정 OHLC를 기존 상·하한가 화면과 같은 방식으로 조회
   - 상한가는 상한가 아래, 하한가는 하한가 위의 0~3틱 범위에 진입한 종목을 표시
   - 가격대 경계를 지날 때 각 구간의 호가가격단위를 한 칸씩 적용해 실제 3개 호가 범위를 계산
   - 최근접가격·잔여틱과 최초 근접 범위 진입시각을 표시하며 확인한 시각은 DB에 저장
   - 잔여틱 0틱인 상·하한가와 1~3틱인 순수 근접 종목을 요약카드에서 분리 집계
   - 확정일 판정 결과는 `nxt_limit_proximity_hits`에 일자·종목·방향별로 저장
5. **KRX 시장조치 조회**
   - 시작일·종료일 조회
   - 전체 또는 8개 세부 분류 복수 선택 후 조회
   - 공시일의 NXT 정규시장 거래현황에 포함된 종목만 표시
   - `NXT 거래가능시장`, `NXT 거래불가사유`를 NXT 원본 값으로 표시
   - KIND 원문 링크 제공
6. **NXT 정규시장 종목 현황** *(탐색 메뉴에서 숨김)*
   - 선택일의 NXT 정규시장 거래현황 종목을 직접 조회
   - `거래가능시장`, `거래불가사유`를 NXT 원본 값으로 표시
   - KOSPI/KOSDAQ 시장 정보 표시
   - 해당 일자의 편입·편출 변동내역 표시
7. **NXT 정규시장 종목 변동내역**
   - 당일 편입·편출 종목수와 종목별 변동내역
   - 화면 조회 시 NXT 사이트를 호출하지 않고 `history.db` 전용 테이블만 조회

`KRX 시장조치` 집계 화면의 구현은 유지하지만 현재 탐색 메뉴에서는 숨겨져 있습니다.
차트 구현도 유지하지만 현재 모든 화면에서 숨겨져 있으며, 브라우저의 라이트·다크 테마를 따릅니다.

## 설치 및 실행

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run app.py
```

`.streamlit/secrets.toml.example`을 `.streamlit/secrets.toml`로 복사한 뒤 KIS Developers 키와 과거일 조회용 `KRX_KEY`를 입력합니다. 상세 내용은 [KIS 실시간 설정](docs/kis_realtime.md)을 참고하세요.

앱은 과거일 대시보드 데이터를 `data/history.db`에 날짜·종목 단위로 정규화해 저장합니다. NXT 공식 종목행, NXT 대상 종목의 KRX 확정행, 4개 지수행, 일별 합계를 저장하므로 같은 과거일을 다시 볼 때 API를 호출하지 않습니다. 공시·원본 응답 캐시는 `data/cache.db`, 당일 수집 상태는 `data/kis_market.db`를 사용합니다.

배포 저장소에는 확정 과거 데이터의 압축 시드인 `data/history.db.gz`를 포함합니다. 새 Streamlit 인스턴스에서 `history.db`가 없으면 이 시드를 자동 복원하므로 과거 화면을 즉시 조회할 수 있습니다. `cache.db`와 `kis_market.db`는 각각 재생성 가능한 API 캐시와 장중 임시 상태이므로 Git에는 포함하지 않습니다. 실행 중 추가된 DB 변경은 컨테이너 재시작 시 유지되지 않으므로, 확정 데이터 시드는 필요할 때 다시 생성해 Git에 반영해야 합니다.

전체 과거 데이터는 다음 명령으로 누락분만 추가 저장할 수 있습니다.

```powershell
python scripts\backfill_market_history.py --start 2025-03-04 --end 2026-08-06 --workers 4
python scripts\build_history_seed.py
```

KRX 전체 종목 원본을 날짜마다 중복 보관하지 않고, 대시보드에 필요한 NXT 대상 종목만 `history.db`에 저장합니다. KRX API 응답 자체는 백필 과정에서 영구 캐시하지 않아 DB 용량 증가를 제한합니다.

### NXT 종목 변동내역 자동 적재

변동내역은 `history.db`의 `nxt_membership_changes` 테이블에 저장합니다. 데이터가 없는 날도 동기화 완료일로 기록하는 `nxt_change_sync_days`와 실행 상태를 기록하는 `nxt_change_sync_state`를 함께 사용해, 앱 재시작 후에도 누락 날짜만 보충합니다.

최초 전체 적재 또는 수동 재적재는 다음 명령으로 실행합니다.

```powershell
python scripts\sync_nxt_changes.py --start 2025-03-04 --end 2026-08-05 --force
```

Windows 자정 작업은 다음 명령으로 등록합니다. 매일 00:00에 전일까지의 누락분을 확인하고 증분 적재하며, PC가 꺼져 실행을 놓친 경우 다음 기동 때 실행됩니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_nxt_change_task.ps1
```

Streamlit 앱에도 동일한 누락 보충 스케줄러가 있어 앱 실행 중 자정 갱신과 앱 재시작 시 누락 보충을 수행합니다. DB 실행 잠금으로 Windows 작업과 앱 스케줄러가 동시에 같은 구간을 수집하지 않도록 했습니다.

### KOSPI200 선물 과거 데이터

KRX OPEN API의 `선물 일별매매정보(주식선물外)`에서 정규시장 KOSPI200 선물 최근월물을 골라 `krx_daily_futures`에 저장합니다. 종가, 전일대비, 등락률, 거래량, 거래대금, 미결제약정, 정산가를 보관합니다.

```powershell
python scripts\backfill_krx_futures.py --start 2025-03-04 --end 2026-08-05 --workers 4 --force
```

### KRX TMI·달러-원 과거 데이터

KRX TMI는 KRX OPEN API의 `KRX 시리즈 일별시세정보`, 달러-원은 KIS의 `FX@KRW`(KMB) 일별 환율을 사용합니다. NXT 지수는 2025-04-01을 100으로 두고 매 거래일의 시가총액 가중 NXT 등락률을 연속 적용합니다.

```powershell
python scripts\backfill_krx_tmi.py --start 2025-03-04 --end 2026-08-06 --workers 4 --force
python scripts\backfill_usd_krw.py --start 2025-03-04 --end 2026-08-07
```

### NXT OHLC·상하한가 과거 데이터

NXT 공식 일별 시가·고가·저가·종가와 기준가격을 저장합니다. 과거 상·하한가는 기준가격의 ±30% 가격제한폭을 NXT 호가단위로 절사해 산출합니다. 도달 종목은 `nxt_limit_hits`에 날짜·종목·상하한 구분별로 저장하므로 화면 조회 때 원본 사이트를 다시 수집하지 않습니다.

```powershell
python scripts\backfill_nxt_ohlc.py --start 2025-03-04 --end 2026-08-06 --workers 4 --force
python scripts\backfill_nxt_limit_hits.py
python scripts\backfill_nxt_limit_proximity_hits.py --start-date 2025-03-04 --end-date 2026-08-06
```

상·하한가 이력 백필은 시장 개설일부터 DB의 최신 확정일까지 전체 OHLC를 판정하고, KIS 분봉 보존기간 안의 장중 도달 건만 최초 도달시각을 추가 조회합니다. 시가 도달은 항상 `시가`로 확정합니다. 분봉 보존기간을 지난 장중 도달 건은 가격과 도달 사실은 보관하되 정확한 최초 시각은 `RETENTION_EXPIRED` 상태로 남깁니다. 작업이 중단돼도 다음 실행에서 `PENDING` 건만 이어서 보강합니다.

상·하한가 근접 이력 백필은 저장된 확정 OHLC만 사용하므로 외부 API를 다시 호출하지 않습니다. 잔여틱 0~3틱의 최근접가격과 방향을 저장하며, 이후 일일 확정 스냅샷 저장 때도 같은 테이블을 자동 갱신합니다.

프리마켓 OHLC는 KIS의 과거 분봉 보관 범위(최대 1년) 안에서 날짜별로 추가 저장할 수 있습니다. 전 종목을 종목별로 조회하므로 거래일당 약 1분이 걸립니다.

```powershell
python scripts\backfill_nxt_premarket.py --start 2026-08-06 --end 2026-08-06
```

### 익일 오전 08:00 확정 데이터 저장

다음 작업은 KRX OPEN API 데이터가 갱신되는 익일 오전 08:00에 직전일의 NXT 종목별 확정 누적값, KRX 종목·지수 확정값, KOSPI200 선물 최근월물 값과 NXT 상·하한가 최초 도달시각을 `history.db`에 저장합니다. 첫 실행에 실패하면 1분 간격으로 최대 10회 자동 재시도합니다. 실행을 놓쳤으면 마지막 확정 저장일 다음 날부터 누락분을 보충하며, 휴장일은 저장하지 않습니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_daily_market_task.ps1
```

익일 08:00 저장이 끝난 확정 행은 장중 수집기가 다시 당일 누적 행으로 덮어쓰지 않습니다.

## 데이터 출처

- [KIND 상세검색](https://kind.krx.co.kr/disclosure/details.do?method=searchDetailsMain)
- [NXT 정규시장 거래현황](https://www.nextrade.co.kr/menu/transactionStatusMain/menuList.do)
- [NXT 매매체결종목 변동내역](https://nextrade.co.kr/menu/transactionStatusConclusion/menuList.do)
- [KRX Data Marketplace OPEN API](https://openapi.krx.co.kr/)
- [KRX 주식시장 거래시간·대량/바스켓 매매 안내](https://global.krx.co.kr/contents/GLB/01/0109/0109000000/guide_to_trading_in_the_korean_stock_market.pdf)

NXT 시장 개설일인 2025-03-04 이후를 지원합니다. KIND와 NXT가 공개한 웹 응답 형식이 변경되면 파서 수정이 필요할 수 있습니다.

NXT 일별 매매체결대상·거래가능·사유별 이력을 다시 계산하고 Excel로 내보내려면 다음 명령을 사용합니다.

```powershell
python scripts\export_nxt_eligibility_history.py --start-date 2025-03-04 --end-date 2026-08-08
```

## 주요 지표 정의

- `NXT 종목수`: 기준일의 NXT 매매체결대상 고유 종목 수. 거래불가 종목도 포함하며, 구형 원본에서 빠진 거래제한 종목은 지정·해제 변동으로 복원
- `편입·편출 종목수`: 해당 일자의 실제 매매체결대상 선정 변동 수. 투자경고·투자위험·단기과열 지정/해제는 제외
- `거래불가 지정·해제 종목수`: 원본 편출·편입 중 투자경고·투자위험·단기과열 등 일시 거래제한의 시작·해제로 재분류한 수
- 거래정지·관리·환기·단기과열 상태 수: 해당 일자 NXT 원본의 `거래불가사유`별 종목 수
- 투자경고·투자위험 상태 수: KIND의 공식 지정일·해제일 기간을 해당 일자 NXT 종목과 교차한 고유 종목 수
- 당일공시 수: 해당 일자의 NXT 정규시장 거래현황 종목에서 각 분류의 공시가 발생한 고유 종목 수

원본 `편입/편출` 값은 감사 추적을 위해 그대로 보관합니다. 파생 집계에서는 투자경고·투자위험·단기과열 지정은 `거래불가`, 해당 해제는 `거래불가 해제`로 처리합니다. 관리종목·투자주의환기·정기변경·시장관리 등은 실제 편출로 유지합니다.

세부 품질 기준과 알려진 제한은 [docs/data_quality.md](docs/data_quality.md)를 참고하세요.
