# 레퍼런스 (명령 · 경로 · 백업 · 설계근거)

> 핵심 명령 요약 · 기기별 파일 위치 · 접속 정보 · 백업 · 참고 URL

---

## A. 핵심 명령 요약

### 차량 Pi
```bash
# 서비스 상태
systemctl is-active lte-auto gps-dashboard wg-quick@wg0
systemctl list-timers backfill.timer
nmcli connection show --active

# LTE
ip a show wwan0 | grep inet
ping -I wwan0 -c 4 8.8.8.8
curl --interface wwan0 ifconfig.me

# GPS (서비스 중지 후)
sudo systemctl stop gps-dashboard
cat /dev/ttyUSB1                    # raw NMEA
sudo systemctl start gps-dashboard

# GPS 디버그 로그 (권장)
sudo systemctl stop gps-dashboard
python3 ~/gps_server.py --debug     # lat/lon/sats/fix/in_waiting 확인

# 모뎀 AT 명령 (minicom)
sudo minicom -D /dev/ttyUSB2
#  AT / AT+CPIN? / AT+CSQ / AT+CGDCONT? / AT+CGPS?
#  AT+CGNSSMODE? / AT+CGPSNMEA? / AT+CGPSINFO / AT+CGNSSINFO

# CAN
sudo ip link set can0 up type can bitrate 250000
candump can0
cansend can0 18DD0140#8888888888888888    # BMS 요청

# 핫스팟
sudo nmcli connection up unimotors-hotspot
sudo nmcli connection down unimotors-hotspot

# backfill 수동
python3 ~/backfill.py --all
sudo systemctl start backfill.service
journalctl -u backfill.service -n 30 --no-pager

# 전원/온도
vcgencmd get_throttled       # 0x0 = 정상
vcgencmd measure_temp
```

### 서버 Pi4
```bash
systemctl status telemetry.service
journalctl -u telemetry.service -f
curl -s http://10.10.0.9:8090/api/status

# DB 조회
sqlite3 ~/telemetry.db "SELECT session,COUNT(*),MIN(ts),MAX(ts) FROM summary GROUP BY session;"
sqlite3 ~/telemetry.db "SELECT ts,speed,lat,lon,alt,voltage,soc FROM summary ORDER BY ts DESC LIMIT 5;"
sqlite3 ~/telemetry.db "SELECT session,COUNT(*) FROM track GROUP BY session;"
sqlite3 ~/telemetry.db ".schema summary"
```

### OCI
```bash
sudo wg show
sudo nginx -t && sudo systemctl reload nginx
sudo certbot certificates
systemctl list-timers certbot.timer
curl -s http://10.10.0.9:8090/api/status      # VPN 너머 서버 확인
sudo iptables -L INPUT -n | grep -E '51820|:80|:443'
sudo iptables -t nat -L POSTROUTING -n
```

## B. 파일·서비스 위치 (기기별)

### OCI VPS (10.10.0.1, unimotors-vps)
| 항목 | 경로 |
|---|---|
| WireGuard 허브 | `/etc/wireguard/wg0.conf` |
| 키/PSK | `~/server_private.key`, `~/shared.psk` 등 (600) |
| nginx SSL 공통 | `/etc/nginx/snippets/unimotors-ssl.conf` |
| nginx 사이트 | `/etc/nginx/sites-available/unimotors-{root,data,dash}` |
| basic auth | `/etc/nginx/.htpasswd` |
| 인증서 | `/etc/letsencrypt/live/unimotors.example.com/` |
| Cloudflare API 토큰 | `/etc/letsencrypt/cloudflare.ini` (600) |
| 랜딩 페이지 | `/var/www/unimotors/index.html` |
| 데이터 포털 | `/var/www/unimotors/app/index.html` |

### 서버 Pi4 (10.10.0.9, unimotors-srv)
| 항목 | 경로 |
|---|---|
| 텔레메트리 서버 | `/home/unimotors/telemetry_server.py` |
| 대시보드 | `/home/unimotors/dashboard.html` |
| DB | `/home/unimotors/telemetry.db` |
| 서비스 | `/etc/systemd/system/telemetry.service` |
| VPN | `/etc/wireguard/wg0.conf` |

### 차량 Pi (10.10.0.3, unimotors-car)
| 항목 | 경로 |
|---|---|
| 계기판/수집 | `/home/unimotors/gps_server.py` |
| BMS 파서 | `/home/unimotors/bms_reader.py` |
| 업로더 | `/home/unimotors/backfill.py` |
| LTE+GPS 스크립트 | `/home/unimotors/lte_auto.sh` |
| 로그 | `/home/unimotors/gps_logs/{gps,telem}_*.csv` |
| 서비스 | `/etc/systemd/system/{lte-auto,gps-dashboard,backfill}.service` |
| 타이머 | `/etc/systemd/system/backfill.timer` |
| CAN 자동 up | `/etc/systemd/network/80-can0.network` |
| 핫스팟 | nmcli `unimotors-hotspot` |
| VPN | `/etc/wireguard/wg0.conf` |

## C. 접속 정보

