#!/usr/bin/env bash
# =====================================================================
#  UNIMOTORS Raspberry Pi 자동 세팅 (install-pi.sh v3.1)
#
#  링크 한 줄 설치:
#    curl -fsSL <BASE_URL>/scripts/install-pi.sh | bash -s -- --role demo --base-url <BASE_URL>
#  로컬 파일로 설치 (스크립트와 같은 폴더에 .py 파일들):
#    bash install-pi.sh --role demo --src .
#
#  역할(--role)
#    demo : 가상 주행 계기판 (demo_sim.py + gps_server.py v7 + race_runtime.py, 서버로 vehicle=demo 전송)
#    car  : 실차 수집기 (gps_server.py + bms_reader.py + backfill.py + lte_auto.sh)
#
#  하는 일 (마스터 문서 V6 37장을 자동화)
#    1. APT 저장소 HTTPS 전환 + 전체 업데이트(update/upgrade) + 패키지 설치
#    2. 코드 내려받기 (기존 파일은 타임스탬프 백업 후 교체)
#    3. systemd 서비스 생성 (계정명/홈 경로 자동 감지)
#    4. Wayland 키오스크 autostart + 화면 꺼짐 해제 (데스크톱 이미지)
#    5. 서버 업로드 토큰 (선택, 입력 숨김)
#    6. WireGuard — 값은 직접 입력, OCI 에 등록할 [Peer] 블록을 파일로 만들어 공유
#    7. 교내 와이파이 프로필 (선택, 프로필만 추가하고 전환은 하지 않음)
#
#  비밀값(PSK, 토큰, 와이파이 비밀번호)은 화면에 표시하지 않고, 로그와 공유 파일에도 남기지 않는다.
#  다시 실행해도 안전하다 (업데이트로 동작, 덮어쓰는 파일은 전부 백업).
# =====================================================================
set -euo pipefail

SCRIPT_VERSION="v3.1"
ROLE="demo"
BASE_URL="${UNIMOTORS_BASE_URL:-}"
SRC_DIR=""
DRY=0
ASSUME_DEFAULTS=0
DO_WG="ask"          # ask | yes | no
DO_WIFI="ask"        # ask | yes | no
DO_UPGRADE=1         # 0 = --no-upgrade
WG_IP="${WG_IP:-}"
WG_ENDPOINT="${WG_ENDPOINT:-}"
WG_SERVER_PUB="${WG_SERVER_PUB:-}"
WG_HUB_IP="10.10.0.1"
SERVER_IP="10.10.0.9"
TS="$(date +%Y%m%d_%H%M%S)"

