# 현재 상태와 남은 작업

> 완료 항목 · 진행 중 · TODO

---

## 13. 남은 작업 (TODO)

> ⚠️ 이 장은 II부 작성 시점의 스냅샷이다.
> **최신 상태는 문서 맨 끝 "최종 상태 요약"을 볼 것.**

### 완료 ✅ (I부: 차량/서버 코드 + VPN 기초)
- LTE 통신 (KT, Raw IP + udhcpc)
- GPS 10Hz 계기판 (버퍼 적체 해결)
- 로컬 핫스팟 + 캡티브 포털
- WireGuard VPN (PC + 차량 Pi)
- Daly BMS CAN 통신 (0x90~0x98 전체 검증)
- bms_reader.py (파서 모듈)
- gps_server.py v6 (GPS+BMS+G-force+uploader+로컬 로깅+차량 프리셋)
- telemetry_server.py v2 (멀티차량 WS + SQLite + 누적 통계 API)
- dashboard.html v2 (실시간 멀티차량 선택 + 분석 탭 차량/세션/누적 + 스크러버)
- backfill.py (주행후 CSV 업로드/보정, 중복무시 검증)

### 완료 ✅ (II부: 이번 세션 — 서버 인프라 실배포. 14~18장 참고)
- WireGuard 메시 확장 (서버 Pi 10.10.0.9, 내부망 10.10.0.11 피어 추가)
- 폰 WireGuard 연결 (풀 터널 + OCI MASQUERADE로 외부 인터넷)
- Pi 4B 서버 **실배포** (Debian 13 trixie, VPN + telemetry_server + systemd)
- OCI nginx 리버스 프록시 3서브도메인 (unimotors./data./dash.)
- 와일드카드 TLS 인증서 (`*.unimotors.example.com`, Cloudflare DNS 플러그인)
- basic auth 보호 + 랜딩/포털 웹페이지
- **전체 파이프라인 실차 1차 검증** (차량→VPN→서버→도메인→대시보드, GPS fix까지)

### 진행/예정 ⬜
- [x] ⭐ **차량 코드 IP 수정 완료** — `gps_server.py`, `backfill.py` 모두 `10.10.0.9` 반영됨
- [ ] **dash SSE 버퍼링** — nginx `proxy_buffering off` 추가함, 실험 대기 (17장)
- [ ] **궤적 노이즈 개선** — CGNSSMODE=15(위성↑) + 좌표 필터(정지 스킵/최소이동거리) (18장)
- [ ] **하드웨어 이관** — Zero W → 3B+ (차량). 서버 Pi4는 완료
- [x] **CAN 자동 up 적용** — `can0.service`(방법 B)로 적용 완료. 핫플러그·재부팅 모두 검증됨
- [ ] **5인치 DSI LCD** — 모델 확인 + config.txt 오버레이 + Chromium 키오스크
- [ ] **IMU 추가** — MPU6050(I2C) 구매 후 정밀 G-force + 센서융합
- [ ] **폰 임시 추적 페이지** — /tracker (Geolocation→WS, HTTPS 준비됨, 서버 훅 있음)
- [ ] **랜딩 페이지 꾸미기** — 로고, 팀 소개, 대회 결과(대회 후 아카이브)
- [ ] **삭제 기능** — 테스트 데이터 정리 (DELETE FROM 또는 /api/purge)
- [ ] **OCI 재부팅 후 iptables/MASQUERADE 유지 확인** (커널 업그레이드 대기 중)
- [ ] **주행 분석 고도화** — 랩타임 자동 감지, 여러 랩 비교, 코너 라인 분석

---


## 최종 상태 요약

### 완성 ✅
- LTE(KT, Raw IP+udhcpc) / GPS 10Hz(버퍼 적체 해결) / 핫스팟+캡티브포털
- Daly BMS CAN 0x90~0x98 전체 검증 + 파서
- WireGuard 메시 4피어 (OCI 허브 + 차량 + 서버 + 클라이언트) + 폰 풀터널
- 차량 수집기(프리셋/G-force/업로더/로컬로깅) + 서버(멀티차량/누적/세션) + 대시보드
- OCI nginx 리버스 프록시 3서브도메인 + 와일드카드 TLS + basic auth
- 랜딩/포털 웹페이지
- Pi4 서버 실배포 (VPN + systemd + 도메인 연동)
- 실차 1차 주행 검증 (GPS fix → 실시간 지도/속도/궤적 확인)
- 고도·배터리 사용량·전비·누적 상승고도
- 주행 분석: 세션별 목록 / 궤적 그라데이션 / 확대·호버 / 비교 그래프 / 스크러버
- 시계 점프 대응 (t_mono + 세션 분리 + backfill 분할)

