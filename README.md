# NXT 시장조치 모니터

KIND의 시장조치 공시와 넥스트레이드(NXT)의 날짜별 정규시장 거래현황 및 편입·편출 내역을 결합해 조회하는 Streamlit 앱입니다.

## 웹 접속

- [NXT 시장조치 모니터](https://nxt-market-monitor.streamlit.app/)

## 화면

1. **KRX 시장조치 조회**
   - 시작일·종료일 조회
   - 전체 또는 8개 분류별 조회
   - 공시일의 NXT 정규시장 거래현황에 포함된 종목만 표시
   - `NXT 거래가능시장`, `NXT 거래불가사유`를 NXT 원본 값으로 표시
   - KIND 원문 링크와 CSV/Excel 다운로드
2. **일자별 NXT종목 현황**
   - 선택일의 NXT 정규시장 거래현황 종목을 직접 조회
   - `거래가능시장`, `거래불가사유`를 NXT 원본 값으로 표시
   - KOSPI/KOSDAQ 시장 정보 표시
   - 해당 일자의 편입·편출 변동내역 표시
3. **NXT종목 변동내역**
   - 당일 편입·편출 종목수와 종목별 변동내역

`KRX 시장조치` 집계 화면의 구현은 유지하지만 현재 탐색 메뉴에서는 숨겨져 있습니다.
차트 구현도 유지하지만 현재 모든 화면에서 숨겨져 있으며, 브라우저의 라이트·다크 테마를 따릅니다.

## 설치 및 실행

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run app.py
```

앱은 `data/cache.db`에 공식 사이트 응답을 캐시합니다. 화면의 **원본 데이터 새로고침** 버튼으로 캐시를 건너뛸 수 있습니다.

## 데이터 출처

- [KIND 상세검색](https://kind.krx.co.kr/disclosure/details.do?method=searchDetailsMain)
- [NXT 정규시장 거래현황](https://www.nextrade.co.kr/menu/transactionStatusMain/menuList.do)
- [NXT 매매체결종목 변동내역](https://nextrade.co.kr/menu/transactionStatusConclusion/menuList.do)

NXT 시장 개설일인 2025-03-04 이후를 지원합니다. KIND와 NXT가 공개한 웹 응답 형식이 변경되면 파서 수정이 필요할 수 있습니다.

## 주요 지표 정의

- `NXT 종목수`: 기준일의 NXT 정규시장 거래현황에 포함된 고유 종목 수. `거래불가` 종목도 공식 현황에 포함돼 있으면 집계
- `당일 편입·편출 종목수`: 해당 일자에 각 변동이 발생한 고유 종목 수
- 거래정지·관리·환기·단기과열 상태 수: 해당 일자 NXT 원본의 `거래불가사유`별 종목 수
- 투자경고·투자위험 상태 수: KIND의 공식 지정일·해제일 기간을 해당 일자 NXT 종목과 교차한 고유 종목 수
- 당일공시 수: 해당 일자의 NXT 정규시장 거래현황 종목에서 각 분류의 공시가 발생한 고유 종목 수

NXT 대상 여부는 편입·편출 이력으로 추정하지 않습니다. 각 날짜의 공식 거래현황에 종목코드가 실제로 존재하는지를 기준으로 모든 공시 필터와 KRX 시장조치 집계를 수행하며, 과거 시장조치 공시를 현재 상태로 무기한 이월하지 않습니다.

세부 품질 기준과 알려진 제한은 [docs/data_quality.md](docs/data_quality.md)를 참고하세요.