| 대상 | 주소 |
|---|---|
| 홍보 페이지 (공개) | `https://unimotors.example.com` |
| 데이터 포털 (로그인) | `https://unimotors.example.com/app` |
| 팀 대시보드 | `https://data.unimotors.example.com` |
| 차량 계기판 (원격) | `https://dash.unimotors.example.com` |
| 차량 계기판 (핫스팟) | `http://10.42.0.1:8080` (WiFi: `UNIMOTORS_DASH`) |
| 차량 계기판 (VPN) | `http://10.10.0.3:8080` |
| 팀 대시보드 (VPN) | `http://10.10.0.9:8090` |
| SSH 차량 | `ssh unimotors@10.10.0.3` |
| SSH 서버 | `ssh unimotors@10.10.0.9` |
| SSH OCI | `ssh ubuntu@<OCI_PUBLIC_IP>` (또는 10.10.0.1) |

> 핫스팟 비밀번호, basic auth 비번, SSH 키, AUTH_TOKEN 등 민감 정보는
> 이 문서에 적지 말고 팀 비밀번호 관리자에 보관.

## D. 백업 (⭐ 가장 중요)

> 이 프로젝트의 **이전 서버는 SSH 키를 분실해 접근 불가**가 된 이력이 있다.
> 핵심 원칙: **"개인키는 재생성 불가(잃으면 접근 끝), 공개키는 언제든 재생성 가능."**

### 백업 대상
1. SSH 개인키 / 공개키 (`~/.ssh/unimotors_oci{,.pub}`)
2. OCI WireGuard 설정 + 키 + PSK
3. WireGuard 클라이언트 conf (Windows 앱 → Export tunnels to zip)
4. Cloudflare API 토큰 (`/etc/letsencrypt/cloudflare.ini`)
5. 서버 DB (`telemetry.db`) — 주행 데이터
6. 코드 6종 (gps_server / bms_reader / backfill / telemetry_server / dashboard / 웹페이지)

### OCI 설정 tar
```bash
sudo tar czf ~/wg-backup-$(date +%Y%m%d).tar.gz -C /etc/wireguard wg0.conf
tar czf ~/keys-backup-$(date +%Y%m%d).tar.gz ~/*.key ~/shared.psk
```
### 노트북으로 내려받기 (PowerShell)
```powershell
mkdir $env:USERPROFILE\Desktop\unimotors-backup
scp -i $env:USERPROFILE\.ssh\unimotors_oci ubuntu@10.10.0.1:"~/*-backup-*.tar.gz" `
    "$env:USERPROFILE\Desktop\unimotors-backup\"
copy $env:USERPROFILE\.ssh\unimotors_oci* $env:USERPROFILE\Desktop\unimotors-backup\
```
### 보관
- **노트북 밖 최소 2곳**: 비밀번호 관리자(Bitwarden/1Password) + 클라우드 비공개 폴더
- 개인키 포함 → **공개 공유 링크 금지**. 클라우드 업로드 시 7-Zip AES-256 암호 압축

## E. 참고 자료 URL

**SIM7600G-H (B) LTE HAT**
- https://www.waveshare.com/wiki/SIM7600G-H_4G_HAT_(B)

**Daly BMS**
- CAN 프로토콜 V1.0: https://robu-prod-media.s3.ap-south-1.amazonaws.com/uploads/2022/02/Daly-CAN-Communications-Protocol-V1.0-1.pdf
- 구형 시리즈 매뉴얼: https://www.dalybms.com/uploads/Old-series-hardware-BMS-manual.pdf
- 액티브 밸런스 설명서(영문): https://www.dalyelec.com/download/主动均衡保护板说明书%20电子版%20（英文）.pdf
- R24TS 스마트 액티브 밸런스: https://www.battery-energy-storage-system.com/battery-management-system/2025/0624/daly-8-17s-r24ts-smart-active-balance-bms.html
- manuals.plus: https://manuals.plus/ae/1005006343087307

## F. 왜 이 기술을 골랐나 (설계 근거)

| 선택 | 대안 | 이유 |
|---|---|---|
| WireGuard | ZeroTier / Tailscale | 커널 모듈로 가볍고(Zero W에 적합), 외부 서비스 의존 없음, 설정이 conf 파일 하나 |
| AT+NDIS (Raw IP) | QMI (qmicli) | 이 모듈 펌웨어에서 QMI가 CID/timeout 실패. AT 방식은 안정적 |
| SQLite | PostgreSQL / InfluxDB | 별도 프로세스 0, Pi에서 충분, 파일 하나로 백업 |
| WebSocket (내부 평문) | MQTT / HTTPS | WireGuard가 이미 암호화하므로 TLS 이중화 불필요. 양방향 실시간 |
| Leaflet + OSM | Google Maps | 무료, API 키 불필요, 다크 타일(CARTO) 지원 |
| 정적 HTML | WordPress | OCI 1GB에 PHP+MySQL 부담, 보안 표적, 홍보 페이지는 수정 빈도 낮음 |
| SSE (계기판) | WebSocket | 단방향이면 SSE가 단순. 단 **nginx 버퍼링 주의**(23-2) |
| systemd 타이머 (backfill) | cron | 의존성(`After=wg-quick`) 지정 가능, `Persistent=true`로 놓친 실행 만회 |

---

← [트러블슈팅 종합](16-troubleshooting.md) · [문서 목차](README.md) · [현재 상태와 남은 작업](18-status-todo.md) →
