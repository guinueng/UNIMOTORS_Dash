# 하드웨어 이관과 배포 체크리스트

> Zero W → 3B+/4B · 파일 배치 · 세팅 순서

---

## 10. 하드웨어 이관: Zero W → Pi 3B+ / 4B

### 10-1. 이관 배경
- Zero W(싱글코어 ARMv6)는 GPS10Hz+BMS+계기판+전송 동시 처리에 빠듯
- 5인치 DSI LCD가 Zero엔 커넥터 없어 불가 (3B+/4B는 DSI 있음)
- 레이싱용 고속 계기판(10Hz 표시) 위해 여유 필요

### 10-2. 이관 배치
| 역할 | 기계 | 이유 |
|---|---|---|
| 차량 | Pi 3B+ | 쿼드코어 충분, DSI LCD, LTE HAT Micro USB 직결 |
| 서버 | Pi 4B 2GB | 대시보드+DB+지도, RAM 여유 |

### 10-3. 이관 시 코드 변경 (거의 없음)
- `bms_reader.py`, `telemetry_server.py`, `dashboard.html`: **그대로**
- `gps_server.py`: **레이트 상수만** 조정 (구조 변경 X)
  - `DISPLAY_RATE` 1.0 → 0.1~0.2 (부드러운 계기판)
  - `SSE_RATE`, `UPLOAD_RATE`, BMS 주기 상향 가능
- **Zero 최적화(스레드 분리, 직접 파싱, flush)는 3B+에서도 유익 → 유지**
- asyncio 재작성 불필요 (스레드가 쿼드코어에 오히려 적합)

### 10-4. 이관 물리 연결 (USB로 통일 — Zero의 OTG 허브 지옥 해방)
- **LTE HAT:** GPIO에 얹지 말고 **Micro USB B → Pi USB 직결** (폼팩터 불일치 무관)
- **USB-CAN:** USB 직결 (gs_usb 자동인식)
- **GPS:** SIM7600 USB 복합포트 → 이관 후 `ls /dev/ttyUSB*`로 **번호 재확인**
  (ttyUSB1이 바뀔 수 있음. 필요 시 udev 규칙으로 고정)

### 10-5. DSI LCD 설정 (미완 — 모델 확인 필요)
- 알리 5인치 DSI는 공식 Pi LCD가 아니라 자체 오버레이 필요할 수 있음
- 뒷면 모델명/터치칩(GT911 등)/주문링크 확인 후 config.txt 설정
- 삽질 시 대안: Pi 4 micro-HDMI로 임시 표시 가능
- 로컬 표시 방법: Chromium 키오스크로 `localhost:8080` 풀스크린

### 10-6. 기타 이관 세팅
- **hostname 구분:** `sudo hostnamectl set-hostname unimotors-car` / `-srv` (계정명은 unimotors 유지)
- **NTP 동기화:** `systemd-timesyncd` 켜두기 (ts 정확도 = backfill 정밀도)

---

## 11. 파일 목록과 배포 체크리스트

### 11-1. 차량 Pi (3B+) 파일
```
/home/unimotors/
├── gps_server.py          (v6: GPS+BMS+G+uploader, 프리셋+레이트 상수화)
├── bms_reader.py          (Daly CAN 파서)
├── backfill.py            (주행후 telem CSV 서버 업로드/보정)
├── lte_auto.sh            (LTE 자동연결)
└── gps_logs/
    ├── gps_*.csv          (10Hz raw NMEA)
    └── telem_*.csv        (1Hz 통합 스냅샷)
```
설치:
```bash
pip3 install pyserial python-can websocket-client --break-system-packages
```
차량 종류 선택 (파일 상단 PRESET 또는 환경변수):
```bash
UNIMOTORS_PRESET=car1 python3 gps_server.py      # 레이싱카
UNIMOTORS_PRESET=starex python3 gps_server.py    # 개인차(스타렉스)
```

### 11-2. 서버 Pi4 파일
```
/home/unimotors/
├── telemetry_server.py    (aiohttp WS + SQLite)
├── dashboard.html         (Leaflet 대시보드)
└── telemetry.db           (자동 생성)
```
설치:
```bash
pip3 install aiohttp --break-system-packages
```

### 11-3. 집 가서 세팅 순서
1. 3B+ 부팅 + hostname(unimotors-car) + WireGuard 피어(10.10.0.3)
2. LTE HAT Micro USB 직결 + GPS 포트 재확인
3. gps_server.py + bms_reader.py 복사 + 레이트 상수 조정
4. CAN 자동 up (udev 규칙 또는 can0.service — 8-3)
5. Pi4 서버 세팅 + WireGuard 피어(10.10.0.9) 추가
6. telemetry_server.py + dashboard.html 복사, aiohttp 설치
7. **양쪽 토큰 일치** 확인
8. 파이프라인 전체 테스트 (배터리 없이 GPS만으로도 지도+계기판 검증 가능)
9. (배터리 있는 곳에서) CAN 꽂으면 배터리 데이터 자동 표시

---

← [텔레메트리 서버 + 실시간 대시보드](08-telemetry-server.md) · [문서 목차](README.md) · [서버 인프라 실배포 (VPN · nginx · TLS)](10-infrastructure.md) →
