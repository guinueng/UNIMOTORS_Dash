# OCI VPS 전체 세팅 순서

> WireGuard 허브 · 방화벽 · nginx · certbot

---

## 26. OCI VPS 전체 세팅 순서

```bash
# 1. WireGuard 허브
sudo apt install -y wireguard
umask 077
wg genkey | tee server_private.key | wg pubkey > server_public.key
wg genpsk > shared.psk
sudo chmod 600 server_private.key shared.psk
sudo nano /etc/wireguard/wg0.conf     # Address=10.10.0.1/24, ListenPort=51820, 피어들

# 2. 커널 포워딩
echo 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/99-wireguard.conf
sudo sysctl -p /etc/sysctl.d/99-wireguard.conf

# 3. 방화벽 (iptables + OCI Security List 콘솔 둘 다!)
sudo iptables -I INPUT -p udp --dport 51820 -j ACCEPT      # VPN (UDP!)
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT         # HTTP
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT        # HTTPS
sudo iptables -I FORWARD 1 -i wg0 -o wg0 -j ACCEPT         # 피어 간 통신
# 폰 풀터널(0.0.0.0/0) 쓸 경우에만:
sudo iptables -t nat -A POSTROUTING -s 10.10.0.0/24 -o ens3 -j MASQUERADE
sudo iptables -I FORWARD 1 -i wg0 -o ens3 -j ACCEPT
sudo iptables -I FORWARD 1 -i ens3 -o wg0 -m state --state RELATED,ESTABLISHED -j ACCEPT
sudo netfilter-persistent save                              # ⭐ 필수 (안 하면 재부팅 시 소실)

sudo systemctl enable --now wg-quick@wg0

# 4. nginx + 와일드카드 인증서 (16장 참고)
sudo apt install -y nginx apache2-utils certbot python3-certbot-dns-cloudflare
sudo htpasswd -c /etc/nginx/.htpasswd unimotors
sudo certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  -d "unimotors.example.com" -d "*.unimotors.example.com"

# 5. 웹 파일
sudo mkdir -p /var/www/unimotors/app
# landing_index.html  → /var/www/unimotors/index.html
# portal_app_index.html → /var/www/unimotors/app/index.html
```

### 26-1. 인증서 자동 갱신 확인
certbot이 systemd 타이머를 자동 등록한다. 확인:
```bash
systemctl list-timers certbot.timer
sudo certbot renew --dry-run           # 갱신 리허설 (에러 없으면 OK)
```

### 26-2. iptables 영속성 확인 (재부팅 후)
```bash
sudo reboot
# 재접속 후
sudo iptables -L INPUT -n | grep -E '51820|:80|:443'
sudo iptables -t nat -L POSTROUTING -n | grep MASQUERADE
```
> 규칙이 사라졌으면 `netfilter-persistent save`를 안 한 것.
> `sudo apt install iptables-persistent` 로 패키지 설치 여부도 확인.

---

← [서버 Pi4 전체 세팅 순서](14-setup-server.md) · [문서 목차](README.md) · [트러블슈팅 종합](16-troubleshooting.md) →
