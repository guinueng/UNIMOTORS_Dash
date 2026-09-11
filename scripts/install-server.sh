#!/usr/bin/env bash
# UNIMOTORS — 서버 Pi4 설치 스크립트
#   sudo bash scripts/install-server.sh
# 자세한 설명: docs/14-setup-server.md
set -euo pipefail

USER_NAME="${UNIMOTORS_USER:-unimotors}"
HOME_DIR="/home/$USER_NAME"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "sudo 로 실행하세요."; exit 1; }
id "$USER_NAME" >/dev/null 2>&1 || { echo "계정 $USER_NAME 이 없습니다."; exit 1; }

echo "==> 1/4 패키지 설치"
apt-get update
apt-get install -y wireguard python3-aiohttp sqlite3

echo "==> 2/4 코드 배치 -> $HOME_DIR"
install -o "$USER_NAME" -g "$USER_NAME" -m 644 \
  "$REPO/server/telemetry_server.py" "$REPO/server/dashboard.html" "$HOME_DIR/"

echo "==> 3/4 환경파일"
if [[ ! -f /etc/default/unimotors ]]; then
  install -m 600 "$REPO/.env.example" /etc/default/unimotors
  echo "    /etc/default/unimotors 생성 — UNIMOTORS_TOKEN 을 차량과 동일하게 설정하세요."
fi

echo "==> 4/4 서비스"
install -m 644 "$REPO/deploy/server/telemetry.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now telemetry.service

cat <<'MSG'

설치 완료.

남은 수동 작업:
  1) /etc/default/unimotors 의 UNIMOTORS_TOKEN 을 차량과 동일하게 설정 후
     sudo systemctl restart telemetry.service
  2) WireGuard: deploy/wg0-client.conf.example 참고 (Address = 10.10.0.9/24)
     sudo systemctl enable --now wg-quick@wg0

확인:
  systemctl status telemetry.service
  curl -s http://127.0.0.1:8090/api/status     # {"online":[],"vehicles":[]}
MSG
