# 차량 Pi 전체 세팅 순서

> 패키지 · systemd 5종 · CAN 자동 up · backfill 타이머 · DSI 키오스크

---

## 24. 차량 Pi 전체 세팅 순서

### 24-1. OS + 기본
```bash
# Raspberry Pi Imager: OS Lite, SSH 켜기, WiFi(2.4GHz), 국가=KR
sudo hostnamectl set-hostname unimotors-car
sudo sed -i 's/127.0.1.1.*/127.0.1.1\tunimotors-car/' /etc/hosts   # sudo 경고 방지
sudo apt update && sudo apt upgrade -y
```

### 24-2. 패키지
```bash
sudo apt install -y minicom udhcpc can-utils wireguard python3-pip
pip3 install pyserial python-can websocket-client --break-system-packages
sudo systemctl stop ModemManager && sudo systemctl disable ModemManager
```

### 24-3. 파일 배치
```
/home/unimotors/
├── gps_server.py      (PRESET 확인! SERVER_WS_URL=10.10.0.9)
├── bms_reader.py
├── backfill.py        (DEFAULT_SERVER=10.10.0.9)
├── lte_auto.sh        (chmod +x)
└── gps_logs/          (자동 생성)
```

### 24-4. systemd 서비스 ① LTE + GPS 자동 연결
`/etc/systemd/system/lte-auto.service`
```ini
[Unit]
Description=Auto LTE Connection for SIM7600
After=network.target

[Service]
Type=oneshot
ExecStart=/home/unimotors/lte_auto.sh
RemainAfterExit=yes
User=root

[Install]
WantedBy=multi-user.target
```

### 24-5. systemd 서비스 ② 계기판 서버
`/etc/systemd/system/gps-dashboard.service`
```ini
[Unit]
Description=UNIMOTORS GPS Dashboard Server
After=lte-auto.service
Wants=lte-auto.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/unimotors/gps_server.py
Restart=on-failure
RestartSec=5
User=unimotors

[Install]
WantedBy=multi-user.target
```

### 24-6. ③ CAN 자동 up (udev 또는 device 유닛)
> ⚠️ `systemd-networkd`는 쓰지 않는다 — NetworkManager(핫스팟)와 충돌 위험. 8-3 참고.

**방법 A (권장, 간단)** — udev 규칙
```bash
sudo tee /etc/udev/rules.d/90-can.rules > /dev/null <<'EOF'
SUBSYSTEM=="net", ACTION=="add", KERNEL=="can*", \
  RUN+="/sbin/ip link set %k up type can bitrate 250000"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=net --action=add
```

**방법 B** — systemd device 유닛 (`/etc/systemd/system/can0.service`, 8-3 참고)
```bash
sudo systemctl enable can0.service
```

### 24-7. systemd 서비스 + 타이머 ④ backfill 자동 업로드
`/etc/systemd/system/backfill.service`
```ini
[Unit]
Description=UNIMOTORS telemetry backfill (telem CSV -> server)
After=network-online.target wg-quick@wg0.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /home/unimotors/backfill.py --all
WorkingDirectory=/home/unimotors
User=unimotors
SuccessExitStatus=1
```
`/etc/systemd/system/backfill.timer`
```ini
[Unit]
Description=Run UNIMOTORS backfill periodically

[Timer]
OnBootSec=3min          # 부팅 3분 후 (VPN/LTE 안정화 대기)
OnUnitActiveSec=15min   # 이후 15분마다
Persistent=true         # 놓친 실행은 다음 부팅에 만회

[Install]
WantedBy=timers.target
```
> **주기 실행이 안전한 이유**: 서버가 `INSERT OR IGNORE`(ts 기본키)라 같은 파일을
> 여러 번 올려도 중복이 쌓이지 않는다. LTE 음영으로 실패하면 다음 타이머에 재시도된다.

**등록·확인**
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now backfill.timer
systemctl list-timers backfill.timer            # 다음 실행 시각
sudo systemctl start backfill.service           # 지금 즉시 1회 실행
journalctl -u backfill.service -n 30 --no-pager # 결과 (수신/신규 행 수)
```
정상 출력:
```
[완료] telem_20260723_100000.csv -> car1/20260723_100000: 수신 300행, 신규 300행
```
- `신규 0` = 이미 올라간 데이터 (**정상**, 중복 무시)
- `서버 연결 불가` = VPN 끊김/서버 꺼짐 → 다음 타이머에 재시도

**대안: 주행 종료(shutdown) 시 1회만 업로드**
```ini
# backfill.service 의 [Unit] 에 추가
Before=shutdown.target
DefaultDependencies=no
Conflicts=reboot.target
```
> ⚠️ 차량은 전원을 갑자기 끊는 경우가 많아 실행이 보장되지 않는다. **타이머 방식을 권장.**

### 24-8. 핫스팟 (nmcli, systemd 아님)
⚠️ 실행 순간 wlan0가 AP 모드로 바뀌어 **WiFi SSH가 끊긴다.**
반드시 **VPN(LTE 경유) 세션에서** 실행할 것. `who`로 `10.10.0.x` 확인.
```bash
sudo nmcli connection add type wifi ifname wlan0 con-name unimotors-hotspot \
  autoconnect yes ssid UNIMOTORS_DASH
