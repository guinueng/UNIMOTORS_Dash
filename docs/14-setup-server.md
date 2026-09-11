# 서버 Pi4 전체 세팅 순서

> WireGuard · telemetry.service · DB 백업 cron

---

## 25. 서버 Pi4 전체 세팅 순서

```bash
# 1. 기본
sudo hostnamectl set-hostname unimotors-srv
sudo sed -i 's/127.0.1.1.*/127.0.1.1\tunimotors-srv/' /etc/hosts
sudo apt update && sudo apt install -y wireguard python3-aiohttp sqlite3

# 2. WireGuard (10.10.0.9) — 키는 Pi에서 생성 (개인키가 Pi를 안 떠남)
umask 077
wg genkey | tee srv_private.key | wg pubkey > srv_public.key
cat srv_public.key      # → OCI wg0.conf 의 [Peer] PublicKey 에 등록
sudo nano /etc/wireguard/wg0.conf   # Address=10.10.0.9/24, keepalive=25
sudo systemctl enable --now wg-quick@wg0
ping -c3 10.10.0.1      # 8ms 정상

# 3. 서버 파일
#   /home/unimotors/telemetry_server.py
#   /home/unimotors/dashboard.html
```

`/etc/systemd/system/telemetry.service`
```ini
[Unit]
Description=UNIMOTORS Telemetry Server
After=network-online.target wg-quick@wg0.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/unimotors/telemetry_server.py
WorkingDirectory=/home/unimotors
Restart=on-failure
RestartSec=5
User=unimotors

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telemetry.service
curl -s http://10.10.0.9:8090/api/status    # {"online":[],"vehicles":[]}
```

### 25-1. (선택) DB 백업 자동화 — cron
```bash
crontab -e
```
```cron
# 매일 새벽 4시 DB 백업 (7일 보관)
0 4 * * * sqlite3 /home/unimotors/telemetry.db ".backup /home/unimotors/backup/telemetry-$(date +\%Y\%m\%d).db" && find /home/unimotors/backup -name 'telemetry-*.db' -mtime +7 -delete
```
```bash
mkdir -p ~/backup
```
> `sqlite3 .backup`은 실행 중인 DB도 안전하게 복사한다 (파일 cp는 손상 위험).
> cron에서 `%`는 `\%`로 이스케이프해야 한다.

---

← [차량 Pi 전체 세팅 순서](13-setup-vehicle.md) · [문서 목차](README.md) · [OCI VPS 전체 세팅 순서](15-setup-oci.md) →
