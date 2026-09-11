#!/usr/bin/env bash
# UNIMOTORS — 차량 Pi 설치 스크립트
#   sudo bash scripts/install-vehicle.sh
# 자세한 설명: docs/13-setup-vehicle.md
set -euo pipefail

USER_NAME="${UNIMOTORS_USER:-unimotors}"
HOME_DIR="/home/$USER_NAME"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CAN_METHOD="${CAN_METHOD:-udev}"     # udev | service

[[ $EUID -eq 0 ]] || { echo "sudo 로 실행하세요."; exit 1; }
id "$USER_NAME" >/dev/null 2>&1 || { echo "계정 $USER_NAME 이 없습니다."; exit 1; }

echo "==> 1/6 패키지 설치"
apt-get update
apt-get install -y minicom udhcpc can-utils wireguard python3-pip curl
pip3 install -r "$REPO/vehicle/requirements.txt" --break-system-packages
systemctl stop ModemManager 2>/dev/null || true
systemctl disable ModemManager 2>/dev/null || true

echo "==> 2/6 코드 배치 -> $HOME_DIR"
install -o "$USER_NAME" -g "$USER_NAME" -m 644 \
  "$REPO"/vehicle/gps_server.py "$REPO"/vehicle/bms_reader.py "$REPO"/vehicle/backfill.py "$HOME_DIR/"
install -o "$USER_NAME" -g "$USER_NAME" -m 755 \
  "$REPO"/deploy/vehicle/lte_auto.sh "$REPO"/deploy/vehicle/kiosk.sh "$HOME_DIR/"
install -d -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/gps_logs"

echo "==> 3/6 환경파일"
if [[ ! -f /etc/default/unimotors ]]; then
  install -m 600 "$REPO/.env.example" /etc/default/unimotors
  echo "    /etc/default/unimotors 생성 — UNIMOTORS_TOKEN 을 반드시 바꾸세요."
else
  echo "    /etc/default/unimotors 이미 존재 — 건드리지 않음"
fi

echo "==> 4/6 systemd 유닛"
install -m 644 "$REPO"/deploy/vehicle/{lte-auto.service,gps-dashboard.service,backfill.service,backfill.timer} \
  /etc/systemd/system/

echo "==> 5/6 CAN 자동 up ($CAN_METHOD)"
case "$CAN_METHOD" in
  udev)
    install -m 644 "$REPO/deploy/vehicle/90-can.rules" /etc/udev/rules.d/90-can.rules
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=net --action=add || true
    ;;
  service)
    install -m 644 "$REPO/deploy/vehicle/can0.service" /etc/systemd/system/
    systemctl enable can0.service
    ;;
  *) echo "CAN_METHOD 는 udev 또는 service"; exit 1 ;;
esac

echo "==> 6/6 서비스 활성화"
systemctl daemon-reload
systemctl enable --now lte-auto.service
systemctl enable --now gps-dashboard.service
systemctl enable --now backfill.timer

cat <<'MSG'

설치 완료.

남은 수동 작업:
  1) /etc/default/unimotors 의 UNIMOTORS_TOKEN 을 서버와 동일하게 설정
  2) WireGuard: deploy/wg0-client.conf.example 를 참고해 /etc/wireguard/wg0.conf 작성
     sudo systemctl enable --now wg-quick@wg0
  3) 핫스팟(nmcli) — docs/05-hotspot-captive.md
     ⚠️ 실행 순간 wlan0 가 AP 모드로 바뀌어 WiFi SSH 가 끊긴다. 반드시 VPN 세션에서.
  4) (선택) DSI 키오스크 — docs/13-setup-vehicle.md 24-12

확인:
  systemctl is-active lte-auto gps-dashboard
  systemctl list-timers backfill.timer
  ip link show can0
MSG
