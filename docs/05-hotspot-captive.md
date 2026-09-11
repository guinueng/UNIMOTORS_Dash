# 로컬 핫스팟 + 캡티브 포털

> nmcli AP 모드 · 인터넷 확인 요청 대응 3가지 방식

---

## 6. 4단계: 로컬 핫스팟 (운전자 폰 계기판)

> 순수 로컬 대시보드 전용. 인터넷 공유(NAT) 안 함.
> 폰은 WiFi(계기판) + 자기 LTE(인터넷) 동시 사용 가능 (목적지별 자동 분리).

### 6-1. 영구 핫스팟 프로파일 (부팅 자동)
> ⚠️ 실행 순간 wlan0가 발신 모드로 바뀌어 **WiFi SSH 끊김**.
> 반드시 **WireGuard VPN(LTE 경유) 세션에서** 실행. `who`로 `10.10.0.2` 확인.
```bash
sudo nmcli connection add type wifi ifname wlan0 con-name unimotors-hotspot \
  autoconnect yes ssid UNIMOTORS_DASH
sudo nmcli connection modify unimotors-hotspot \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  ipv4.method shared \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "<핫스팟_비밀번호>"
sudo nmcli connection up unimotors-hotspot
```
- `band bg` = 2.4GHz (Zero W 필수), `ipv4.method shared` = 10.42.0.1 게이트웨이 + DHCP
- 폰에서 `UNIMOTORS_DASH` 접속 → `http://10.42.0.1:8080`

### 6-2. ⭐ 캡티브 포털 ("인터넷 연결 없음" 문제)
폰이 WiFi에 붙으면 **인터넷 확인 요청**을 보내고, 응답이 없으면 "인터넷 없음"으로
판정해 경고를 띄우거나 WiFi를 끊는다.

| OS | 요청 URL | 포트 |
|---|---|---|
| Android | `connectivitycheck.gstatic.com/generate_204` | **80** |
| iOS | `captive.apple.com/hotspot-detect.html` | **80** |
| Windows | `www.msftconnecttest.com/connecttest.txt` | **80** |

⚠️ **`gps_server.py`의 `_captive()` 만으로는 동작하지 않는다.** 두 가지가 막혀 있다:
1. **포트** — 서버는 8080, 폰은 80으로 요청
2. **DNS** — 체크 도메인이 실제 구글/애플 IP로 해석되어 연결 자체가 실패

**핵심 트레이드오프**: 체크에 "성공"으로 답하면 폰이 *"이 WiFi는 인터넷이 된다"*고 믿고
**모든 트래픽을 WiFi로 보낸다.** 실제 인터넷이 없으면 폰 인터넷이 완전히 끊겨 더 나빠진다.
그래서 "성공" 응답은 **실제 인터넷(NAT)을 제공할 때만** 쓴다.

**세 가지 선택지**
| 방식 | 폰 상태 | 폰 인터넷 | LTE 데이터 |
|---|---|---|---|
| **A. LTE 공유(NAT)** | 경고 없음 | Pi의 LTE 경유 | 소모됨 |
| **B. 캡티브 포털(302)** | "로그인 필요" | **셀룰러 유지** | 없음 |
| **C. 폰에서 무시 설정** | "인터넷 없음" | 셀룰러 유지 | 없음 |

원래 설계 의도(*폰은 자기 LTE, WiFi는 계기판 전용*)에 맞는 것은 **B**다.

**코드 설정** (`gps_server.py`)
```python
CAPTIVE_MODE = "portal"                        # portal | success | off
CAPTIVE_REDIRECT = "http://10.42.0.1:8080/"
```
- `portal`  : 302 리다이렉트 → 폰이 캡티브로 인식, 셀룰러 유지 (NAT 없을 때 권장)
- `success` : 204/Success → **NAT 켰을 때만** 사용
- `off`     : 404

**방식 B 실제 설정 2단계**