sudo nmcli connection modify unimotors-hotspot \
  802-11-wireless.mode ap 802-11-wireless.band bg \
  ipv4.method shared wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "<핫스팟_비밀번호>"
sudo nmcli connection up unimotors-hotspot
```
**되돌리기** (집에서 일반 WiFi 클라이언트로):
```bash
sudo nmcli connection down unimotors-hotspot
sudo nmcli connection modify unimotors-hotspot connection.autoconnect no
# 완전 삭제: sudo nmcli connection delete unimotors-hotspot
```

### 24-9. WireGuard (차량, 10.10.0.3)
`/etc/wireguard/wg0.conf`
```ini
[Interface]
PrivateKey = <pi_private>
Address = 10.10.0.3/24

[Peer]
PublicKey = <OCI server_public>
PresharedKey = <shared.psk>
Endpoint = <OCI_PUBLIC_IP>:51820
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
```

### 24-10. 전체 활성화
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lte-auto.service
sudo systemctl enable --now gps-dashboard.service
sudo systemctl enable --now wg-quick@wg0
sudo systemctl enable --now backfill.timer
# CAN 자동 up: udev 규칙(24-6 방법A) 또는
# sudo systemctl enable can0.service   (방법B)
```

### 24-11. 재부팅 검증
```bash
sudo reboot
# 2~3분 대기 후 VPN으로 재접속
ssh unimotors@10.10.0.3

systemctl is-active lte-auto gps-dashboard wg-quick@wg0   # 전부 active
systemctl list-timers backfill.timer                       # 다음 실행 시각
nmcli connection show --active | grep hotspot              # unimotors-hotspot
ip a show wwan0 | grep inet                                # LTE IP
ip a show wlan0 | grep 10.42                               # 핫스팟 게이트웨이
sudo wg show                                               # handshake
ip link show can0                                          # CAN (어댑터 꽂았으면)
```

### 24-12. ⭐ 차량 디스플레이 (DSI LCD + 키오스크) — 권장 구성

> **왜 이 구성이 중요한가**: 계기판을 폰으로 보면 폰이 핫스팟에 묶여 **인터넷을 잃는다.**
> 주행 중 디스코드로 오더를 받아야 하면 치명적이다. 계기판을 Pi 자체 화면(`localhost`)에
> 띄우면 **캡티브 포털 문제가 원천적으로 사라지고**, 폰은 셀룰러 100%로 자유로워진다.
> 계기판 HTML은 외부 리소스가 **0개**라 인터넷 없이 완전히 동작한다.

| 항목 | 폰 계기판 | **Pi 내장 디스플레이** |
|---|---|---|
| 폰 인터넷 | ❌ 희생 (핫스팟에 묶임) | ✅ 셀룰러 100% |
| 캡티브 포털 문제 | 있음 | **없음** |
| 지연 | WiFi 경유 | localhost (거의 0) |
| 주행 중 실패 지점 | WiFi 끊김 위험 | 없음 |
| 디스코드 통화 | 어려움 | 문제없음 |

핫스팟은 **보조**로 남겨둔다(피트 크루가 근처에서 볼 때). 단 **폰에서 `UNIMOTORS_DASH`
자동 연결을 꺼야** 한다 — 차 근처에서 폰이 자동으로 붙어 인터넷을 잃는 것을 막는다.