### 이번에 추가된 것 (문서 V5)
- **충전 세션 지원** — 세션 유형 자동 판별(주행/충전/정차), 충전 통계(SOC 변화·충전량·
  완충 예상), `used_ah` MAX 집계 버그 수정 (22-B-2b)
- **배터리 단독 분석 모드** — [주행/배터리] 토글, 온도(최고·최저 2시리즈)·셀편차 차트 추가
- **CAN 자동 up 완료** — `can0.service`, systemd-networkd 권장 철회(NetworkManager 충돌)
- **랩 분석** — 출발선 통과 감지(선분 교차+방향판정+보간+디바운스), 랩별 시간·거리·속도·G·Ah·Wh/km,
  베스트랩 강조, 랩 클릭 시 구간 확대, 출발선 서버 저장 (22-B-1~2)
- **데이터 삭제 UI** — 4중 안전장치(대상 미리보기 → 이름 재입력 → 서버 confirm → 서버 verify) (22-B-3)
- **대회 수집 권장 데이터** — 1~3순위 + 센서 없이 계산 가능한 파생 지표 (22-B-4)

### 이전에 추가/해결된 것 (V4)
- **SSE 프록시 문제 해결** — `protocol_version="HTTP/1.1"` + chunked + `X-Accel-Buffering: no`.
  실제 nginx로 재현·검증 완료 (23-2). `proxy_buffering off`만으로는 해결되지 않음
- **캡티브 포털 정확한 진단** — 포트 80 + DNS 때문에 코드에 도달조차 못 하던 문제,
  `CAPTIVE_MODE`(portal/success/off) 도입 (6-2)
- **DSI 키오스크 구성** — 계기판을 Pi 내장 화면에 띄워 폰을 해방 (24-12) ★권장
- **Zero W 헤드리스 복구 전체 이력** — `regdom=GB` 근본 원인, USB OTG,
  LTE HAT의 USB 라인 점유, Windows RNDIS 수동 설치 (27-1)

### 남은 작업 ⬜
- [ ] **DSI LCD 모델 확인 → 24-12 키오스크 구성 적용** (최우선 — 폰 해방)
- [ ] `TELEM_LOG_RATE` 0.1로 낮춰 진짜 10Hz 정밀 궤적 확보 (22-3)
- [ ] 새 `gps_server.py` 배포 후 dash 도메인에서 계기판 실시간 확인 (23-2)
- [ ] `CGNSSMODE=15` 적용 후 실주행에서 위성 수·궤적 개선 확인
- [ ] 좌표 필터 (정지 스킵 + 최소 이동거리) — 실주행 결과 보고 판단
- [ ] 하드웨어 이관 Zero W → 3B+ (차량)
- [ ] IMU (MPU6050) + RTC (DS3231) 추가 — 같은 I2C 버스
- [ ] 폰 임시 추적 페이지 `/tracker` (Geolocation→WS, 서버 훅 준비됨)
- [ ] 랜딩 페이지 로고·팀 소개·대회 결과 아카이브
- [ ] 테스트 데이터 삭제 기능 (`/api/purge` 또는 SQL)
- [ ] OCI 재부팅 후 iptables/MASQUERADE 영속성 확인
- [ ] 섹터 타임 (출발선처럼 중간 라인 2~3개 추가)
- [ ] 회생 회수율·코스팅 비율 등 파생 지표 화면화 (22-B-4)
- [ ] 스로틀 개도 센서(ADS1115) + 브레이크 신호 → 코스팅 분석

---



---

← [레퍼런스 (명령 · 경로 · 백업 · 설계근거)](17-reference.md) · [문서 목차](README.md)
