# 37. 계기판 데모 Pi (3B+, 가상 주행)

> Pi 3B+ + 5인치 DSI LCD 로 가상 주행 계기판을 띄우고, 기존 서버 파이프라인(VPN → 서버 Pi4 →
> `data.` 도메인)으로 태블릿에서 실시간 연동을 시연하는 데모 기기의 세팅 기록 (2026-10-29).
> 코드: [`vehicle/demo_sim.py`](../vehicle/demo_sim.py) · 설치: [`scripts/install-pi.sh`](../scripts/install-pi.sh)
>
> `demo_sim.py` v2.1은 **v7(RaceRuntime)** 가상 센서 모드(37-5-1)를 지원한다.
> 설치 스크립트 v3.1은 공통 모듈·운전자 HTML을 함께 받고, 임시 폴더에서 v7 import와 계산을 확인한 뒤 설치한다.
> v6 직접 실행의 호환 경로는 유지하지만 새 설치 대상은 v7이다.
>
> 2026-11-01 추가: 설치 파일에 `claude_dashboard.html`도 포함한다. 화면에서 v7 연구형 / Claude 클래식을 선택하며 `/legacy`도 새 클래식 테마를 연다. [Project CARS 수신 및 테마 안내](24-pcars-and-themes.md).

---

## 37-1. 목적과 구성

실제 차량·배터리 없이 **계기판 + 원격 대시보드 연동**을 보여주기 위한 데모 기기.
실차와 **같은 코드 경로**를 그대로 쓰고, GPS·BMS 입력만 가상 차량으로 바꿨다.

```
데모 Pi 3B+ (Unimotors-Demo, VPN 10.10.0.4)
  demo_sim.py ── import ── gps_server.py (계기판 HTML · SSE · CSV · 업로더 그대로)
    ├─ LCD     : Chromium 키오스크 → localhost:8080  (+ 랩 오버레이, DEMO 배지)
    └─ 업로더  : ws://10.10.0.9:8090/ingest  (vehicle="demo", v7 표본 ID·outbox·ACK batch)
                         ↓ WireGuard
기숙사 Pi4 telemetry_server → data.unimotors.example.com → 태블릿 (실시간 지도 · 랩 분석)
```

- `gps_server.py` **원본은 수정하지 않는다.** `demo_sim.py`가 import 후 설정 상수만 덮어쓴다.
- 서버는 `vehicle` 별로 세션·누적·랩 분석을 조회하므로 `demo` 데이터가 `car1` 통계에 섞이지 않는다.
  정리할 때는 분석 탭 **[이 차량 전체 삭제]** 로 `demo` 만 지운다 (22-B-3).

## 37-2. 하드웨어 · OS

| 항목 | 값 |
|---|---|
| 보드 | Raspberry Pi 3B+ (1GB) |
| 화면 | 5인치 DSI LCD (알리) — **Imager 기본 이미지에서 오버레이 없이 자동 인식됨** |
| OS | Raspberry Pi OS 64-bit **데스크톱** (Debian 13 trixie, Wayland / labwc) |
| 호스트명 | `Unimotors-Demo` |
| 계정 | **`unimotors-demo`** ⚠️ 다른 Pi(`unimotors`)와 다름 → 경로가 `/home/unimotors-demo/` |
| 홈 폴더 파일 | `demo_sim.py`, `gps_server.py` (v7: + `race_runtime.py`, `driver_dashboard.html`, `claude_dashboard.html`, `telemetry_protocol.py`) — 같은 폴더 필수 |

> ⚠️ **10-5 / 24-12-1 "DSI LCD 모델 확인 필요" 항목 해소**: 이 LCD는 trixie 기본 설정
> (`display_auto_detect=1`)로 바로 잡힌다. 차량 Pi(3B+) 이관 시에도 같은 이미지를 쓰면 된다.

## 37-3. WireGuard 피어 (10.10.0.4)

- 데모 Pi = **10.10.0.4**. (14-2에서 "초기 서버 계획 IP"였던 주소. 서버는 실제로 .9 를 쓰므로 충돌 없음)
- 키는 Pi에서 생성(15-1 방식 B). OCI 쪽 등록은 **재시작 대신 syncconf** — 다른 피어 연결이 끊기지 않는다.

