# 서버 인프라 실배포 (VPN · nginx · TLS)

> 네트워크 지형도 · 폰 풀터널 · 리버스 프록시 · 와일드카드 인증서

---

## 14. 네트워크 지형도 (실제 배포된 상태)

### 14-1. WireGuard 메시 (10.10.0.0/24) — 최종
```
OCI VPS (unimotors-vps, 공인 <OCI_PUBLIC_IP>, UDP 51820)
  = 허브 10.10.0.1  [nginx 리버스 프록시 + WireGuard 허브]
  ├─ 10.10.0.2   PC (노트북)
  ├─ 10.10.0.3   차량 Pi (unimotors-car, keepalive 25)
  ├─ 10.10.0.9   기숙사 서버 Pi4 (unimotors-srv, keepalive 25) ★이번 세션 실연결
  └─ 10.10.0.11  내부망/폰 클라이언트 (con_1, keepalive 25)
```
- **모든 피어 공통 PSK** (shared.psk) 사용
- **keepalive=25는 NAT 뒤 기기(.3/.9/.11)에만**, OCI 허브(.1) conf엔 안 넣음
- 서버 conf(OCI)의 각 `[Peer] AllowedIPs`는 `/32` (피어 신원 지정)

### 14-2. ⭐ 서버 IP = 10.10.0.9 (이력)
- 초기 설계 단계에서는 서버를 `10.10.0.4`로 계획했으나,
  **실제 배포 시 `10.10.0.9`로 등록**했다 (OCI conf에 이 IP로 피어 생성).
- **현재 문서·코드 전체가 `10.10.0.9`로 통일되어 있다:**
  - `gps_server.py`: `SERVER_WS_URL = "ws://10.10.0.9:8090/ingest"`
  - `backfill.py`: `DEFAULT_SERVER = "http://10.10.0.9:8090"`
  - nginx `unimotors-data`: `proxy_pass http://10.10.0.9:8090`
- 혹시 예전 사본을 쓰게 되면 아래로 확인:
```bash
grep -rn "10.10.0.4" ~/gps_server.py ~/backfill.py    # 결과 없어야 정상
```

### 14-3. 도메인 구조 (Cloudflare 관리, example.com)
```
unimotors.example.com        → OCI nginx → 랜딩 페이지(정적, nginx 직접 서빙)
  └ /app                       → basic auth → 데이터 포털(정적)
data.unimotors.example.com   → OCI nginx → 10.10.0.9:8090 (기숙사 Pi 대시보드)
dash.unimotors.example.com   → OCI nginx → 10.10.0.3:8080 (차량 계기판)
```
- 서브도메인 전부 같은 OCI 공인 IP를 가리킴 (nginx가 server_name으로 분기)
- `*.unimotors` 와일드카드 A 레코드 하나로 커버 가능 (새 서브도메인 시 DNS 작업 불필요)

## 15. WireGuard 피어 추가 & 폰 풀터널

### 15-1. 서버 Pi(10.10.0.9) 피어 추가 (방식 B: Pi에서 키 생성)
개인키가 Pi를 떠나지 않는 안전한 방식.
```bash
# Pi에서
umask 077
wg genkey | tee srv_private.key | wg pubkey > srv_public.key
cat srv_public.key      # 이 공개키를 OCI에 등록
```
OCI `wg0.conf`에 `[Peer]` 추가 (PublicKey=위 공개키, AllowedIPs=10.10.0.9/32), 재시작.
Pi `/etc/wireguard/wg0.conf`:
```ini
[Interface]
PrivateKey = <Pi srv_private>
Address = 10.10.0.9/24
[Peer]
PublicKey = <OCI server_public>
PresharedKey = <shared.psk>
Endpoint = <OCI_PUBLIC_IP>:51820
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
```
```bash
sudo systemctl enable --now wg-quick@wg0
sudo wg show            # handshake 확인
ping -c3 10.10.0.1      # 8ms 정상 확인됨
```
> 키 자리 규칙: `[Interface] PrivateKey`=자기 개인키, `[Peer] PublicKey`=상대(OCI) 공개키.
> 개인키는 절대 서로 바뀌면 안 됨.

### 15-2. 키 파일 권한
- 개인키/PSK는 `chmod 600` (소유자만). 공개키는 644 무방.
- `server_private.key`가 root 소유 644로 노출돼 있던 것 → `sudo chmod 600`로 교정.
- **소유자는 600이어도 `cat` 가능** (600은 남 차단이지 본인 차단 아님).
  root 소유 파일만 `sudo cat`.