① DNS 하이재킹 (체크 도메인만 Pi 로)
```bash
sudo mkdir -p /etc/NetworkManager/dnsmasq-shared.d
sudo tee /etc/NetworkManager/dnsmasq-shared.d/captive.conf > /dev/null <<'EOF'
address=/connectivitycheck.gstatic.com/10.42.0.1
address=/connectivitycheck.android.com/10.42.0.1
address=/clients3.google.com/10.42.0.1
address=/captive.apple.com/10.42.0.1
address=/www.msftconnecttest.com/10.42.0.1
address=/www.msftncsi.com/10.42.0.1
address=/detectportal.firefox.com/10.42.0.1
EOF
sudo nmcli connection down unimotors-hotspot
sudo nmcli connection up unimotors-hotspot
```
> `address=/#/10.42.0.1`(전체 하이재킹)은 폰의 다른 앱까지 가로채므로 쓰지 않는다.

② 포트 80 → 8080 리다이렉트 (**wlan0 에만**)
```bash
sudo iptables -t nat -A PREROUTING -i wlan0 -p tcp --dport 80 -j REDIRECT --to-port 8080
sudo netfilter-persistent save
```

**확인**
```bash
curl -s -o /dev/null -w "%{http_code} -> %{redirect_url}\n" \
  -H "Host: connectivitycheck.gstatic.com" http://10.42.0.1/generate_204
# 302 -> http://10.42.0.1:8080/  이면 정상
```

**방식 A (LTE 공유)로 갈 경우**
```bash
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-hotspot.conf
sudo sysctl -p /etc/sysctl.d/99-hotspot.conf
sudo iptables -t nat -A POSTROUTING -o wwan0 -j MASQUERADE
sudo iptables -A FORWARD -i wlan0 -o wwan0 -j ACCEPT
sudo iptables -A FORWARD -i wwan0 -o wlan0 -m state --state RELATED,ESTABLISHED -j ACCEPT
sudo netfilter-persistent save
```
> `ipv4.method shared`가 이미 NAT를 구성하므로, 안 되면 먼저 진단할 것:
> `ip route`(default가 wwan0인지), `ping -I wwan0 8.8.8.8`,
> `iptables -t nat -L POSTROUTING -n | grep -i masq`, `cat /proc/sys/net/ipv4/ip_forward`
> 폰에서 `http://1.1.1.1`이 되면 NAT는 정상이고 DNS만 문제다.

**방식 C (폰 설정)**
- Android: "인터넷에 연결되어 있지 않습니다" 알림 → **연결 유지 / 항상**
- iOS: 보통 자동으로 셀룰러 유지(Wi-Fi Assist)

**권장 조합**
| 상황 | 권장 |
|---|---|
| 데이터 여유 있음, 경험 매끄럽게 | **A**(NAT) + `CAPTIVE_MODE="success"` |
| 폰의 셀룰러 유지 (원래 설계) | **B**(portal) + DNS 하이재킹 + 80→8080 |
| 대회 당일 급함 | **C**(폰에서 "연결 유지" 선택) |
| **주행 중 폰으로 통화·오더 필요** | **24-12 (Pi 내장 디스플레이)** — 폰을 아예 안 씀 |

> ⭐ **가장 좋은 해법은 폰을 계기판으로 쓰지 않는 것이다.** DSI LCD를 달아
> `localhost`로 계기판을 띄우면(24-12) 이 문제 전체가 사라지고 폰은 셀룰러 100%로 자유롭다.
> 핫스팟은 피트 크루용 보조로만 남긴다.

**주의사항**
- **`CAPTIVE_MODE="success"`를 NAT 없이 쓰지 말 것.** 폰이 WiFi로 모든 트래픽을 보내
  인터넷이 완전히 끊긴다.
- 포트 80 리다이렉트는 **`-i wlan0`으로 제한**한다. 전체 인터페이스에 걸면 VPN/LTE
  트래픽까지 가로챈다.
- iptables 규칙은 `sudo netfilter-persistent save`로 저장(재부팅 시 소실).
- 핫스팟 설정 변경 후 `nmcli connection down/up`으로 dnsmasq를 재시작해야 DNS가 반영된다.
- **폰에서 `UNIMOTORS_DASH` 자동 연결을 끄는 것**을 잊지 말 것 — 차 근처에서 폰이
  자동으로 붙어 인터넷을 잃는 사고를 막는다.

### 6-3. 계기판 서버 systemd `/etc/systemd/system/gps-dashboard.service`
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
```bash
sudo systemctl enable --now gps-dashboard.service
```

---

← [GPS 10Hz 계기판](04-gps.md) · [문서 목차](README.md) · [WireGuard VPN 메시](06-wireguard.md) →