```bash
# OCI: /etc/wireguard/wg0.conf 에 [Peer] 추가 (AllowedIPs = 10.10.0.4/32) 후
sudo bash -c 'wg syncconf wg0 <(wg-quick strip wg0)'
```
Pi `wg0.conf`는 24-9와 동일 구조(Address=10.10.0.4/24, AllowedIPs=10.10.0.0/24, Keepalive=25).

**14-1 메시 갱신**
```
OCI 10.10.0.1 ├─ .2 PC ├─ .3 차량 Pi ├─ .4 데모 Pi (Unimotors-Demo) ★신규 ├─ .9 서버 Pi4 └─ .11 폰
```

PC 접속: `ssh unimotors-demo@10.10.0.4` (별칭을 쓰려면 `~/.ssh/config`에 `Host demo` 등록)

## 37-4. ⭐ APT 다운로드 실패 — HTTPS 전환으로 해결

**증상**: `apt full-upgrade` 중 `Tried to start delayed item … but failed` 경고 →
`Unable to connect to deb.debian.org:http [IP: 151.101.2.132 80]` 에러.
ping·curl 은 되는데 **HTTP(80) 대용량 다운로드가 중간에 계속 끊김.**

**해결**: 공식 저장소를 **HTTPS** 로 전환 (trixie apt 는 HTTPS 기본 지원).
```bash
sudo cp /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list.d/debian.sources.bak
sudo sed -i 's|http://deb.debian.org/|https://deb.debian.org/|g' /etc/apt/sources.list.d/debian.sources
sudo apt update
```

**실패한 시도 (반복 금지)**
| 시도 | 결과 |
|---|---|
| 카카오 미러 `mirror.kakao.com/debian` | trixie arm64 `Packages` **404** → 패키지 목록이 비어 `Unable to locate package` |
| `sed 's|http://deb.debian.org/debian|…|'` | 패턴이 **`debian-security` 앞부분까지 매칭** → 보안 저장소가 미러로 바뀌어 `Clearsigned file isn't valid, got 'NOSPLIT'` |

> 교훈: 저장소 주소를 바꿀 때 패턴은 **`http://deb.debian.org/` (슬래시까지)** 로 잡아
> `debian` / `debian-security` 가 각각 올바르게 바뀌게 한다.
> 목록이 깨진 상태에서 업그레이드하면 의존성이 맞지 않아 패키지가 제거될 수 있다(이번엔 `vlc` 메타패키지).

데모에 필요한 패키지: `python3-websocket` (업로더). pyserial·python-can 은 불필요(데모는 시리얼/CAN 미사용).

## 37-5. demo_sim.py (v1)

### 설계
- **실도로 폐루프 코스**: UNIST 캠퍼스 순환 약 **2.6 km** (OSM 도로, OSRM 경로).
  출발선(유니스트길 교차점) → 고래 다리 → 사연길 → 곡연1길 → 사연길 → 유니스트길 → 출발선.
  막다른 길 왕복 구간은 자동 제거(`clean_ring`).
- **모든 값이 하나의 물리 모델에서 나온다** (서로 어긋나지 않음):

| 값 | 계산 |
|---|---|
| 속도 | 곡률 한계(횡 0.46G) + 가속·제동 한계 + 모터 출력 한계로 만든 프로파일 추종. 직각 교차로 ~20 km/h, 직선 55 km/h |
| G | 종G = 실제 가속도, 횡G = v²·곡률 (우회전 +) → G-G 점이 코너 방향과 일치 |
| 배터리 | 구름·공기·경사·가속 저항 → 모터 출력 → 효율 → 팩 전력 → NMC 14S OCV − I·R → 전압·전류 → 쿨롱카운팅 SOC·잔여Ah·사용Ah/Wh, 회생(전류 음수), I²R 발열 온도, 전류 비례 셀 편차 |
| 고도 | **합성값**(북쪽이 높은 완만한 경사 40~54 m) — 실측 아님 |
| 랩 | 출발선 통과 시 증가. 랩마다 페이스 −0.4%(최대 8%) + ±1.2% 랜덤 → 랩타임이 흔들리며 점점 늘어남 |