### 15-3. 폰 WireGuard (QR로 등록)
```bash
sudo apt install -y qrencode
# 폰용 conf 내용을 QR로 (터미널에 표시, 파일 안 남김)
cat <<EOF | qrencode -t ansiutf8
[Interface]
PrivateKey = <con_1_private>
Address = 10.10.0.11/24
DNS = 1.1.1.1, 8.8.8.8          # 풀터널일 때 필수
[Peer]
PublicKey = <OCI server_public>
PresharedKey = <shared.psk>
Endpoint = <OCI_PUBLIC_IP>:51820
AllowedIPs = 0.0.0.0/0          # 풀터널(모든 트래픽). 스플릿이면 10.10.0.0/24
PersistentKeepalive = 25
EOF
```
폰 WireGuard 앱 → QR 스캔 → 등록.

### 15-4. ⭐ 풀터널(0.0.0.0/0) → OCI MASQUERADE 필수
풀터널로 폰 인터넷을 VPN 경유시키려면 OCI가 NAT 마스커레이딩 해야 함.
**이거 없으면 "VPN은 붙는데 인터넷 안 됨" 증상** (이번에 실제 겪음).
```bash
# OCI에서. 인터페이스 이름 확인:
ip route get 8.8.8.8            # dev 뒤 이름 (보통 ens3)
# 포워딩 + NAT
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-wireguard.conf
sudo sysctl -p /etc/sysctl.d/99-wireguard.conf
sudo iptables -t nat -A POSTROUTING -s 10.10.0.0/24 -o ens3 -j MASQUERADE
sudo iptables -I FORWARD 1 -i wg0 -o ens3 -j ACCEPT
sudo iptables -I FORWARD 1 -i ens3 -o wg0 -m state --state RELATED,ESTABLISHED -j ACCEPT
sudo netfilter-persistent save
```
- 진단: `wg show`에 handshake 있는데 인터넷만 안 되면 = MASQUERADE 문제 확정
- 스플릿 터널(10.10.0.0/24)만 쓰면 이 규칙 불필요
- 풀터널 conf엔 `DNS=` 필수 (없으면 이름 해석 안 됨)

## 16. OCI nginx 리버스 프록시 + 와일드카드 TLS

### 16-1. 설치 + 방화벽
```bash
sudo apt install -y nginx
# 80/443 개방 (iptables + OCI Security List 콘솔 둘 다!)
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```
> 80/443은 TCP만. (WireGuard만 UDP 51820)
> OCI는 **Security List(콘솔) + iptables(서버)** 둘 다 열어야 함.

### 16-2. 와일드카드 인증서 (Cloudflare DNS-01)
HTTP 챌린지는 서브도메인마다 개별 발급이라, 와일드카드로 한 방에:
```bash
sudo apt install -y certbot python3-certbot-dns-cloudflare
# Cloudflare API 토큰 발급 (Edit zone DNS, example.com 한정) 후:
sudo tee /etc/letsencrypt/cloudflare.ini >/dev/null <<'EOF'
dns_cloudflare_api_token = <토큰>
EOF
sudo chmod 600 /etc/letsencrypt/cloudflare.ini
sudo certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  -d "unimotors.example.com" -d "*.unimotors.example.com"
```
- 루트 + 와일드카드 **둘 다 `-d`** (별표는 서브만, 루트 자신은 커버 안 함)
- 인증서: `/etc/letsencrypt/live/unimotors.example.com/` (만료 2026-10-19, 자동갱신)
- DNS 플러그인이 TXT 자동 생성/삭제 → Cloudflare 프록시(주황/회색) 무관하게 발급됨
- HTTP 챌린지로 처음 시도 시 **Cloudflare 프록시(주황구름)면 403** → 회색(DNS only)으로
  바꾸거나 DNS 챌린지 사용 (이번에 겪은 함정)