#### 24-12-1. DSI LCD 인식
공식 Pi 디스플레이는 `config.txt`의 `display_auto_detect=1`로 자동 인식된다(기본값).
알리 등 서드파티 5인치 DSI는 전용 오버레이가 필요할 수 있다.
```bash
# 인식 확인
ls /sys/class/drm/                      # card1-DSI-1 같은 항목이 있으면 인식됨
dmesg | grep -i -E "dsi|panel|drm"
kmsprint 2>/dev/null || modetest -c 2>/dev/null | head -20
```
안 잡히면 판매처가 안내한 오버레이를 `/boot/firmware/config.txt`에 추가한다.
(모델별로 다르므로 뒷면 모델명·터치칩(GT911 등)·주문 링크를 먼저 확인)
```ini
# /boot/firmware/config.txt 예시
display_auto_detect=1
dtoverlay=vc4-kms-v3d
max_framebuffers=2
# 화면 회전이 필요하면 (KMS 환경)
# video=DSI-1:800x480@60,rotate=180
```
> 삽질이 길어지면 **Pi 4의 micro-HDMI로 임시 확인**해 소프트웨어 쪽 문제를 먼저 배제한다.

#### 24-12-2. 키오스크 패키지 (Pi OS Lite 기준)
Lite에는 X/데스크톱이 없으므로 최소 구성만 설치한다.
```bash
sudo apt install -y --no-install-recommends \
  xserver-xorg x11-xserver-utils xinit openbox chromium-browser unclutter
```
> 데스크톱 환경 전체(`raspberrypi-ui-mods`)는 설치하지 않는다 — 3B+에서 무겁다.

#### 24-12-3. 키오스크 실행 스크립트
`/home/unimotors/kiosk.sh`
```bash
#!/bin/bash
# 화면 절전/블랭킹 해제 (주행 중 화면이 꺼지면 안 됨)
xset s off
xset s noblank
xset -dpms
unclutter -idle 0.1 -root &        # 마우스 커서 숨김
openbox-session &                   # 최소 윈도우 매니저

# 계기판이 뜰 때까지 대기 (gps_server 부팅 순서 방어)
until curl -s -o /dev/null http://127.0.0.1:8080/; do sleep 1; done

chromium-browser \
  --kiosk http://127.0.0.1:8080/ \
  --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
  --disable-features=Translate --no-first-run \
  --check-for-update-interval=31536000 \
  --disable-pinch --overscroll-history-navigation=0 \
  --autoplay-policy=no-user-gesture-required
```
```bash
chmod +x /home/unimotors/kiosk.sh
```

#### 24-12-4. systemd 서비스 ⑤ 키오스크
`/etc/systemd/system/kiosk.service`
```ini
[Unit]
Description=UNIMOTORS Dashboard Kiosk (local display)
After=gps-dashboard.service
Wants=gps-dashboard.service

[Service]
Type=simple
User=unimotors
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/unimotors/.Xauthority
# startx 로 X 를 띄우고 그 안에서 kiosk.sh 실행
ExecStart=/usr/bin/startx /home/unimotors/kiosk.sh -- :0 -nocursor
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
```bash
# 콘솔 사용자에게 X 실행 허용
sudo dpkg-reconfigure xserver-xorg-legacy   # "Anybody" 선택
#  또는: /etc/X11/Xwrapper.config 에  allowed_users=anybody  +  needs_root_rights=yes

sudo systemctl daemon-reload
sudo systemctl enable --now kiosk.service
```

#### 24-12-5. 로컬 표시용 레이트 상향
디스플레이가 로컬이라 네트워크 부담이 없다. 3B+라면 부드럽게 올린다.
```python
# gps_server.py
DISPLAY_RATE = 0.1      # 1.0 → 0.1 (계기판 state 10Hz)
SSE_RATE     = 0.1      # 0.5 → 0.1 (localhost 라 부담 적음)
UPLOAD_RATE  = 0.5      # 서버 전송은 2Hz 유지 (LTE 데이터 절약)
```
> `UPLOAD_RATE`는 올리지 않는다 — LTE로 나가는 트래픽이므로 2Hz가 적절하다.

#### 24-12-6. 확인
```bash
systemctl status kiosk.service
# 화면에 계기판이 풀스크린으로 떠야 함. 안 뜨면:
journalctl -u kiosk.service -n 40 --no-pager
cat /home/unimotors/.local/share/xorg/Xorg.0.log | tail -30
```
| 증상 | 확인 |
|---|---|
| 검은 화면만 | DSI 인식 실패 → 24-12-1 |
| X 시작 실패 | `Xwrapper.config`의 `allowed_users=anybody` |
| 계기판 대신 오류 페이지 | `gps-dashboard.service` 미실행 → `systemctl status` |
| 화면이 몇 분 뒤 꺼짐 | `kiosk.sh`의 `xset -dpms` 누락 |
| 커서가 보임 | `unclutter` 미실행 또는 `-nocursor` 누락 |

---

← [주행 분석 · 랩 분석 · 배터리 분석](12-analysis.md) · [문서 목차](README.md) · [서버 Pi4 전체 세팅 순서](14-setup-server.md) →
