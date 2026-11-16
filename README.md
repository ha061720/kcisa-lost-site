# 다시찾음 — KCISA 유실물 검색 웹사이트

## 들어 있는 것
- `index.html`: 검색 화면 (물품명, 날짜, 장소, 상세정보, CSV)
- `data.json`: 사용자가 제공한 KCISA XML 공지에서 추출한 실제 물품 데이터
- `source.xml`: 사용자가 제공한 원본 XML
- `parse_data.py`: 공지의 HTML 표를 개별 유실물로 추출
- `server.py`: 웹서버 + KCISA 실시간 갱신 요청

## 실행 (Windows)
1. Python 설치
2. 이 폴더에서 PowerShell 실행
3. `pip install -r requirements.txt`
4. `python server.py`
5. 브라우저에서 `http://127.0.0.1:5000` 열기

## KCISA API로 최신 데이터 갱신
PowerShell에서 서버 실행 전에:
`$env:KCISA_SERVICE_KEY="발급받은_API_키"`
그 다음 `python server.py` 실행 후 웹 화면의 'API 새로고침' 클릭.

## 주의
- 제공받은 XML은 2024년 등의 과거 공지입니다. 현재 보관 상태를 의미하지 않습니다.
- 실제 API 필드의 `description` 내부에 HTML 표가 있어 이를 별도 추출했습니다.
- API 인증키는 서버 환경변수로 관리하며 웹 브라우저에 노출하지 않습니다.
- 갱신은 1페이지 최대 100개 공지를 대상으로 하며 전체 페이지 자동 수집은 아직 지원하지 않습니다.
- 데이터 제공 기관의 이용약관과 출처 표시 기준을 확인하세요.
