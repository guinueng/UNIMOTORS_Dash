# 텔레메트리 서버 + 실시간 대시보드

> 멀티차량 · 3층 저장구조 · API · backfill

---

## 9. 7단계: 텔레메트리 서버 + 실시간 대시보드 (멀티 차량)

> ⚠️ 이 장의 스키마·API는 이후 III부(20~22장)에서 확장되었다.
> **최종 스키마(32필드), 고도·배터리 사용량, 세션별 분석은 20~22장 참고.**

### 9-0. 멀티 차량 지원 (v2)
- **차량 구분:** 스냅샷의 `vehicle` 필드로 서버가 자동 구분. `latest/recent/last_seen`을
  차량별 dict로 관리.
- **대시보드 표시:** 한 번에 1대만 표시. 온라인 차량이 2대 이상이면 헤더에 **선택 박스**
  자동 표시, 1대면 자동 선택(박스 숨김).
- **차량 프리셋 (gps_server.py 상단 `PRESET`):**
  | 프리셋 | VEHICLE_ID | BMS | 서버전송 | 용도 |
  |---|---|---|---|---|
  | `car1` | car1 | O | O | 레이싱카 |
  | `starex` | starex | X | O | 개인차(배터리 없음) |
  | `car_personal2` | car_personal2 | X | O | 개인차 추가 템플릿 |
  | `local_only` | local | X | X | 순수 로컬 로거(전송X) |
  - 환경변수로도: `UNIMOTORS_PRESET=starex python3 gps_server.py`
  - 배터리 없는 차는 스냅샷 배터리 필드가 전부 null → 대시보드에서 자동 `--` 표시
- **폰 임시 추적 (미래):** `vehicle="phone-*"`로 시작하면 서버가 **DB 저장 제외**
  (개인정보), 실시간 지도에만 표시. 브라우저 Geolocation/DeviceMotion으로 위치·G 전송
  가능. HTTPS(보안 컨텍스트) 필요. 서버 훅(`is_ephemeral`)은 이미 구현됨.

### 9-1. 아키텍처 결정
- **차량 = Pi 3B+** (쿼드코어면 수집+계기판 충분, DSI LCD 지원)
- **서버 = 기숙사 Pi4 2GB** (대시보드+DB+지도, RAM 여유)
- **OCI = VPN 허브 전용** (1GB라 무거운 스택 부적합, 라우팅만)
- **프로토콜 = WebSocket** (VPN 내부 평문, TLS 불필요 — WireGuard가 이미 암호화)
- **저장 = SQLite** (경량, 별도 프로세스 0)
- **지도 = Leaflet + OpenStreetMap(CARTO 다크 타일)** (무료, API키 불필요)

### 9-2. 서버 저장 3층 구조
```
① 실시간 표시 : 서버 메모리 (latest + recent deque) → 브라우저 WS. DB 안 거침.
② 세션 저장   : SQLite summary 테이블 (2Hz, ts PRIMARY KEY)
③ 정밀 원본   : SQLite track 테이블 (10Hz raw, 주행후 업로드)
```
> temporary view는 RAM 캐싱이 아님(저장된 쿼리일 뿐). 메모리 변수 + SQLite INSERT 병행이 정답.

### 9-3. 실시간 구멍 보정 (backfill)
- LTE 음영(터널 등)으로 실시간 스트림에 구멍 발생 → 로컬 telem CSV로 사후 메꿈
- **summary.ts = PRIMARY KEY + INSERT OR IGNORE** → 중복 무시, 구멍만 채워짐 (검증 완료)
- 소스: telem CSV(1Hz, 서버 스키마와 동일). 트리거: 수동 backfill 스크립트(추후) → WiFi 자동

### 9-4. telemetry_server.py (aiohttp + asyncio, v2 멀티차량, 완성)
- `WS /ingest` — 차량/폰 push (토큰 인증). `vehicle`로 구분 → 메모리+DB+브로드캐스트
  (phone-*는 DB 저장 제외)
