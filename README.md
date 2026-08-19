# NEXTRADE 시장운영 DASHBOARD

한국투자증권 KIS Open API로 NXT 대상종목의 KRX/NXT 가격·거래량·거래대금을 비교하고, KIND 시장조치와 넥스트레이드 종목 현황도 함께 조회하는 Streamlit 앱입니다.

## 웹 접속

- [NXT 시장조치 모니터](https://nxt-market-monitor.streamlit.app/)
- [모니터링 페이지 소개자료](docs/dashboard_introduction.md)

## 화면

1. **NXT DASHBOARD**
   - 최신 NXT 대상 전종목의 KRX/NXT 누적 시세 조회
   - KIS 코스피·코스닥 종목 마스터에서 보통주 후보군을 만들고 KIS NXT REST 시세로 당일 대상종목을 계산
   - 직전 대상종목 중 KIS 마스터에 거래정지·관리·단기과열·투자경고/위험이 표시된 종목은 NXT 시세가 0이어도 거래불가 종목으로 보존
   - KIS WebSocket은 최근 저장 NXT 거래대금 상위 최대 20종목의 연결·실시간 수신 정상 여부 확인에 사용
   - 과거일은 NXT 공식 종가와 KRX OPEN API 종목·지수 종가를 결합해 동일한 비교표 제공
   - KOSPI, KOSDAQ, KOSPI200, KOSDAQ150 현재지수 표시
   - KOSPI200 선물·KOSPI200 야간선물 최근월물 현재가 표시
   - KIS 멀티종목 REST API로 15종목의 KRX/NXT 30개 항목을 한 번에 처리
   - 전체 갱신 목표 주기 10초로 고정
   - 최대 5명 동시 접속을 고려해 전 종목 REST는 초당 약 5건, 분봉 전용 큐는 초당 1건으로 제한
   - NXT 휴장 구간인 08:50~09:00에는 전 종목·분봉 REST 조회를 중지
   - NXT 거래대금 내림차순 정렬과 KIS 시장조치 및 로컬 NXT 대사로 확인한 거래불가사유 표시
   - 종목별 KRX 현재가와 시가총액 표시(장중은 현재가×상장주식수, 과거는 KRX MKTCAP)
   - 종목표 위 NXT·KRX 시장 거래량·거래대금 합계와 NXT/KRX 비율 표시
   - 전체 진행률, 실제 완료시간, 검색 및 KOSPI/KOSDAQ 필터 제공
   - NXT 가중 등락률을 기준가 대비·KRX 현재가 대비 두 값으로 한 카드에 표시
   - 과거일은 `data/history.db`에 저장된 상세 데이터를 우선 사용
2. **NXT 상·하한가 근접 종목 현황**
   - 당일 장중 시세와 과거 확정 OHLC를 기존 상·하한가 화면과 같은 방식으로 조회
   - 상한가는 상한가 아래, 하한가는 하한가 위의 0~3틱 범위에 진입한 종목을 표시
   - 가격대 경계를 지날 때 각 구간의 호가가격단위를 한 칸씩 적용해 실제 3개 호가 범위를 계산
   - 최근접가격·잔여틱과 최초 근접 범위 진입시각을 표시하며 확인한 시각은 DB에 저장
   - 잔여틱 0틱인 상·하한가와 1~3틱인 순수 근접 종목을 요약카드에서 분리 집계
   - 확정일 판정 결과는 `nxt_limit_proximity_hits`에 일자·종목·방향별로 저장
3. **NXT 정규시장 종목 변동내역**
   - 시작일 기본값은 시장 개설일인 2025-03-04이며 `일별 집계`를 첫 화면으로 표시
   - 최초 10종목을 `최초` 사유의 편입으로 포함하고, 기간 편입 합계-편출 합계로 순증감을 계산
   - 편입·편출 종목수와 종목별 변동내역에 `변경사유` 표시
   - 일별 NXT 대상 종목 중 KRX 공식 KOSPI200·KOSDAQ150 구성종목 수 표시
   - 거래불가·거래불가 해제는 편입·편출 표에서 제외하고 `거래불가 현황` 표로 분리
   - 2025-06-20까지는 종목 변동내역을 보완 근거로, 2025-06-23부터는 거래현황의 `거래가능시장·거래불가사유`를 우선 사용
   - 변동 API에 없는 편입·편출은 일별 대상 명단과 공식 공지로 대사해 보정
   - 화면 조회 시 NXT 사이트를 호출하지 않고 `history.db` 전용 테이블만 조회
4. **NXT·KRX 일별 거래 추이**
   - 2025-03-04 이후 NXT·KRX 거래량·거래대금과 NXT/KRX 비율을 SQLite에서 조회
   - 종료일 기본값은 DB의 직전 확정 거래일
   - 직전 거래일 기준 최근 6개 달의 확정 거래일 일평균 거래량·거래대금 표시
   - 거래량비율·거래대금비율을 각각 별도 차트로 제공
   - 시장별 거래량·거래대금 차트는 일별 상세표 아래에 표시
   - 오늘 값은 기본 대시보드의 전종목 갱신 완료 때 당일 누적으로 저장
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
7. **NXT 상·하한가 종목 현황** *(탐색 메뉴에서 숨김)*
   - 상·하한가 기록 종목 조회 기능은 유지하지만 사이드바에는 표시하지 않음

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

### 로그인 및 사용자 관리

앱을 처음 실행하면 초기 관리자 계정을 한 번 생성합니다. 이후 사용자는 로그인 화면에서 아이디·비밀번호·사번·이름으로 회원가입을 신청하고, 관리자는 사이드바의 `사용자 관리` 화면에서 신청을 승인하거나 반려할 수 있습니다. 관리자가 직접 계정을 발급하는 방식도 유지되며 권한 변경, 계정 활성화·비활성화, 로그인 잠금 해제와 임시 비밀번호 발급을 지원합니다. 임시 비밀번호로 로그인한 사용자는 대시보드에 들어가기 전에 비밀번호를 반드시 변경해야 합니다.

`AUTH_DATABASE_URL`이 설정되면 계정과 감사기록을 PostgreSQL에 영구 저장하고, 설정하지 않은 로컬 개발환경에서는 `data/auth.db`를 사용합니다. 비밀번호 원문은 저장하지 않으며 로컬 DB는 Git 제외 대상입니다. 기존 SQLite 계정은 `python scripts\migrate_auth_to_postgres.py`로 빈 PostgreSQL에 이전할 수 있습니다. 자세한 설정과 이전 방법은 [로그인·사용자 관리 안내](docs/authentication.md)를 참고하세요.

앱은 과거일 대시보드 데이터를 `data/history.db`에 날짜·종목 단위로 정규화해 저장합니다. NXT 공식 종목행, NXT 대상 종목의 KRX 확정행, 지수행, 일별 합계를 저장하므로 같은 과거일을 다시 볼 때 API를 호출하지 않습니다. KOSPI·KOSDAQ 전 상장종목의 일별 거래정보와 종목속성은 용량과 조회 목적을 분리하기 위해 `data/krx_listed_history.db`에 별도로 저장합니다. 공시·원본 응답 캐시는 `data/cache.db`, 당일 수집 상태는 `data/kis_market.db`, KIS 계산 대상종목과 NXT 공식 대사 결과는 `data/kis_universe.db`를 사용합니다.

배포 저장소에는 확정 과거 데이터의 `data/history.db.gz`, `data/krx_listed_history.db.gz`와 KIS 대상종목의 `data/kis_universe.db.gz` 압축 시드를 포함할 수 있습니다. 새 Streamlit 인스턴스에서 원본 DB가 없으면 시드를 자동 복원합니다. 시드가 갱신되면 파일 식별값과 확정 거래일을 기존 실행 DB와 대조해 누락된 거래일이 있는 실행 DB도 최신 시드로 교체합니다. `cache.db`, `kis_market.db`, `kis_universe_ws.db`는 재생성 가능한 캐시·장중 상태이므로 Git에 포함하지 않습니다. 실행 중 추가된 DB 변경은 컨테이너 재시작 시 유지되지 않으므로 시드는 필요할 때 다시 생성해 Git에 반영해야 합니다.

### KIS 기준 NXT 대상종목·로컬 NXT 대사

Streamlit 화면은 NXT 홈페이지를 직접 호출하지 않습니다. 화면은 `kis_universe.db`와 `history.db`만 읽고, 서버의 KIS 대상종목 계산은 프로세스당 하루 한 번만 백그라운드에서 실행됩니다. 전 종목 장중 가격·누적 거래량·거래대금은 기존의 공유 REST 수집기 한 개가 갱신하므로 동시 접속자마다 별도 호출하지 않습니다.

로컬 PC에서는 다음 명령으로 KIS 계산 결과를 만들고 NXT 공식 거래현황을 한 번 조회해 종목 포함 여부와 거래가능시장·거래불가사유를 대사합니다. NXT 공식 명단은 KIS 대상종목을 대체하지 않고, 불일치 이력 저장과 일치 종목의 상태 메타데이터 보정에만 사용합니다.

```powershell
python scripts\sync_kis_nxt_universe.py --reconcile-official
python scripts\build_kis_universe_seed.py
```

하루 두 번(08:10, 18:10) 자동 대사를 원하면 아래 스크립트를 한 번 실행해 Windows 작업 스케줄러에 등록합니다. 08:50~09:00 NXT 휴장 구간에는 예약하지 않았으며, 앱의 REST·WebSocket 자동 수집도 이 구간과 08:00~20:05 밖에서는 중지합니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_nxt_universe_reconciliation_task.ps1
```

전체 과거 데이터는 다음 명령으로 누락분만 추가 저장할 수 있습니다.

```powershell
python scripts\backfill_market_history.py --start 2025-03-04 --end 2026-08-06 --workers 4
python scripts\backfill_krx_index_constituents.py --start 2025-03-04
python scripts\backfill_krx_listed_history.py --start 2025-03-04 --workers 4
python scripts\build_history_seed.py
python scripts\build_krx_listed_history_seed.py
```

KRX 전체 종목은 전용 DB에 표준코드·단축코드·종목명·시장·주식종류·증권구분·상장주식수·상장일·거래량·거래대금·K200/Q150 편입 여부를 거래일별로 저장합니다. KRX API 원본 JSON은 영구 캐시하지 않습니다.

### KRX 전체 상장종목 일별 거래현황

KRX OPEN API의 유가증권·코스닥 일별매매정보와 종목기본정보를 단축코드로 결합합니다. 코넥스와 ETF·ETN·ELW는 제외하며, KOSPI200·KOSDAQ150 편입 여부는 `history.db`의 일자별 공식 구성종목 이력으로 보강합니다. Streamlit 페이지는 외부 API를 호출하지 않고 `krx_listed_history.db`만 읽습니다.

```powershell
python scripts\backfill_krx_listed_history.py --start 2025-03-04 --workers 4
python scripts\build_krx_listed_history_seed.py
```

### NXT 종목 변동내역 자동 적재

변동 원본은 `history.db`의 `nxt_membership_changes` 테이블에 그대로 저장합니다. 일별 대상 명단에는 반영됐지만 변동 원본에 없는 내역은 `nxt_membership_change_adjustments`에 근거·출처와 함께 별도 저장합니다. 데이터가 없는 날도 동기화 완료일로 기록하는 `nxt_change_sync_days`와 실행 상태를 기록하는 `nxt_change_sync_state`를 함께 사용해, 앱 재시작 후에도 누락 날짜만 보충합니다.

거래불가 일별 상태는 `nxt_daily_unavailability`, 거래불가 지정·해제 이벤트는 `nxt_unavailability_events`에 별도로 저장합니다. 거래현황 원본을 우선 사용하고, 거래현황에서 사유 제공이 시작되기 전 구간은 종목 변동내역으로 복원합니다. KIND 공시 연결은 `nxt_unavailability_kind_links`에 별도로 저장하며, 이벤트일 이전 45일 이내 공시에서 종목코드·거래불가 사유·지정/해제 방향을 우선 검증합니다. 정지와 해제가 한 원문에 함께 있거나 같은 종목의 연속된 거래불가 구간이면 시작 원문을 해당 해제 이벤트에도 연결합니다. 이 조건으로 확정할 수 없는 이벤트는 KIND 공시와 원문을 빈칸으로 표시합니다.

최초 전체 적재 또는 수동 재적재는 다음 명령으로 실행합니다.

```powershell
python scripts\sync_nxt_changes.py --start 2025-03-04 --end 2026-08-05 --force
python scripts\rebuild_nxt_unavailability.py
python scripts\backfill_nxt_unavailability_kind_links.py --start 2025-03-04 --end 2026-08-10
python scripts\build_history_seed.py
```

Windows 자정 작업은 다음 명령으로 등록합니다. 매일 00:00에 전일까지의 누락분을 확인하고 증분 적재하며, PC가 꺼져 실행을 놓친 경우 다음 기동 때 실행됩니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_nxt_change_task.ps1
```

Streamlit 앱에도 동일한 누락 보충 스케줄러가 있어 앱 실행 중 자정 갱신과 앱 재시작 시 누락 보충을 수행합니다. DB 실행 잠금으로 Windows 작업과 앱 스케줄러가 동시에 같은 구간을 수집하지 않도록 했습니다.

### KOSPI200·KOSDAQ150 구성종목 이력

KRX OPEN API에는 지수 시세만 있고 구성종목 이력 API는 없으므로, KRX 지수 사이트의 공식 조회일자별 구성종목과 구성종목 변경내역을 사용합니다. 최신 구성종목에서 변경내역을 역적용해 NXT 거래일별 구성종목을 복원하고 `krx_index_constituents`에 저장합니다. 일별 집계 화면은 이 테이블과 NXT 매매체결대상 종목코드를 교차해 두 지수의 해당 종목수를 표시합니다.

```powershell
python scripts\backfill_krx_index_constituents.py --start 2025-03-04
python scripts\build_history_seed.py
```

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

다음 작업은 KRX OPEN API 데이터가 갱신되는 익일 오전 08:00에 직전일의 NXT 종목별 확정 누적값, KRX 종목·지수 확정값, KRX 전 상장주권 거래정보, KOSPI200 선물 최근월물 값, KOSPI200·KOSDAQ150 구성종목과 NXT 상·하한가 최초 도달시각을 저장합니다. 첫 실행에 실패하면 1분 간격으로 최대 10회 자동 재시도합니다. 실행을 놓쳤으면 마지막 확정 저장일 다음 날부터 누락분을 보충하며, 휴장일은 저장하지 않습니다.

적재가 성공하면 두 배포 DB를 무결성 검사 후 압축해 GitHub Release의 `daily-market-data` 자산으로 교체하고, 날짜·크기·SHA-256 해시를 담은 `data/daily_seed_manifest.json`만 `main`에 커밋합니다. 이 작은 커밋이 Streamlit 재배포를 시작하며, 새 앱 인스턴스는 manifest의 해시를 검증한 최신 Release DB를 내려받습니다. 전체 DB를 매일 Git 이력에 추가하지 않으므로 저장소 용량 증가를 억제합니다. GitHub 인증이나 업로드·푸시에 실패하면 작업이 실패 상태로 끝나 예약 작업의 1분 간격 재시도 대상이 됩니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_daily_market_task.ps1
```

익일 08:00 저장이 끝난 확정 행은 장중 수집기가 다시 당일 누적 행으로 덮어쓰지 않습니다.

## 데이터 출처

- [KIND 상세검색](https://kind.krx.co.kr/disclosure/details.do?method=searchDetailsMain)
- [NXT 정규시장 거래현황](https://www.nextrade.co.kr/menu/transactionStatusMain/menuList.do)
- [NXT 매매체결종목 변동내역](https://nextrade.co.kr/menu/transactionStatusConclusion/menuList.do)
- [KRX Data Marketplace OPEN API](https://openapi.krx.co.kr/)
- [KRX 지수 구성종목](https://data.krx.co.kr/contents/MDC/STAT/standard/MDCSTAT006.jsp)
- [KRX 주식시장 거래시간·대량/바스켓 매매 안내](https://global.krx.co.kr/contents/GLB/01/0109/0109000000/guide_to_trading_in_the_korean_stock_market.pdf)

NXT 시장 개설일인 2025-03-04 이후를 지원합니다. KIND와 NXT가 공개한 웹 응답 형식이 변경되면 파서 수정이 필요할 수 있습니다.

NXT 일별 매매체결대상·거래가능·사유별 이력을 다시 계산하고 Excel로 내보내려면 다음 명령을 사용합니다.

```powershell
python scripts\export_nxt_eligibility_history.py --start-date 2025-03-04 --end-date 2026-08-08
```

## 주요 지표 정의

- `NXT 종목수`: 기준일의 NXT 매매체결대상 고유 종목 수. 거래불가 종목도 포함하며, 구형 원본에서 빠진 거래제한 종목은 지정·해제 변동으로 복원
- `KOSPI200·KOSDAQ150 종목수`: 기준일의 NXT 매매체결대상 종목 중 각 KRX 대표지수 구성종목에 해당하는 고유 종목 수
- `편입·편출 종목수`: 해당 일자의 실제 매매체결대상 선정 변동 수. 투자경고·투자위험·단기과열 지정/해제는 제외
- `NXT 종목수 증감`: 조회기간의 일별 편입 종목수 합계-편출 종목수 합계. 2025-03-04 조회 시 최초 10종목을 편입에 포함
- `변경사유`: 최초·단계별 확대·분기 정기변경·거래량한도관리·관리종목 지정·상장폐지 등 공식 공지와 원본 사유를 해석한 표시값
- `거래불가 지정·해제 종목수`: 전 거래일과 당일의 거래불가 상태 변화를 비교한 고유 종목 수. 구형 구간은 종목 변동내역으로 보완
- 거래정지·관리·환기·단기과열 상태 수: 해당 일자 NXT 원본의 `거래불가사유`별 종목 수
- 투자경고·투자위험 상태 수: KIND의 공식 지정일·해제일 기간을 해당 일자 NXT 종목과 교차한 고유 종목 수
- 당일공시 수: 해당 일자의 NXT 정규시장 거래현황 종목에서 각 분류의 공시가 발생한 고유 종목 수

원본 `편입/편출` 값은 감사 추적을 위해 그대로 보관합니다. 화면의 종목 변동내역에는 실제 편입·편출만 표시하며, 투자경고·투자위험·단기과열 지정·해제 등은 별도 거래불가 현황으로 표시합니다. 관리종목·투자주의환기·정기변경·거래량한도관리 등은 실제 편출로 유지합니다.

세부 품질 기준과 알려진 제한은 [docs/data_quality.md](docs/data_quality.md)를 참고하세요.
