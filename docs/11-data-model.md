# 데이터 모델 (32필드 스키마 · 시계 점프)

> TELEM_FIELDS · 누적 사용량 · DB 마이그레이션 · monotonic 시간축

---

## 20. 스키마 최종형 (32개 필드)

`gps_server.py`의 `TELEM_FIELDS`와 `telemetry_server.py`의 `FIELDS`는 **완전히 동일해야** 한다.
(불일치 시 CSV 컬럼 수가 안 맞아 backfill이 조용히 실패한다.)

```python
FIELDS = [
    "ts", "vehicle", "session", "t_mono",              # 식별/시각
    "lat", "lon", "alt", "speed", "heading",           # 위치·속도
    "sats", "fix", "hdop",                             # GPS 품질
    "g_lon", "g_lat",                                  # G-force
    "voltage", "current", "soc", "power_w", "regen",   # 배터리 순간값
    "remain_ah", "range_km", "state",
    "used_ah", "used_wh",                              # 배터리 누적 사용량
    "temp_max", "temp_min",
    "cell_v_max", "cell_v_min", "cell_v_diff",
    "balancing", "alarm_level", "alarms",
]
```

### 20-1. 새로 추가된 필드와 의미
| 필드 | 의미 | 출처 |
|---|---|---|
| `session` | 주행 단위 식별자 (`YYYYMMDD_HHMMSS`) | 프로세스 시작 시 생성, 시계 점프 시 갱신 |
| `t_mono` | 프로세스 시작 후 경과초 | `time.monotonic()`. **시계 점프에 영향 없음** |
| `alt` | 고도(m) | GGA 9번 필드 |
| `hdop` | 수평 정밀도 (1 이하 우수) | GGA 8번 필드 |
| `used_ah` / `used_wh` | 이번 주행 누적 사용량 | 전류·전력 시간적분 (회생 시 자동 차감) |

### 20-2. GGA 파싱 (고도·HDOP 추가)
```
$GPGGA,145521.00,3534.631528,N,12911.281770,E,1,06,0.9,143.2,M,24.0,M,,*6C
        ^시각      ^위도       ^N ^경도       ^E ^fix ^위성 ^HDOP ^고도
```
- 인덱스: `p[6]`=fix품질, `p[7]`=위성수, `p[8]`=HDOP, `p[9]`=고도, `p[10]`='M'
- **fix가 없으면 위경도·고도를 전부 None**으로 (오염 방지)

### 20-3. 배터리 누적 사용량 계산
```python
dt_h = (now - last_t) / 3600.0
if 0 < dt_h < 0.01:                      # 36초 이상 간격은 무시(재시작/멈춤)
    usage["used_ah"] += current * dt_h   # 방전 양수 / 회생 음수 → 순소비
    usage["used_wh"] += power_w * dt_h
```
- 회생제동은 전류가 음수라 **자동으로 차감**된다 (별도 처리 불필요)
- 전비 = 거리 ÷ `used_ah` (km/Ah), `used_wh` ÷ 거리 (Wh/km)

### 20-4. DB 마이그레이션 (기존 데이터 보존)
서버는 시작 시 `ALTER TABLE`로 컬럼을 자동 추가한다. 기존 데이터는 그대로 유지되고
새 컬럼만 NULL이 된다. 수동 작업 불필요.
```python
migrations = [("summary","session TEXT"), ("summary","t_mono REAL"),
              ("summary","alt REAL"), ("summary","hdop REAL"),
              ("summary","used_ah REAL"), ("summary","used_wh REAL"),
              ("track","t_mono REAL"), ("track","alt REAL"),
              ("track","soc REAL"), ("track","voltage REAL"),
              ("track","current REAL"), ("track","used_ah REAL")]
```
> `track` 테이블에 배터리 컬럼이 추가되어, **세션 궤적에서도 배터리를 볼 수 있다**
> (이전엔 GPS+G만 저장했음).

## 21. ⭐ 시계 점프 문제 (RTC 없는 Pi)