### 16-3. nginx 설정 구조
```
/etc/nginx/snippets/unimotors-ssl.conf   ← 공통 SSL (모든 서브도메인 공유)
/etc/nginx/sites-available/
  ├─ unimotors-root   (랜딩: 정적 서빙, / 공개 + /app basic auth)
  ├─ unimotors-data   (10.10.0.9:8090 프록시, WebSocket)
  └─ unimotors-dash   (10.10.0.3:8080 프록시, SSE → proxy_buffering off 필요)
```
**공통 SSL snippet:**
```nginx
ssl_certificate     /etc/letsencrypt/live/unimotors.example.com/fullchain.pem;
ssl_certificate_key /etc/letsencrypt/live/unimotors.example.com/privkey.pem;
include /etc/letsencrypt/options-ssl-nginx.conf;
ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
```
**랜딩(root) — 공개 홍보 + /app만 인증:**
```nginx
server {
    listen 443 ssl; server_name unimotors.example.com;
    include snippets/unimotors-ssl.conf;
    root /var/www/unimotors; index index.html;
    location / { try_files $uri $uri/ =404; }              # 공개
    location /app {                                          # 로그인 필요
        auth_basic "UNIMOTORS";
        auth_basic_user_file /etc/nginx/.htpasswd;
        try_files $uri $uri/ /app/index.html;
    }
}
server { listen 80; server_name unimotors.example.com; return 301 https://$host$request_uri; }
```
**data (대시보드, WebSocket):**
```nginx
server {
    listen 443 ssl; server_name data.unimotors.example.com;
    include snippets/unimotors-ssl.conf;
    auth_basic "UNIMOTORS"; auth_basic_user_file /etc/nginx/.htpasswd;
    location / {
        proxy_pass http://10.10.0.9:8090;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;      # WebSocket 필수
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;
    }
}
```
**dash (계기판, SSE) — 버퍼링 꺼야 함:**
```nginx
    location / {
        ... (위와 동일) ...
        proxy_pass http://10.10.0.3:8080;
        proxy_buffering off;    # ★ SSE 실시간 위해 필수 (없으면 계기판 속도 멈춤)
        proxy_cache off;
    }
```
```bash
# basic auth 비번
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd unimotors
# 활성화
sudo ln -s /etc/nginx/sites-available/unimotors-{root,data,dash} /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### 16-4. WebSocket vs SSE 프록시 차이 (중요 교훈)
- **data 대시보드 = WebSocket** (`/live`, `/ingest`): `Upgrade`/`Connection` 헤더면 됨.
  버퍼링 문제 없음.
- **dash 계기판 = SSE** (Server-Sent Events): 두 가지가 모두 필요하다.
  1. **애플리케이션**: HTTP/1.1 + `Transfer-Encoding: chunked` + `X-Accel-Buffering: no` (23-2)
  2. **nginx**: `proxy_buffering off; proxy_cache off;` (보조)
  ⚠️ nginx만 고쳐도 안 된다 — 업스트림이 HTTP/1.0으로 길이 미확정 응답을 주면 막힌다.
- 증상: 핫스팟 직결(10.42.0.1:8080)은 계기판 속도 올라가는데, dash 도메인은 안 올라감
  → SSE 버퍼링이 원인.

## 17. Pi 4B 서버 배포 (실작업)

### 17-1. 기본
- OS: **Debian 13 trixie (aarch64)** — 테스팅이나 서버(유선)라 무방하다고 판단, 그대로 진행
- 전원: Pi 4는 **정식 PD 협상 안 함** (5V/3A 요구). **e-marker 60W 케이블은 rev1.1에서 문제**
  → 평범한 케이블/5V 어댑터로. `vcgencmd get_throttled`=0x0 확인 (언더볼티지 없음)
- hostname: `sudo hostnamectl set-hostname unimotors-srv`
  → `/etc/hosts`의 127.0.1.1에도 같은 이름 추가 (안 하면 sudo가 "unable to resolve host" 경고)

### 17-2. WireGuard (15-1 참고) → 서버 코드 배포
```bash
sudo apt install -y python3-aiohttp     # trixie는 apt로 최신. 안되면 pip --break-system-packages
# telemetry_server.py + dashboard.html 을 /home/unimotors/ 에 배치
python3 telemetry_server.py             # [SERVER] ... on :8090 확인
```

### 17-3. systemd 등록 (부팅 자동 + VPN 이후 실행)
`/etc/systemd/system/telemetry.service`:
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
sudo systemctl status telemetry.service    # active (running) 확인됨
```

### 17-4. 검증 (통과함)
```bash
# OCI에서 VPN 너머 서버 API
curl -s http://10.10.0.9:8090/api/status
# → {"online": [], "vehicles": []}  정상
```
전체 경로 확인: 브라우저 → data.도메인(HTTPS) → OCI nginx(basic auth) → VPN 10.10.0.9
→ Pi4 telemetry_server → 대시보드 표시 + "차량 신호 없음" 로직 작동.

### 17-5. ⭐ Leaflet integrity 해시 함정 (두 번 겪음)
- dashboard.html의 Leaflet CDN `integrity` 해시가 틀리면 **브라우저가 스크립트 차단**
  → `L is not defined` → 지도 안 뜨고 JS 연쇄 실패.