- 배터리 사양은 정정 문서 기준: 삼성 SDI 50S **NMC 14S16P 80Ah**.
- SOC 10% 이하로 랩을 마치면 "배터리 교체"로 처리하고 **새 세션**을 시작한다.
- LCD 에 **LAP · 현재 랩타임 · LAST · BEST · DEMO 배지** 오버레이를 주입한다
  (`gps_server.DASHBOARD_HTML` 을 메모리에서만 패치).
- 서버/CSV로 나가는 `used_ah/used_wh` 는 시뮬레이터 정밀값으로 덮어써 샘플링 오차를 없앤다.

### 검증 (dry-run, Pi 실측)
```
랩  시간    거리km  평속  최고  maxG   Wh    Wh/km  SOC%  온도
1  3:07.8  2.601  49.9  55.0  0.56  116.0  44.6  82.3  25.9
2  3:06.8  2.601  50.1  54.7  0.57  113.6  43.7  79.6  26.9
3  3:09.9  2.601  49.3  53.8  0.55  110.4  42.4  77.0  27.7
검증: 적분거리 = 차량거리, 사용 Ah = SOC 감소분 (일치)
```
→ 랩당 SOC 약 2.7% 감소, 85%에서 시작하면 약 1시간 반 연속 주행.

### 사용법
```bash
python3 ~/demo_sim.py --dry-run 3        # 즉시 계산·검증
python3 ~/demo_sim.py                    # 실시간 (LCD + 서버 전송)
python3 ~/demo_sim.py --no-upload        # 서버 전송 없이 LCD 만
python3 ~/demo_sim.py --fade 0           # 랩타임 일정하게
python3 ~/demo_sim.py --osrm "lat,lon;lat,lon;…"   # 다른 코스 생성 (인터넷 필요)
```
주요 옵션: `--soc` 시작 SOC, `--soc-floor` 교체 기준, `--ambient` 외기온도, `--gps-noise` GPS 노이즈(m),
`--start-wait` 출발 전 대기, `--vehicle` 서버 표시 ID, `--server` 업로드 주소.
서버 토큰을 바꿨다면 `UNIMOTORS_TOKEN` 환경변수(또는 `/etc/default/unimotors`).

### 37-5-1. v2 — v7(RaceRuntime) 가상 센서 모드

v7 에서는 계기판 값을 `RaceRuntime` 이 계산하므로, demo_sim 은 **상태를 직접 쓰지 않고 센서 원본만 넣는다.**
그래서 랩 판정·에너지 적분·예산·경고·CSV·ACK 전송이 실차와 같은 코드를 탄다.

| 항목 | 값 |
|---|---|
| 입력 | GPS 10Hz `update_gps()` · BMS 3Hz `update_bms()` (Daly fast 주기와 동일) |
| 극성 | `current_sign=+1` 고정 (가상 차량은 + = 방전으로 값을 만든다. 실차 Daly 극성과 무관) |
| 경기 | 시작 시 `new_race()` → 내구 프로필(`total_laps` 기본 30, 사용 가능 Wh = OCV 적분(SOC 하한~시작 SOC)) → `RACE` |
| 출발선 | 코스 s=20 m 지점, 진행 방향 수직 20 m 폭. 출발 직후 1랩 계측이 시작된다 |
| 배터리 교체 | SOC 하한 도달 시 `new_race()` 로 새 경기(segment) |
| 화면 | `/` = 운전자 화면(`driver_dashboard.html`, 헤더에 DEMO 배지 — 메모리에서만 주입), `/legacy` = 기존 화면 + 랩 오버레이 |
| 추가 옵션 | `--laps N` 목표 랩, `--sample-rate 초` CSV/전송 표본 주기 (기본 실차와 같은 0.2 s), `--bind`, `--port`(0=자동 할당), `--duration 초`(정상 종료) |
| 상태 분리 | `UNIMOTORS_STATE_DIR`를 공유해도 그 아래 `demo/`를 사용한다. 실제 차량 DB·복원 후보와 섞이지 않는다 |

검증 (400 m 시험 코스, 로컬 v7 서버): 시뮬레이터 랩 37.7 / 38.6 s ↔ 런타임 BEST 37.65 / LAST 38.63 s 일치,
`energy_quality=measured`, 서버 `online:["demo"]`, 800×480 넘침 없음.