usage() {
  cat <<'EOF'
사용법: install-pi.sh [옵션]
  --role demo|car         설치할 역할 (기본 demo)
  --base-url URL          파일을 받을 주소 (레포 raw 루트. 예: https://raw.githubusercontent.com/<계정>/unimotors-telemetry/main)
  --src DIR               파일을 로컬 폴더에서 복사 (DIR/vehicle/*.py 또는 DIR/*.py)
  --wg-ip 10.10.0.X       이 기기의 VPN IP
  --wg-endpoint IP:PORT   OCI 허브 엔드포인트 (공개해도 되는 값, 팀 공유용)
  --wg-server-pub KEY     OCI 허브 공개키 (공개해도 되는 값, 팀 공유용)
  --no-wg / --wg          WireGuard 단계 건너뛰기 / 묻지 않고 진행
  --no-wifi / --wifi      교내 와이파이 단계 건너뛰기 / 묻지 않고 진행
  --no-upgrade            apt-get upgrade 생략 (빠른 재실행용. update 는 항상 실행)
  --yes                   질문에 기본값으로 답함 (선택 단계는 건너뜀)
  --dry-run               실제로 바꾸지 않고 할 일만 출력
  -h, --help              도움말
환경변수: UNIMOTORS_BASE_URL, WG_IP, WG_ENDPOINT, WG_SERVER_PUB
          WG_PSK, UNIMOTORS_TOKEN (자동화용. 셸 기록에 남지 않게 주의)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --base-url) BASE_URL="$2"; shift 2 ;;
    --src) SRC_DIR="$2"; shift 2 ;;
    --wg-ip) WG_IP="$2"; shift 2 ;;
    --wg-endpoint) WG_ENDPOINT="$2"; shift 2 ;;
    --wg-server-pub) WG_SERVER_PUB="$2"; shift 2 ;;
    --no-wg) DO_WG="no"; shift ;;
    --wg) DO_WG="yes"; shift ;;
    --no-wifi) DO_WIFI="no"; shift ;;
    --no-upgrade) DO_UPGRADE=0; shift ;;
    --wifi) DO_WIFI="yes"; shift ;;
    --yes) ASSUME_DEFAULTS=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "알 수 없는 옵션: $1"; usage; exit 1 ;;
  esac
done

case "$ROLE" in demo|car) ;; *) echo "--role 은 demo 또는 car"; exit 1 ;; esac

# ---------------------------------------------------------------------
#  출력 / 입력 도우미
# ---------------------------------------------------------------------
if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'
else B=""; G=""; Y=""; R=""; C=""; N=""; fi
STEP=0
step() { STEP=$((STEP+1)); echo; echo "${B}${C}[$STEP] $*${N}"; }
ok()   { echo "  ${G}✓${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; }
die()  { echo "  ${R}✗ $*${N}"; exit 1; }

HAS_TTY=0
if [ "$ASSUME_DEFAULTS" -eq 0 ] && { : < /dev/tty; } 2>/dev/null; then HAS_TTY=1; fi

ask() {                     # ask "질문" "기본값"  → 답을 stdout 으로
  local q="$1" def="${2:-}" ans=""
  if [ "$HAS_TTY" -eq 0 ]; then echo "$def"; return; fi
  sleep 0.2                 # 로그(tee)로 가는 진행 메시지가 질문보다 먼저 보이도록
  if [ -n "$def" ]; then printf "  %s [%s]: " "$q" "$def" > /dev/tty
  else printf "  %s: " "$q" > /dev/tty; fi
  IFS= read -r ans < /dev/tty || true
  echo "${ans:-$def}"
}
ask_secret() {              # 입력이 화면에 보이지 않음
  local q="$1" ans=""
  if [ "$HAS_TTY" -eq 0 ]; then echo ""; return; fi
  sleep 0.2
  printf "  %s (입력 숨김): " "$q" > /dev/tty
  IFS= read -rs ans < /dev/tty || true
  echo > /dev/tty
  echo "$ans"
}
confirm() {                 # confirm "질문" y|n
  local a; a="$(ask "$1 (y/n)" "$2")"
  [[ "$a" =~ ^[Yy] ]]
}

run() {                     # 명령 실행 (dry-run 이면 출력만)
  if [ "$DRY" -eq 1 ]; then echo "    + $*"; else "$@"; fi
}
put_root() {                # put_root 경로 모드  (내용은 stdin)
  local f="$1" mode="${2:-644}"
  if [ "$DRY" -eq 1 ]; then echo "    + write $f ($mode)"; cat > /dev/null; return; fi
  sudo tee "$f" > /dev/null
  sudo chmod "$mode" "$f"
}
put_user() {                # put_user 경로  (내용은 stdin, 소유자 = 대상 사용자)
  local f="$1"
  if [ "$DRY" -eq 1 ]; then echo "    + write $f"; cat > /dev/null; return; fi
  mkdir -p "$(dirname "$f")"
  cat > "$f"
}
backup() {                  # 덮어쓰기 전에 타임스탬프 사본 (root 파일이면 sudo)
  local f="$1"
  [ -e "$f" ] || return 0
  if [ -w "$f" ] && [ -w "$(dirname "$f")" ]; then run cp -a "$f" "$f.bak-$TS"
  else run sudo cp -a "$f" "$f.bak-$TS"; fi
  echo "    백업: $f.bak-$TS"
}

# BEGIN TESTABLE INSTALL HELPERS
save_upload_token() {        # 기존 EnvironmentFile 보존, 토큰은 stdin으로만 전달
  local f="$1" token="$2"
  if [ "$DRY" -eq 1 ]; then echo "    + update token in $f (600, other settings preserved)"; return; fi
  printf '%s' "$token" | sudo python3 -c '
import os
from pathlib import Path
import re
import sys
import tempfile

path = Path(sys.argv[1]).resolve()
token = sys.stdin.read()
if not token or any(c in token for c in ("\x00", "\r", "\n")):
    raise SystemExit("Token must be a nonempty single line")
original = path.read_bytes() if path.exists() else b""
text = original.decode("utf-8")
# EnvironmentFile supports quoted multiline values. Keep legacy records intact;
# the managed assignment at EOF overrides earlier token assignments (systemd).
marker = "# UNIMOTORS installer managed upload token"
tail = re.compile(r"(?:\n)?" + re.escape(marker) + r"\nUNIMOTORS_TOKEN=\"(?:[^\"\\\r\n]|\\.)*\"\n?\Z")
text = tail.sub("", text)
encoded = token
for char in ("\\", "\"", "$", "`"):
    encoded = encoded.replace(char, "\\" + char)
if text and not text.endswith("\n"):
    text += "\n"
updated = (text + marker + "\nUNIMOTORS_TOKEN=\"" + encoded + "\"\n").encode("utf-8")
if updated == original:
    os.chmod(path, 0o600)
    raise SystemExit(0)
fd, temp = tempfile.mkstemp(prefix=".unimotors-env-", dir=path.parent)
try:
    os.chmod(temp, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(updated)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
finally:
    if os.path.exists(temp):
        os.unlink(temp)
' "$f"
}

validate_staged_code() {     # 실행 파일을 교체하기 전에 v7 공통 모듈·화면 검사
  local stage="$1"
  python3 -B - "$stage" <<'PY'
from pathlib import Path
import sys

stage = Path(sys.argv[1])
required = ("gps_server.py", "race_runtime.py", "telemetry_protocol.py", "driver_dashboard.html", "claude_dashboard.html")
missing = [name for name in required if not (stage / name).is_file()]
if missing:
    raise SystemExit("v7 필수 파일 누락: " + ", ".join(missing))
for path in stage.glob("*.py"):
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
sys.path.insert(0, str(stage))
import gps_server
if not hasattr(gps_server, "RaceRuntime") or not hasattr(gps_server, "V7_FIELDS"):
    raise SystemExit("gps_server.py v7/RaceRuntime 구성이 필요합니다.")
if (stage / "demo_sim.py").is_file():
    import demo_sim
    if not all(callable(getattr(demo_sim, name, None)) for name in ("serve_v7", "demo_profile")):
        raise SystemExit("demo_sim.py의 v7 입력 어댑터가 없습니다. v6 데모와 v7 런타임을 혼합 설치하지 마세요.")
page = (stage / "driver_dashboard.html").read_text(encoding="utf-8")
if "<html" not in page or 'id="phase"' not in page:
    raise SystemExit("v7 운전자 화면이 비어 있거나 지원하지 않는 형식입니다.")
page = (stage / "claude_dashboard.html").read_text(encoding="utf-8")
if "<html" not in page or 'id="theme"' not in page:
    raise SystemExit("Claude 테마 파일이 비어 있거나 지원하지 않는 형식입니다.")
PY
}

cleanup_stage() {
  [ -n "${STAGING_DIR:-}" ] || return 0
  # 이 설치기가 만든 평면 디렉터리의 파일만 지웁니다. 재귀 삭제하지 않습니다.
  local rel
  for rel in "${FILES[@]}"; do rm -f -- "$STAGING_DIR/$(basename "$rel")"; done
  rmdir -- "$STAGING_DIR" 2>/dev/null || true
}
# END TESTABLE INSTALL HELPERS

# ---------------------------------------------------------------------
#  환경 확인
# ---------------------------------------------------------------------
if [ "$(id -u)" -eq 0 ]; then
  die "root 로 실행하지 마세요. 일반 계정으로 실행하면 필요한 곳에서 sudo 를 씁니다."
fi
USER_NAME="$(id -un)"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
HOST_NAME="$(hostname)"
MODEL="$( (tr -d '\0' < /proc/device-tree/model) 2>/dev/null || echo unknown)"
OS_NAME="$( . /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || echo unknown)"
LOG="$HOME_DIR/unimotors-install-$TS.log"

echo "${B}UNIMOTORS Pi 자동 세팅 $SCRIPT_VERSION${N}"
echo "  역할      : $ROLE"
echo "  기기      : $MODEL"
echo "  OS        : $OS_NAME"
echo "  계정/홈   : $USER_NAME  $HOME_DIR"
echo "  호스트명  : $HOST_NAME"
[ "$DRY" -eq 1 ] && echo "  ${Y}DRY-RUN: 아무것도 바꾸지 않고 할 일만 출력합니다${N}"

if [ "$ROLE" = "car" ] && [ "$USER_NAME" != "unimotors" ]; then
  warn "차량 역할인데 계정이 '$USER_NAME' 입니다. gps_server.py 의 로그 경로(/home/unimotors/gps_logs)가"
  warn "고정돼 있어 계정을 'unimotors' 로 쓰는 것을 권장합니다 (마스터 문서 1-3)."
  confirm "그래도 계속할까요?" n || exit 1
fi

if [ "$DRY" -eq 0 ]; then
  sudo -v || die "sudo 권한이 필요합니다."
  exec > >(tee -a "$LOG") 2>&1      # 화면 출력만 로그로 (비밀값 입력은 /dev/tty 라 기록 안 됨)
  echo "  로그      : $LOG"
fi

# ---------------------------------------------------------------------
#  1. APT
# ---------------------------------------------------------------------
step "APT 저장소 HTTPS 전환 + 전체 업데이트 + 패키지 설치"
for f in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do
  [ -f "$f" ] || continue
  if grep -q 'http://deb.debian.org/' "$f"; then
    backup "$f"
    run sudo sed -i 's|http://deb.debian.org/|https://deb.debian.org/|g' "$f"
    ok "$f → https"
  else
    ok "$f 이미 HTTPS (또는 해당 없음)"
  fi
done
if grep -rqs 'mirror.kakao.com' /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null; then
  warn "카카오 미러 설정이 남아 있습니다. trixie arm64 목록이 없어 실패할 수 있습니다 (37-4)."
fi

PKGS=(curl wireguard-tools python3-websocket)
if [ "$ROLE" = "car" ]; then
  PKGS+=(python3-serial python3-can can-utils minicom udhcpc)
fi
# 설치 중 설정파일 질문으로 멈추지 않게 (curl | bash 대비): 기존 설정 유지가 기본
APT_OPTS=(-y -o Acquire::Retries=3 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
run sudo apt-get update -y -o Acquire::Retries=3
ok "저장소 목록 갱신 (apt-get update)"
if [ "$DO_UPGRADE" -eq 1 ]; then
  echo "    전체 패키지 업그레이드 중 — 처음이면 수 분~수십 분 걸릴 수 있습니다"
  run sudo env DEBIAN_FRONTEND=noninteractive apt-get upgrade "${APT_OPTS[@]}"
  ok "시스템 패키지 최신화 (apt-get upgrade)"
else
  ok "업그레이드 생략 (--no-upgrade)"
fi
run sudo env DEBIAN_FRONTEND=noninteractive apt-get install "${APT_OPTS[@]}" "${PKGS[@]}"
ok "패키지: ${PKGS[*]}"

if [ "$ROLE" = "car" ] && systemctl list-unit-files 2>/dev/null | grep -q '^ModemManager'; then
  run sudo systemctl disable --now ModemManager || true
  ok "ModemManager 비활성화 (LTE 포트 점유 방지)"
fi

# ---------------------------------------------------------------------
#  2. 코드 내려받기
# ---------------------------------------------------------------------
step "코드 내려받기 → $HOME_DIR"
# v7 계기판 = gps_server.py + race_runtime.py + driver_dashboard.html + telemetry_protocol.py
#   (install-vehicle.sh 와 같은 평면 배치: 모두 홈 폴더 한 곳에 둔다)
V7_FILES=(vehicle/gps_server.py vehicle/race_runtime.py vehicle/driver_dashboard.html
          vehicle/claude_dashboard.html shared/telemetry_protocol.py)
if [ "$ROLE" = "demo" ]; then
  FILES=("${V7_FILES[@]}" vehicle/demo_sim.py)
else
  FILES=("${V7_FILES[@]}" vehicle/bms_reader.py vehicle/backfill.py deploy/vehicle/lte_auto.sh)
fi

fetch_one() {               # fetch_one 레포경로 대상경로
  local rel="$1" dst="$2" name; name="$(basename "$rel")"
  if [ -n "$SRC_DIR" ]; then
    local cand
    for cand in "$SRC_DIR/$rel" "$SRC_DIR/$name"; do
      if [ -f "$cand" ]; then run cp "$cand" "$dst"; return 0; fi
    done
    die "로컬에서 $name 을 찾지 못했습니다 ($SRC_DIR)"
  elif [ -n "$BASE_URL" ]; then
    run curl -fsSL "${BASE_URL%/}/$rel" -o "$dst" || die "다운로드 실패: ${BASE_URL%/}/$rel"
  else
    die "--base-url 또는 --src 가 필요합니다."
  fi
}

STAGING_DIR=""
if [ "$DRY" -eq 1 ]; then
  for rel in "${FILES[@]}"; do fetch_one "$rel" "$HOME_DIR/$(basename "$rel").new"; done
  echo "    + 검증 후 설치: v7 런타임 import / 화면 / 데모 계산 (dry-run에서는 실행하지 않음)"
else
  STAGING_DIR="$(mktemp -d "$HOME_DIR/.unimotors-stage.XXXXXX")"
  trap cleanup_stage EXIT
  for rel in "${FILES[@]}"; do fetch_one "$rel" "$STAGING_DIR/$(basename "$rel")"; done
  if ! validate_staged_code "$STAGING_DIR"; then
    die "코드 호환성 검증 실패 — 설치를 중단합니다."
  fi
  if [ "$ROLE" = "demo" ]; then
    if ! python3 -B "$STAGING_DIR/demo_sim.py" --dry-run 1 > /dev/null; then
      die "demo_sim.py 계산 검증 실패 — 설치를 중단합니다."
    fi
    ok "demo_sim.py 계산 검증 통과 (--dry-run 1)"
  fi
  for rel in "${FILES[@]}"; do
    name="$(basename "$rel")"; dst="$HOME_DIR/$name"
    if [ -f "$dst" ] && cmp -s "$dst" "$STAGING_DIR/$name"; then
      ok "$name (변경 없음)"; continue
    fi
    backup "$dst"
    cp "$STAGING_DIR/$name" "$dst.new"
    mv "$dst.new" "$dst"
    case "$name" in *.sh) chmod +x "$dst" ;; esac
    ok "$name"
  done
  cleanup_stage
  STAGING_DIR=""
  trap - EXIT
fi

# ---------------------------------------------------------------------
#  3. systemd 서비스
# ---------------------------------------------------------------------
step "systemd 서비스 생성 (사용자 $USER_NAME)"
install_unit() {            # install_unit 이름 (내용은 stdin)
  local f="/etc/systemd/system/$1"
  backup "$f"
  put_root "$f" 644
  ok "$f"
}

if [ "$ROLE" = "demo" ]; then
  install_unit demo-sim.service <<EOF
[Unit]
Description=UNIMOTORS Dashboard Demo Simulator
After=network-online.target wg-quick@wg0.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=-/etc/default/unimotors
ExecStart=/usr/bin/python3 $HOME_DIR/demo_sim.py
WorkingDirectory=$HOME_DIR
Restart=on-failure
RestartSec=5
User=$USER_NAME

[Install]
WantedBy=multi-user.target
EOF
  ENABLE_NOW=(demo-sim.service); ENABLE_ONLY=()
else
  install_unit lte-auto.service <<EOF
[Unit]
Description=Auto LTE Connection for SIM7600
After=network.target

[Service]
Type=oneshot
ExecStart=$HOME_DIR/lte_auto.sh
RemainAfterExit=yes
User=root

[Install]
WantedBy=multi-user.target
EOF
  install_unit gps-dashboard.service <<EOF
[Unit]
Description=UNIMOTORS GPS Dashboard Server
After=lte-auto.service
Wants=lte-auto.service

[Service]
Type=simple
EnvironmentFile=-/etc/default/unimotors
ExecStart=/usr/bin/python3 $HOME_DIR/gps_server.py
WorkingDirectory=$HOME_DIR
Restart=on-failure
RestartSec=5
User=$USER_NAME

[Install]
WantedBy=multi-user.target
EOF
  install_unit backfill.service <<EOF
[Unit]
Description=UNIMOTORS telemetry backfill (telem CSV -> server)
After=network-online.target wg-quick@wg0.service
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=-/etc/default/unimotors
ExecStart=/usr/bin/python3 $HOME_DIR/backfill.py --all
WorkingDirectory=$HOME_DIR
User=$USER_NAME
SuccessExitStatus=1
EOF
  install_unit backfill.timer <<'EOF'
[Unit]
Description=Run UNIMOTORS backfill periodically

[Timer]
OnBootSec=3min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF
  IP_BIN="$(command -v ip || echo /usr/sbin/ip)"
  install_unit can0.service <<EOF
[Unit]
Description=Bring up can0 (250kbps)
BindsTo=sys-subsystem-net-devices-can0.device
After=sys-subsystem-net-devices-can0.device

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$IP_BIN link set can0 up type can bitrate 250000
ExecStop=$IP_BIN link set can0 down

[Install]
WantedBy=sys-subsystem-net-devices-can0.device
EOF
  ENABLE_NOW=(gps-dashboard.service backfill.timer)
  ENABLE_ONLY=(lte-auto.service can0.service)      # LTE HAT·CAN 어댑터가 붙어야 의미 있음
fi

run sudo systemctl daemon-reload
for u in "${ENABLE_NOW[@]}"; do
  run sudo systemctl enable "$u"
  run sudo systemctl restart "$u" && ok "$u 실행" || warn "$u 시작 실패 → journalctl -u $u"
done
for u in "${ENABLE_ONLY[@]}"; do run sudo systemctl enable "$u"; ok "$u 등록 (부팅/장치 연결 시 시작)"; done

# ---------------------------------------------------------------------
#  4. 키오스크
# ---------------------------------------------------------------------
step "LCD 키오스크 (Wayland/labwc)"
CHROME="$(command -v chromium || command -v chromium-browser || true)"
if [ -d /etc/xdg/labwc ] || command -v labwc > /dev/null 2>&1; then
  if [ -z "$CHROME" ]; then
    warn "Chromium 이 없습니다 → sudo apt install chromium 후 다시 실행하세요."
  else
    AUTO="$HOME_DIR/.config/labwc/autostart"
    LINE="sh -c 'until curl -s -o /dev/null http://localhost:8080; do sleep 1; done; $(basename "$CHROME") --ozone-platform=wayland --kiosk --noerrdialogs --disable-infobars --password-store=basic --no-first-run http://localhost:8080' &"
    if [ -f "$AUTO" ] && grep -q 'localhost:8080' "$AUTO"; then
      ok "키오스크 autostart 이미 있음 ($AUTO)"
    else
      backup "$AUTO"
      { [ -f "$AUTO" ] && cat "$AUTO"; echo "# UNIMOTORS 계기판 키오스크 (install-pi.sh $TS)"; echo "$LINE"; } \
        | put_user "$AUTO.tmp"
      run mv "$AUTO.tmp" "$AUTO"
      ok "키오스크 autostart 등록 → 다음 로그인(재부팅)부터 자동 실행"
    fi
  fi
  if command -v raspi-config > /dev/null 2>&1; then
    run sudo raspi-config nonint do_blanking 1 && ok "화면 자동 꺼짐 해제 (재부팅 후 적용)"
  fi
else
  warn "labwc 데스크톱이 아닙니다 (Lite 이미지?). X11 키오스크는 마스터 문서 24-12 절차를 따르세요."
fi

# ---------------------------------------------------------------------
#  5. 서버 업로드 토큰
# ---------------------------------------------------------------------
step "서버 업로드 토큰 (/etc/default/unimotors)"
TOKEN="${UNIMOTORS_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  TOKEN="$(ask_secret "서버 AUTH_TOKEN (비우면 건너뜀 · 기본값을 쓰는 서버면 비워두세요)")"
fi
if [ -n "$TOKEN" ]; then
  backup /etc/default/unimotors
  if ! save_upload_token /etc/default/unimotors "$TOKEN"; then
    die "토큰 저장 실패 — 기존 설정을 유지하고 설치를 중단합니다."
  fi
  ok "토큰 갱신 (기존 설정 보존, root 전용 600). 서비스 재시작 시 적용"
  for u in "${ENABLE_NOW[@]}"; do
    if [[ "$u" == *.service ]]; then run sudo systemctl restart "$u" || true; fi
  done
else
  ok "건너뜀"
fi
unset TOKEN

# ---------------------------------------------------------------------
#  6. WireGuard (직접 입력 + OCI 등록용 공유 파일)
# ---------------------------------------------------------------------
step "WireGuard VPN"
want_wg=0
case "$DO_WG" in
  yes) want_wg=1 ;;
  ask) if [ -f /etc/wireguard/wg0.conf ]; then
         confirm "wg0.conf 가 이미 있습니다. 새로 설정할까요?" n && want_wg=1
       else
         confirm "VPN(WireGuard) 을 설정할까요?" y && want_wg=1
       fi ;;
esac
if [ "$HAS_TTY" -eq 0 ] && [ "$DO_WG" = "ask" ] && [ -z "$WG_IP" ]; then want_wg=0; fi

if [ "$want_wg" -eq 1 ]; then
  echo "  OCI 허브의 공개 정보는 팀원과 공유해도 되는 값이고, PSK 는 비밀입니다."
  WG_IP="$(ask "이 기기의 VPN IP (10.10.0.X — 사용 중: .1 허브 .2 PC .3 차량 .4 데모 .9 서버 .11 폰)" "$WG_IP")"
  [[ "$WG_IP" =~ ^10\.10\.0\.([0-9]{1,3})$ ]] && [ "${BASH_REMATCH[1]}" -ge 2 ] && [ "${BASH_REMATCH[1]}" -le 254 ] \
    || die "VPN IP 형식이 올바르지 않습니다: '$WG_IP'"
  WG_ENDPOINT="$(ask "OCI 엔드포인트 (공인IP:51820)" "$WG_ENDPOINT")"
  [[ "$WG_ENDPOINT" =~ ^[^:]+:[0-9]+$ ]] || die "엔드포인트 형식은 IP:포트 입니다"
  WG_SERVER_PUB="$(ask "OCI 서버 공개키 (OCI 에서: sudo wg show wg0 public-key)" "$WG_SERVER_PUB")"
  [[ "$WG_SERVER_PUB" =~ ^[A-Za-z0-9+/]{42,43}=$ ]] || die "공개키 형식이 올바르지 않습니다"
  PSK="${WG_PSK:-}"
  [ -z "$PSK" ] && PSK="$(ask_secret "공유 PSK (OCI 의 shared.psk 내용)")"
  [[ "$PSK" =~ ^[A-Za-z0-9+/]{42,43}=$ ]] || die "PSK 형식이 올바르지 않습니다"

  # 개인키: 이 기기에서 생성, root 전용. 이미 있으면 재사용 (OCI 재등록 불필요)
  KEY=/etc/wireguard/device_private.key
  run sudo install -d -m 700 /etc/wireguard
  if [ "$DRY" -eq 1 ]; then
    echo "    + wg genkey > $KEY (없을 때만)"; PUB="<dry-run-public-key>"; PRIV="<dry-run>"
  else
    if ! sudo test -s "$KEY"; then
      sudo sh -c "umask 077; wg genkey > $KEY"; ok "개인키 생성 ($KEY, 이 기기 밖으로 나가지 않음)"
    else
      ok "기존 개인키 재사용"
    fi
    PRIV="$(sudo cat "$KEY")"; PUB="$(printf '%s' "$PRIV" | wg pubkey)"
  fi

  backup /etc/wireguard/wg0.conf
  put_root /etc/wireguard/wg0.conf 600 <<EOF
# UNIMOTORS $HOST_NAME ($ROLE) — install-pi.sh $TS
[Interface]
PrivateKey = $PRIV
Address = $WG_IP/24

[Peer]
PublicKey = $WG_SERVER_PUB
PresharedKey = $PSK
Endpoint = $WG_ENDPOINT
AllowedIPs = 10.10.0.0/24
PersistentKeepalive = 25
EOF
  unset PRIV PSK
  ok "/etc/wireguard/wg0.conf (600)"

  # ---- 공유용: OCI 에 붙여넣을 [Peer] 블록 (비밀값 없음) ----
  SHARE="$HOME_DIR/wg-peer-$HOST_NAME.txt"
  put_user "$SHARE" <<EOF
# ===== OCI 허브에 등록할 피어 — $HOST_NAME ($ROLE), $TS =====
# 1) OCI 에서:  sudo nano /etc/wireguard/wg0.conf   맨 아래에 붙여넣기
[Peer]                       # $HOST_NAME ($ROLE)
PublicKey = $PUB
PresharedKey = <shared.psk 내용>
AllowedIPs = $WG_IP/32

# 2) 재시작 없이 적용 (다른 피어 연결 유지):
#    sudo bash -c 'wg syncconf wg0 <(wg-quick strip wg0)'
# 3) 확인:  sudo wg show | grep -A3 '$PUB'
# (이 파일에는 비밀값이 없습니다. 팀 채팅/문서에 공유해도 됩니다.)
EOF
  echo
  echo "  ${B}── OCI 에 등록할 내용 (공유용 파일: $SHARE) ──${N}"
  if [ "$DRY" -eq 0 ]; then sed 's/^/  │ /' "$SHARE"; fi
  echo "  ${B}──────────────────────────────────────────────${N}"

  run sudo systemctl enable wg-quick@wg0
  go="$(ask "OCI 등록을 마쳤으면 Enter, 나중에 하려면 s" "")"
  if [ "$go" != "s" ] && [ "$go" != "S" ] && [ "$HAS_TTY" -eq 1 ]; then
    if [ "$DRY" -eq 0 ]; then
      sudo systemctl restart wg-quick@wg0 || warn "wg-quick 시작 실패 → journalctl -u wg-quick@wg0"
      okp=0
      for _ in $(seq 1 10); do
        if ping -c1 -W2 "$WG_HUB_IP" > /dev/null 2>&1; then okp=1; break; fi
        sleep 2
      done
      if [ "$okp" -eq 1 ]; then
        ok "VPN 연결됨 — 허브 $WG_HUB_IP 응답"
        ping -c1 -W2 "$SERVER_IP" > /dev/null 2>&1 && ok "서버 $SERVER_IP 응답" || warn "서버 $SERVER_IP 응답 없음 (서버 상태 확인)"
        sudo wg show wg0 | grep -E 'handshake|transfer' | sed 's/^/    /'
      else
        warn "허브 응답 없음. OCI 등록·syncconf 적용 여부와 UDP 51820 차단 여부를 확인하세요."
      fi
    fi
  else
    ok "VPN 은 다음 부팅 때 자동으로 올라옵니다 (OCI 등록 후)"
  fi
else
  ok "건너뜀"
fi

# ---------------------------------------------------------------------
#  7. 교내 와이파이 (프로필만 추가)
# ---------------------------------------------------------------------
step "교내 와이파이 프로필 (WPA2-Enterprise)"
want_wifi=0
case "$DO_WIFI" in
  yes) want_wifi=1 ;;
  ask) confirm "교내 와이파이 프로필을 추가할까요?" n && want_wifi=1 ;;
esac
if [ "$want_wifi" -eq 1 ] && [ "$HAS_TTY" -eq 1 ]; then
  SSID="$(ask "SSID" "WUNIST_AAA")"
  WID="$(ask "아이디" "")"
  WPW="$(ask_secret "비밀번호")"
  [ -n "$WID" ] && [ -n "$WPW" ] || die "아이디/비밀번호가 비었습니다"
  if nmcli -t -f NAME connection show 2>/dev/null | grep -qx campus; then
    run sudo nmcli connection delete campus
  fi
  if [ "$DRY" -eq 1 ]; then
    echo "    + sudo nmcli connection add … ssid $SSID 802-1x.identity $WID 802-1x.password *** …"
  else
    sudo nmcli connection add type wifi ifname wlan0 con-name campus ssid "$SSID" \
      wifi-sec.key-mgmt wpa-eap 802-1x.eap peap 802-1x.phase2-auth mschapv2 \
      802-1x.identity "$WID" 802-1x.password "$WPW" 802-1x.password-flags 0 \
      connection.autoconnect yes connection.autoconnect-priority 10 > /dev/null
  fi
  unset WPW
  CUR="$(nmcli -t -f NAME,TYPE connection show --active 2>/dev/null | awk -F: '$2 ~ /wireless/ {print $1; exit}')"
  ok "프로필 'campus' 추가 (자동연결 우선순위 10). 지금 연결은 바꾸지 않았습니다."
  echo "    지금 바로 전환하려면 (SSH 가 끊기므로 백그라운드 + 실패 시 복귀):"
  echo "    sudo nohup sh -c 'nmcli connection up campus || nmcli connection up \"${CUR:-<현재 연결 이름>}\"' > /tmp/wifi.log 2>&1 &"
else
  ok "건너뜀"
fi

# ---------------------------------------------------------------------
#  요약
# ---------------------------------------------------------------------
step "완료"
echo "  역할 $ROLE · 계정 $USER_NAME · 호스트 $HOST_NAME"
[ "$want_wg" -eq 1 ] && echo "  VPN  $WG_IP  (OCI 등록용: $HOME_DIR/wg-peer-$HOST_NAME.txt)"
for u in "${ENABLE_NOW[@]}" "${ENABLE_ONLY[@]}"; do
  [ "$DRY" -eq 1 ] && continue
  printf "  %-22s %s\n" "$u" "$(systemctl is-enabled "$u" 2>/dev/null)/$(systemctl is-active "$u" 2>/dev/null)"
done
[ "$DRY" -eq 0 ] && echo "  로그 $LOG"
echo
if [ -f /var/run/reboot-required ]; then
  echo "  ${Y}! 업그레이드로 커널/펌웨어가 바뀌었습니다 — 재부팅이 필요합니다${N}"
fi
echo "  다음: ${B}sudo reboot${N} 후 DSI 화면에 계기판이 뜨는지, 서버 대시보드에 차량이 보이는지 확인하세요."
echo "        서버 확인: curl -s http://$SERVER_IP:8090/api/status"