- `WS /live` — 브라우저 (접속 시 온라인 차량목록 + 각 차량 latest/recent 초기전송)
- `POST /upload?session=&vehicle=` — 10Hz raw CSV → track (세션별)
- `GET /api/vehicles` — 기록/온라인 차량 목록
- `GET /api/sessions?vehicle=` — 차량의 날짜/세션 목록
- `GET /api/history?date=&vehicle=` / `/api/track?session=` — 조회
- `GET /api/cumulative?vehicle=` — 전체 궤적 + 누적 통계
- `GET /` — dashboard.html 서빙
- SQLite `run_in_executor` 비블로킹, 오프라인 감시(차량별 600초)
```bash
pip3 install aiohttp --break-system-packages
python3 telemetry_server.py    # :8090
```

### 9-5. dashboard.html (v2 멀티차량, 완성)
**실시간 탭:** Leaflet 지도 + 속도/헤딩/위성 + G-G 다이어그램 + 배터리 전체 + 알람 배너.
차량 2대+면 헤더 선택박스로 전환(1대면 자동).
**주행 분석 탭:** 차량 선택 → [전체(누적) / 궤적 세션 / 요약 날짜] 계층
- 개별 세션: 10Hz 궤적 + 속도/종G/횡G 스파크라인 + 요약(최고속도/가속·제동·코너G)
- **전체(누적):** 전체 궤적 겹침 + 통계(총거리/주행횟수/최고속도/**평속**/**최대G**/주행시간)

**UNIST/영광 지도 로직 (중요):**
- GPS fix 없으면 → 지도 배경만 **UNIST(35.572, 129.189)**, 궤적 안 그림
- 첫 실제 좌표(fix + 유효 위경도) → 그때부터 지도 이동 + 궤적 누적 시작
- 대회장(영광) 도착해 fix 잡히면 자동으로 영광으로 이동. UNIST는 시작점 아님, 기본 배경일 뿐
- **차량 신호 없음(10분):** "차량 신호 없음" 오버레이. 차량 전환 시 궤적 리셋

### 9-5b. backfill.py (실시간 구멍 보정 + 10Hz 정밀 업로드, 완성)
주행 후 로컬 telem CSV를 서버 `/upload`로 전송. 표준 라이브러리만 사용(설치 불필요).
```bash
python3 backfill.py ~/gps_logs/telem_20260721_140134.csv   # 단일
python3 backfill.py --all                                   # gps_logs 전체
python3 backfill.py telem_x.csv --session race1 --vehicle car1
```
- session 미지정 → 파일명에서 자동 생성 / vehicle 미지정 → CSV에서 자동 추출
- **중복 무시 검증됨:** 같은 파일 재업로드 시 inserted=0 (summary.ts PRIMARY KEY 덕).
  여러 번 올려도 데이터 안 꼬임, 빠진 것만 채워짐.
- 서버 주소 기본 `http://10.10.0.9:8090` (VPN). `--server`로 변경 가능.

### 9-6. G-force (GPS 기반, IMU 자리 열어둠)
- 종G = 속도 미분, 횡G = 속도 × 방향변화율 (검증 완료)
- GPS 기반이라 근사. `_calc_gforce`만 나중에 IMU 값으로 교체
- **IMU 추가 예정:** MPU6050 (I2C, ~2천원). 정밀 G + 자세 + GPS 센서융합.
  현재 미보유, 구매 예정.

### 9-7. 상수 일치 필수 (차량 ↔ 서버)
| 항목 | 차량 gps_server.py | 서버 telemetry_server.py |
|---|---|---|
| 토큰 | `UPLOAD_TOKEN` | `AUTH_TOKEN` (동일해야 함) |
| 주소 | `SERVER_WS_URL=ws://10.10.0.9:8090/ingest` | 포트 8090 |
| 차량ID | `VEHICLE_ID=car1` | (다중차량 대비) |

---

← [Daly BMS CAN 통신](07-bms-can.md) · [문서 목차](README.md) · [하드웨어 이관과 배포 체크리스트](09-hardware-migration.md) →