> ⚠️ **데이터량 (PR #1 공통, 데모 고유 아님)**: v7 표본 1건이 약 **3.2 KB**(CSV) / **4.3 KB**(전송 JSON) —
> `meta` 에 프로필 전체·센서 age·health 가 매 표본 반복된다. 5Hz 기준 **CSV ≈ 60 MB/h, 업링크 ≈ 78 MB/h**
> (v6 는 2Hz·32필드). LTE 요금제·SD 수명 검토 필요 → PR #1 코멘트에 기록.

## 37-6. 자동 실행 (서비스 + Wayland 키오스크)

### 데모 서비스 `/etc/systemd/system/demo-sim.service` (계정 `unimotors-demo` 기준)
```ini
[Unit]
Description=UNIMOTORS Dashboard Demo Simulator
After=network-online.target wg-quick@wg0.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=-/etc/default/unimotors
ExecStart=/usr/bin/python3 /home/unimotors-demo/demo_sim.py
WorkingDirectory=/home/unimotors-demo
Restart=on-failure
RestartSec=5
User=unimotors-demo

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now demo-sim
```

### ⭐ Chromium 키오스크 — 데스크톱(Wayland) 이미지 기준
24-12 의 X11/openbox 방식은 **Lite 이미지용**이다. 데스크톱 trixie 는 Wayland(labwc)라 방식이 다르다.

**SSH 에서 Pi 화면에 바로 띄우기** (키보드 없이)
```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0
nohup chromium --ozone-platform=wayland --kiosk --noerrdialogs --disable-infobars \
  --password-store=basic --no-first-run http://localhost:8080 > ~/chromium.log 2>&1 &
```
| 옵션 | 빠뜨리면 |
|---|---|
| `--ozone-platform=wayland` | X11 로 뜨려다 즉시 `Exit 1` (실제로 겪음) |
| `--password-store=basic` | "키링 잠금 해제" 창이 떠서 키보드 없이 진행 불가 |

**로그인 시 자동 실행** — `~/.config/labwc/autostart`
```bash
sh -c 'until curl -s -o /dev/null http://localhost:8080; do sleep 1; done; chromium --ozone-platform=wayland --kiosk --noerrdialogs --disable-infobars --password-store=basic --no-first-run http://localhost:8080' &
```
- 이 파일이 있으면 시스템 기본 autostart 대신 쓰여 **작업표시줄·배경 없이 계기판만** 뜬다.
  데스크톱으로 되돌리려면 파일 삭제.
- 화면 자동 꺼짐 해제: `sudo raspi-config nonint do_blanking 1`
- 원격 화면 조작이 필요하면 VNC: `sudo raspi-config nonint do_vnc 0`

## 37-7. 교내 와이파이 (WUNIST_AAA, WPA2-Enterprise)

SSH 로 `nmcli` 프로필만 추가해 두면, 학교에서는 우선순위로 자동 연결되고 집에서는 기존 와이파이에 붙는다.
```bash
sudo nmcli connection add type wifi ifname wlan0 con-name campus ssid "WUNIST_AAA" \
  wifi-sec.key-mgmt wpa-eap 802-1x.eap peap 802-1x.phase2-auth mschapv2 \
  802-1x.identity '<아이디>' 802-1x.password '<비밀번호>' 802-1x.password-flags 0 \
  connection.autoconnect yes connection.autoconnect-priority 10
```
**안전한 전환** (SSH 가 끊기므로 백그라운드 + 실패 시 원래 연결로 복귀)
```bash
sudo nohup sh -c 'nmcli connection up campus || nmcli connection up "<현재 연결 이름>"' > /tmp/wifi.log 2>&1 &
```
**겪은 문제**
| 증상 | 원인 / 해결 |
|---|---|
| `Secrets were required, but not provided` | 비밀번호 미저장. **작은따옴표**로 다시 넣고 `802-1x.password-flags 0` (시스템 저장) |
| 복귀 명령 `unknown connection` | 기존 연결의 실제 이름은 현재 연결 이름 (`nmcli -t -f NAME connection show --active` 로 확인) |

**결과**: 교내망에서 **WireGuard(UDP 51820) 정상** — OCI·서버 ping 응답, handshake 갱신,
서버 `online` 에 `demo` 확인. 교내에서도 10.10.0.4 로 원격 접속 가능.
> 비밀번호는 root 전용(600) NetworkManager 설정 파일에 저장된다. 이 문서·레포에는 적지 않는다.

## 37-8. ⭐ 지도 "API KEY REQUIRED" 워터마크 (CARTO 정책 변경)

- 2026년 8월 말부터 CARTO 가 `basemaps.cartocdn.com` 에 **API 키를 요구**한다. 키가 없으면 타일에
  워터마크가 찍힌다(차단은 아님). 9-1 의 "API 키 불필요" 는 더 이상 사실이 아니다.
- 무료 키: carto.com/basemaps/apikey (계정 불필요, 월 500만 타일 무료). 같은 키로 Dark Matter 사용 가능.
- `dashboard.html` 수정 (기존 파일은 백업):
```javascript
const tileURL="https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}{r}.png?key=<키>";
```
- **공개 GitHub 레포에는 키를 넣지 않는다** — `YOUR_CARTO_KEY` 자리표시자로 마스킹.
- 분석 탭 지도의 `attributionControl:false` 는 OSM/CARTO 출처 표기 조건에 맞게 켜는 것을 권장.

## 37-9. 트러블슈팅 요약 (이번 세팅)

| 증상 | 원인 / 해결 |
|---|---|
| `scp: stat local … No such file` | cmd 위치가 `C:\Windows\System32`. 파일이 있는 폴더(다운로드)에서 실행 |
| `Could not resolve hostname demo` | `~/.ssh/config` 별칭 미등록. `unimotors-demo@10.10.0.4` 직접 사용 |
| apt `delayed item … failed` / `Unable to connect …:80` | HTTP 끊김 → **HTTPS 전환** (37-4) |
| `Unable to locate package python3-websocket` | 패키지 목록 손상(미러 404). 목록 복구 후 재설치. 급하면 `pip3 install websocket-client --break-system-packages` |
| Chromium 키오스크 `Exit 1` | `--ozone-platform=wayland` 누락 |
| 키보드 없이 키링 창에 막힘 | `--password-store=basic` |
| 교내 와이파이 `Secrets were required` | 37-7 |
| 서버에 `demo` 안 보임 | `grep "\[UP\]" ~/demo.log`. 없으면 `GPS_DEBUG=1` 로 실행해 401(토큰)/timeout(VPN) 구분 |

## 37-10. 자동 세팅 스크립트 (`scripts/install-pi.sh`)

37-4 ~ 37-7 을 한 번에 수행한다. 다시 실행해도 안전하다(업데이트로 동작, 덮어쓰는 파일은 `.bak-날짜` 백업).

```bash
# 링크 한 줄 (레포 raw 경로)
# PR #2 검토 중에는 통합 브랜치를 사용한다. main 병합 후에는 main으로 바꿀 수 있다.
BASE=https://raw.githubusercontent.com/guinueng/UNIMOTORS_Dash/claude/demo-pi-installer
curl -fsSL $BASE/scripts/install-pi.sh | bash -s -- --role demo --base-url $BASE
# 로컬 파일로 (스크립트와 .py 가 같은 폴더)
bash install-pi.sh --role demo --src .
```

| 단계 | 내용 |
|---|---|
| 1 | APT 저장소 HTTPS 전환 → `apt-get update -y` → `apt-get upgrade -y` (기존 설정 유지, 재시도 3회) → 패키지 |
| 2 | 코드 내려받기 — v7 묶음 `gps_server.py` `race_runtime.py` `driver_dashboard.html` `telemetry_protocol.py` + 역할별 파일 (기존 파일 타임스탬프 백업) |
| 3 | systemd 서비스 생성 (계정명·홈 경로 자동 감지) |
| 4 | Wayland 키오스크 autostart + 화면 꺼짐 해제 |
| 5 | 서버 토큰 `/etc/default/unimotors` (선택, 입력 숨김) |
| 6 | **WireGuard — 값은 직접 입력**, OCI 에 붙여넣을 `[Peer]` 블록을 `~/wg-peer-<호스트명>.txt` 로 생성 (비밀값 없음 → 팀 공유 가능) |
| 7 | 교내 와이파이 프로필 (선택, 프로필만 추가하고 연결은 바꾸지 않음) |

- 공개 가능한 값은 옵션으로 넘겨 팀에 공유: `--wg-endpoint <OCI_PUBLIC_IP>:51820 --wg-server-pub <서버공개키>`.
  받은 사람은 VPN IP 와 PSK 만 입력한다.
- PSK·토큰·와이파이 비밀번호는 화면·설치 로그·공유 파일에 남지 않는다.
- `--no-upgrade` 로 업그레이드 생략, `--dry-run` 으로 할 일만 출력, `--role car` 는 실차 수집기 구성(실차 하드웨어 미검증).

### v3.1 설치 검증과 설정 보존

받은 파일은 먼저 사용자 홈의 임시 폴더에 모은다. Python 문법, `gps_server` v7과 `race_runtime`·`telemetry_protocol` import, 운전자 HTML을 확인하고, 데모는 한 랩 계산이 성공해야 기존 파일을 교체한다. 실패 시 기존 코드 파일과 서비스 설정을 교체하지 않는다. APT 단계는 이 검증보다 앞에 있으므로 패키지 업데이트는 이미 수행됐을 수 있다. `--dry-run`은 실행 계획만 출력하며 검증 통과라고 표시하지 않는다.

토큰 갱신은 기존 `/etc/default/unimotors`의 주석·프리셋·다중행 값을 보존한다. 맨 뒤의 설치기 관리 블록에서 유효한 토큰 값을 설정하며, 반복 갱신은 이 블록만 교체한다. 이전에 수동으로 작성한 토큰 줄은 그대로 남지만 마지막 관리 블록의 값이 적용된다. 파일은 임시 파일 작성·fsync·원자적 교체로 갱신하고 권한 600을 적용한다. 토큰은 Python 프로세스의 명령행 인자에 넣지 않는다. [EnvironmentFile 규칙](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#EnvironmentFile=)

PC에서 v7 연결을 확인하려면 저장소 루트에서 다음을 실행한다.

기존 v7 데모의 상태 DB를 사용하던 경우 새 경로는 그 아래 `demo/`다. 기존 DB는 삭제·자동 이동하지 않는다. 이전 데모의 미전송 자료가 필요하면 기존 CSV의 backfill 경로로 별도 처리한다. 실제 차량 DB를 데모 초기화로 변경하지 않기 위한 분리다.

```bash
python vehicle/demo_sim.py --no-upload --bind 127.0.0.1 --port 8080 --seed 2026
```

`http://127.0.0.1:8080/`은 새 운전자 화면이고 `/legacy`는 이전 화면이다. Windows에서는 먼저 `python -m pip install -r requirements-dev.txt`로 테스트 의존성을 준비한다. Raspberry Pi에서는 기존 설치 스크립트를 사용한다.

자동 검증은 `python -m pytest -q`, 브라우저 검증은 Chromium을 준비한 뒤 `python scripts/demo-browser-smoke.py`로 실행한다. 후자는 합성 데이터와 임시 DB만 사용해 실제 HTTP/SSE/로컬 WS ACK, 서버 중단·재시작 후 재전송, 800×480 세 모드를 확인한다. 실제 Pi·DSI·WireGuard 검증과는 구별한다.

상용 게임으로 학생 반응을 평가하는 방법은 [Project CARS 1 연동 설계](22-project-cars-study.md)를 참고한다. 게임 입력 어댑터는 현재 데모 입력 발생기와 별도 구현 대상이다.

## 37-11. 남은 작업

- [ ] **재부팅 검증** — 전원만 켜서 DSI 계기판 + 태블릿 `demo` 표시 확인 (서비스·autostart 등록 후)
- [ ] 서버 `dashboard.html` 에 CARTO 키 적용 확인
- [ ] (선택) 서버 `VEHICLE_NAMES` 에 `"demo": "데모 차량"` 추가
- [x] `demo_sim.py` · `install-pi.sh` 레포 반영 (이 장)
- [x] demo_sim v2 / install-pi v3 — v7 RaceRuntime 대응 (PR #1 브랜치)
- [ ] PR #1 병합 후 데모 Pi 재설치(`install-pi.sh --role demo`) → LCD 운전자 화면·태블릿 확인
- [ ] 데모 종료 후 `demo` 데이터 정리 여부 결정 (22-B-3)