### 21-1. 증상과 원인
- **증상**: 콜드 부팅 후 주행하면 주행 시간이 몇 시간으로 뻥튀기됨
- **원인**: Pi에는 **RTC 배터리가 없다.** 부팅 시 시계가 "마지막 종료 시각"에서 시작
  (`fake-hwclock`)하고, 이후 NTP(LTE) 또는 GPS로 갱신되면서 **시계가 훌쩍 점프**한다.
  `마지막 ts − 첫 ts`로 시간을 계산하면 그 점프가 그대로 주행시간이 된다.
- **위험**: 대회에서 몇 시간 대기 후 주행하면 데이터 신뢰도가 무너짐

### 21-2. 해결 1 — monotonic 시간축 (근본 방어)
`t_mono`는 `time.monotonic()` 기반이라 **시스템 시계와 무관하고 절대 뒤로 가지 않는다.**
주행시간·데이터레이트 계산이 모두 이 값을 우선 사용한다(없으면 `ts`로 폴백).

### 21-3. 해결 2 — 점프 감지 후 세션 분리
monotonic 경과분과 wall clock 경과분을 비교해 점프를 감지한다.
```python
dm = now_monotonic - last_monotonic      # 실제 흐른 시간
dw = now_wall - last_wall                # 시계가 주장하는 시간
if abs(dw - dm) > 30.0:                  # 30초 이상 어긋나면 점프
    SESSION_ID = 새 시각으로 갱신         # 세션을 끊어 오염 구간 분리
```
로그 출력:
```
[CLOCK] 시계 점프 감지 (+10800s) -> 세션 분리 20260722_220000 -> 20260723_140000
```
→ 부팅 직후 오염 구간과 정상 주행 구간이 **다른 세션으로 분리**되어,
실제 주행 세션의 통계는 깨끗하게 유지된다.

### 21-4. 해결 3 — backfill 세션별 분할 업로드
한 CSV 파일에 세션이 2개 이상 섞이면(점프 발생), `session` 컬럼으로 그룹화해
**각각 별도 세션으로 업로드**한다.
```
[완료] telem_20260722_220000.csv [20260722_220000] -> car1/...: 수신 60행, 신규 60행
[완료] telem_20260722_220000.csv [20260723_140000] -> car1/...: 수신 120행, 신규 120행
  (시계 점프로 세션 2개로 분리됨)
```

### 21-5. 검증 결과
시계가 16시간 틀린 상태를 시뮬레이션한 결과:
| 항목 | 결과 |
|---|---|
| 세션 자동 분리 | 60행 + 120행 분리 ✓ |
| 세션별 주행시간 | 59초 / 119초 (정확) ✓ |
| 누적 주행시간 | **3.0분** ✓ (ts 기반이면 16시간+로 뻥튀기) |

### 21-6. 해결 4 — NTP 동기 (설정)
LTE가 있으면 대기 중 자동 동기되므로, 대회 시나리오는 대부분 이것으로 해결된다.
```bash
timedatectl                                  # "System clock synchronized: yes" 확인
sudo systemctl enable --now systemd-timesyncd # no 인 경우 활성화
```

### 21-7. 해결 5 — RTC 모듈 (근본 해결, 권장)
**DS3231** (I2C, 2~3천원). 코인 배터리로 전원이 꺼져도 시간을 유지하므로
콜드 부팅 즉시 정확한 시계로 시작한다. LTE가 없는 곳에서도 동작.
MPU6050(IMU)과 **같은 I2C 버스**에 물릴 수 있어 함께 구매 권장.
```bash
sudo raspi-config          # Interface Options → I2C 활성화
echo "dtoverlay=i2c-rtc,ds3231" | sudo tee -a /boot/firmware/config.txt
sudo reboot
# 확인
sudo hwclock -r            # RTC 시각 읽기
# fake-hwclock 제거 (RTC를 시간 소스로)
sudo apt remove fake-hwclock
sudo systemctl disable fake-hwclock
```

---

← [서버 인프라 실배포 (VPN · nginx · TLS)](10-infrastructure.md) · [문서 목차](README.md) · [주행 분석 · 랩 분석 · 배터리 분석](12-analysis.md) →
