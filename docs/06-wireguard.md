# WireGuard VPN 메시

> OCI 허브 · 피어 구성 · CGNAT keepalive · 방화벽

---

## 7. 5단계: WireGuard VPN (원격 관리)

> CGNAT(LTE) 뒤의 차량에 외부에서 접속하기 위한 관리 채널.
> OCI를 공인 IP 허브로, 차량·서버·클라이언트가 가상 사설망(10.10.0.0/24)을 이룸.

### 7-1. 메시 구성 (완료됨)
```
OCI 10.10.0.1 (허브, 공인 <OCI_PUBLIC_IP>, UDP 51820)
├─ 10.10.0.2  노트북 (PC)
├─ 10.10.0.3  차량 Pi (PersistentKeepalive=25 필수 — CGNAT NAT 유지)
└─ 10.10.0.9  기숙사 Pi4 서버 (실제 배포됨)
```

### 7-2. 서버 conf `/etc/wireguard/wg0.conf` (핵심 구조)
```ini
[Interface]
Address = 10.10.0.1/24
ListenPort = 51820
PrivateKey = <server_private>

[Peer]                       # PC
PublicKey = <pc_public>
PresharedKey = <psk>
AllowedIPs = 10.10.0.2/32

[Peer]                       # 차량 Pi
PublicKey = <pi_public>
PresharedKey = <psk>
AllowedIPs = 10.10.0.3/32
```
- 서버 쪽 `AllowedIPs .../32` = 피어 신원 지정 (클라이언트의 라우팅 지정과 의미 다름)

### 7-3. 서버 방화벽 (2곳 모두 필수)
```bash
# 커널 IP 포워딩 (피어 간 통신)
echo 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/99-wireguard.conf
sudo sysctl -p /etc/sysctl.d/99-wireguard.conf
# iptables: WireGuard 포트 + 피어간 포워딩
sudo iptables -I INPUT -p udp --dport 51820 -j ACCEPT
sudo iptables -I FORWARD 1 -i wg0 -o wg0 -j ACCEPT
sudo netfilter-persistent save
```
> **OCI Security List에서도 UDP 51820 인바운드 개방 필수** (SSH는 22/TCP).
> WireGuard는 UDP인 것 주의!

### 7-4. 차량 Pi conf (차이점: Address .3, Keepalive)
```ini
[Interface]
PrivateKey = <pi_private>
Address = 10.10.0.3/24

[Peer]
PublicKey = <server_public>
PresharedKey = <psk>
Endpoint = <OCI_PUBLIC_IP>:51820
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
```
> ⭐ `PersistentKeepalive = 25` — Pi는 CGNAT 뒤라 25초마다 패킷 보내 NAT 구멍 유지.
> 없으면 몇 분 뒤 외부→Pi 접속(원격 SSH) 불가.
> `AllowedIPs = 10.10.0.0/24` — VPN 대역만 (0.0.0.0/0 금지: LTE/핫스팟 라우팅 꼬임).

### 7-5. 새 피어 추가 (기숙사 Pi4 = 10.10.0.9)
```bash
# OCI 서버에서
cd ~; umask 077
wg genkey | tee pi_srv_private.key | wg pubkey | tee pi_srv_public.key
# wg0.conf에 [Peer] 추가 (AllowedIPs = 10.10.0.9/32), restart
sudo systemctl restart wg-quick@wg0
```

### 7-6. ⭐ 백업 (이 프로젝트 이전 서버는 SSH 키 분실로 접근 불가된 이력)
백업 대상: SSH 개인키/공개키, 서버 WireGuard 설정, 공인 IP.
> 개인키는 재생성 불가, 공개키는 재생성 가능. **개인키 반드시 백업.**

---

← [로컬 핫스팟 + 캡티브 포털](05-hotspot-captive.md) · [문서 목차](README.md) · [Daly BMS CAN 통신](07-bms-can.md) →