- 올바른 값 (1.9.4): JS = `sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=`
  (대문자 `VM`. 소문자 `Vm`이면 틀림 — 이 오타로 차단됨)
- 정확한 해시 직접 계산:
  `curl -s <url> | openssl dgst -sha256 -binary | openssl base64 -A`
- 브라우저 콘솔의 `computed SHA-256 integrity '...'` 값이 실제 정답.
- 해결: 해시를 올바르게 고치거나(권장, 무결성 보호 유지), integrity/crossorigin 속성 제거.

## 18. 실차 1차 테스트 (개인차) 결과와 과제

### 18-1. 테스트 구성
- 개인차에 차량 Pi(Zero) 탑재, `car1` 프리셋 그대로 (BMS 없음 → `[BMS] 오류` 정상, 배터리 `--`)
- gps-dashboard.service로 실행, `[UP] 서버 연결: ws://10.10.0.9:8090/ingest` 확인
- 결과: **GPS fix 잡히고 대시보드에 실시간 위치·속도·궤적 표시 성공** (첫 실증)

### 18-2. 발견된 문제
1. **궤적이 구불구불** (길 안 따라감)
   - 원인: GPS 노이즈 + 필터 없음 + 위성 수 적음(4~6개, GPS만 켜진 듯)
   - 해결책 A(하드/설정): `AT+CGNSSMODE=15,1`로 GPS+GLONASS+BeiDou+Galileo 전부 → 위성 2~4배
     ```
     AT+CGPS=0
     AT+CGNSSMODE=15,1
     AT+CGPS=1,1
     ```
   - 해결책 B(소프트, 예정): 좌표 필터 — 정지 시 점 스킵 + 최소 이동거리(예 3m) 미만 무시
   - 둘 다 해야 깔끔. 안테나도 하늘 보이는 곳에.
2. **dash 도메인에서 계기판 속도 안 올라감** (핫스팟 직결은 정상)
   - 원인: SSE를 nginx가 버퍼링. `proxy_buffering off` 추가함 (16-3), 실험 대기.

### 18-3. 추가된 기능
- **대시보드 스크러버** (주행 분석 탭): 세션 궤적 로드 후 시간축 슬라이더로 재생.
  드래그 시 그 시점 위치가 지도 마커 + 속도/G 정보 + 스파크라인 현재위치선.
  (코드/문법 검증 완료. Leaflet 로드되는 실환경에서 동작)

### 18-4. 주행 후 데이터 검증 방법
```bash
# 차량: 로컬 telem CSV를 서버로 (정밀 궤적 + 구멍 보정)
python3 ~/backfill.py --all
# 서버(Pi4): BMS 없을 때 로깅이 빈칸(NULL)인지 확인
sqlite3 ~/telemetry.db "SELECT ts,speed,lat,lon,voltage,soc FROM summary ORDER BY ts DESC LIMIT 5;"
# → speed/lat/lon 값 있고 voltage/soc는 NULL이면 정상 (BMS 경로는 살아있고 값만 없음)
```

## 19. 파일 배치 최종 요약 (기기별)

```
[OCI VPS 10.10.0.1]  nginx + WireGuard 허브
  /etc/wireguard/wg0.conf                     (4 피어)
  /etc/nginx/snippets/unimotors-ssl.conf
  /etc/nginx/sites-available/unimotors-{root,data,dash}
  /etc/nginx/.htpasswd
  /etc/letsencrypt/... (와일드카드 인증서 + cloudflare.ini)
  /var/www/unimotors/index.html               (랜딩 = landing_index.html)
  /var/www/unimotors/app/index.html           (포털 = portal_app_index.html)

[기숙사 Pi4 10.10.0.9]  unimotors-srv, 서버
  /home/unimotors/telemetry_server.py
  /home/unimotors/dashboard.html
  /home/unimotors/telemetry.db                (자동 생성)
  /etc/wireguard/wg0.conf
  /etc/systemd/system/telemetry.service

[차량 Pi 10.10.0.3]  unimotors-car, 수집+계기판
  /home/unimotors/gps_server.py               (PRESET, SERVER_WS_URL=10.10.0.9)
  /home/unimotors/bms_reader.py
  /home/unimotors/backfill.py                 (DEFAULT_SERVER=10.10.0.9)
  /home/unimotors/lte_auto.sh
  /etc/wireguard/wg0.conf
  /etc/systemd/system/{lte-auto,gps-dashboard}.service
```

---

← [하드웨어 이관과 배포 체크리스트](09-hardware-migration.md) · [문서 목차](README.md) · [데이터 모델 (32필드 스키마 · 시계 점프)](11-data-model.md) →
