# 프로젝트 개요와 시스템 아키텍처

> 목적 · 보유 하드웨어 · 이중 네트워크 전략 · 데이터 흐름과 레이트 설계

---

## 1. 프로젝트 개요와 하드웨어

### 1-1. 목적
전기 레이싱카(자작차)에 실시간 텔레메트리 시스템 구축. 운전자에게는 차량 내 계기판을,
팀에게는 원격지에서 차량 상태(위치·속도·배터리·G포스)를 실시간 모니터링 + 사후 정밀 분석
기능을 제공.

### 1-2. 현재 보유 하드웨어
- **Raspberry Pi Zero W** ×1 (현재 개발/검증기, ARMv6 싱글코어, 512MB) — 곧 이관 예정
- **Raspberry Pi 3B+** ×2 (쿼드코어 ARMv8 1.4GHz, 1GB) — 차량용으로 이관 예정
- **Raspberry Pi 4B (2GB)** ×2 — 기숙사 서버용으로 사용 예정
- **SIM7600G-H (B) LTE HAT** (Waveshare) — LTE + GPS 내장. USB 인터페이스(포고핀으로
  GPIO에서 전원·USB D+/D- 수급). 별도 Micro USB B 단자 있음(Pi 4 직결용).
- **USB-CAN 어댑터** (gs_usb / candleLight 계열, SocketCAN 네이티브) — Daly BMS 연결
- **배터리 팩** — **삼성 INR21700-50S** (고니켈 NMC, 5.0Ah/셀) **14S16P = 224셀**
  → 공칭 50.4V · 만충 58.8V · 방전차단 35.0V · 80Ah · **약 4.0kWh** (팩 상세는 7장)
- **Daly Smart BMS `R24TS1A-17S300A`** — 8~17S 대응(14S 사용) · **300A 연속** ·
  **1A 액티브 밸런싱** · Li-ion/LFP/LTO 선택 · RS485 + **CAN** + 블루투스 내장
- **5인치 DSI LCD** (알리익스프레스, 리본케이블 방식) — Pi 3B+/4B용 차량 계기판 (Zero 미지원)

### 1-3. 통신사/네트워크
- **LTE:** KT망, APN `lte.ktfwing.com`, 50GB USIM
- **VPS:** Oracle Cloud (OCI) Always Free, 서울 리전, `VM.Standard.E2.1.Micro`
  (AMD 1/8 OCPU, 1GB RAM), Ubuntu 24.04, 공인 IP `<OCI_PUBLIC_IP>`
- **계정명:** 모든 Pi가 `unimotors` 동일 (파일 경로 통일 목적, 문제 없음)

---

## 2. 전체 시스템 아키텍처

### 2-1. 이중 네트워크 전략
```
[서버] <---LTE(KT, wwan0)--- [차량 Pi] ---Hotspot(로컬)---> [운전자 휴대폰]
                                  |
                           SIM7600G-H HAT (LTE+GPS)
                                  |
                           USB-CAN --- Daly BMS
```
- **WAN (LTE):** 서버로 텔레메트리 전송 (아웃바운드). CGNAT 뒤라 인바운드는 VPN으로 해결.
- **LAN (핫스팟):** 차량 내부 로컬망. 운전자 폰이 계기판 접속. 인터넷 공유(NAT) 안 함.

### 2-2. 데이터 흐름 (3단계 완성형)
```
차량 Pi (gps_server.py)
  ├─ 10Hz raw NMEA   → 로컬 SD (gps_*.csv)          [정밀 궤적/랩분석 원본]
  ├─ 1Hz telem       → 로컬 SD (telem_*.csv)         [백업/보정]
  ├─ 2Hz 계기판       → 폰/LCD (SSE, 포트 8080)       [운전자 표시]
  └─ 2Hz 텔레메트리   → WS push (10.10.0.9:8090)  ┐
                                                    ↓ VPN
기숙사 Pi4 (telemetry_server.py)
  ├─ 메모리 latest/recent → /live 브로드캐스트
  ├─ SQLite summary (ts PK, 보정 대비)
  ├─ /upload → track (10Hz raw 주행후 업로드)
  └─ dashboard.html 서빙
                                                    ↓
                                        브라우저 (실시간 지도 + 주행 분석)
```

### 2-3. 3가지 데이터 레이트 (용도별 분리)
| 데이터 | 레이트 | 위치 | 용도 |
|---|---|---|---|
| raw NMEA | 10Hz | 로컬 SD | 정밀 궤적, 랩타임, G분석 (사후) |
| telem CSV | 1Hz | 로컬 SD | 백업, 실시간 구멍 보정 |
| 계기판 | 1~2Hz | 폰/LCD (SSE) | 운전자 실시간 표시 |
| 서버 push | 2Hz | WS → 서버 | 원격 실시간 모니터링 |

> **핵심 원칙:** 실시간 표시엔 2Hz면 충분(사람 눈). 10Hz는 사후 정밀 분석용으로
> 로컬에만 원본 보관. "버티니까 10Hz"가 아니라 "필요한 만큼만".

---

[문서 목차](README.md) · [라즈베리파이 초기 세팅 (헤드리스)](02-pi-setup.md) →
