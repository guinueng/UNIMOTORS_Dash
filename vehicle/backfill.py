#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill.py — 주행 후 로컬 telem CSV를 서버에 업로드 (UNIMOTORS).

용도 2가지:
  1) 실시간 스트림의 구멍 보정 — LTE 음영(터널 등)으로 서버가 놓친 구간을,
     차량 로컬에 완전하게 남은 telem CSV 로 사후 메꿈.
     서버 summary.ts 가 PRIMARY KEY(INSERT OR IGNORE)라 중복은 무시, 구멍만 채움.
  2) 10Hz 정밀 궤적 업로드 — 주행을 'session' 으로 track 테이블에 벌크 저장.
     대시보드 주행분석 탭에서 정밀 궤적/랩/G 분석에 사용.

동작:
  - 지정한 telem_*.csv 를 읽어 서버 POST /upload?session=&vehicle= 로 전송.
  - session 미지정 시 파일명에서 자동 생성 (telem_20260721_140134.csv -> 20260721_140134).
  - vehicle 미지정 시 CSV 안의 vehicle 컬럼 첫 값 사용.

사용 예:
  python3 backfill.py ~/gps_logs/telem_20260721_140134.csv
  python3 backfill.py telem_x.csv --session race1 --vehicle car1
  python3 backfill.py --all           # gps_logs 안의 모든 telem_*.csv 업로드
  python3 backfill.py --all --server http://10.10.0.9:8090

주의:
  - 서버(telemetry_server.py)가 떠 있고 VPN(10.10.0.9)이 연결돼 있어야 함.
  - 토큰(TOKEN)은 서버 AUTH_TOKEN 과 일치해야 함.
  - 이 스크립트는 표준 라이브러리만 사용 (추가 설치 불필요).
"""
import argparse
import glob
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse

# 서버/인증 기본값 (gps_server.py 와 동일하게)
DEFAULT_SERVER = "http://10.10.0.9:8090"   # 기숙사 Pi4 (VPN IP). WS와 같은 호스트, http.
DEFAULT_TOKEN  = os.environ.get("UNIMOTORS_TOKEN", "change-me")
LOG_DIR        = os.path.expanduser("~/gps_logs")


def session_from_filename(path):
    """telem_20260721_140134.csv -> 20260721_140134"""
    base = os.path.basename(path)
    name = base.rsplit(".", 1)[0]
    if name.startswith("telem_"):
        name = name[len("telem_"):]
    elif name.startswith("gps_"):
        name = name[len("gps_"):]
    return name or base


def vehicle_from_csv(text):
    """CSV 첫 데이터행의 vehicle 컬럼 값을 추출 (없으면 None)."""
    lines = text.strip().splitlines()
    if len(lines) < 2:
        return None
    header = lines[0].split(",")
    if "vehicle" not in header:
        return None
    idx = header.index("vehicle")
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) > idx and parts[idx]:
            return parts[idx]
    return None


def upload_one(path, server, token, session=None, vehicle=None):
    if not os.path.exists(path):
        print(f"  [건너뜀] 파일 없음: {path}")
        return False
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    lines = text.strip().splitlines()
    if len(lines) < 2:
        print(f"  [건너뜀] 데이터 없음: {os.path.basename(path)}")
        return False

    header = lines[0].split(",")
    veh = vehicle or vehicle_from_csv(text) or "unknown"

    # CSV 안의 session 컬럼으로 그룹화.
    #  차량에서 시계 점프(RTC 없는 Pi)가 나면 한 파일에 세션이 2개 이상 섞이므로,
    #  세션별로 나눠서 각각 업로드해야 서버 통계가 오염되지 않는다.
    groups = {}
    if session:                       # 사용자가 명시하면 전체를 그 세션으로
        groups[session] = lines[1:]
    elif "session" in header:
        idx = header.index("session")
        for ln in lines[1:]:
            parts = ln.split(",")
            key = parts[idx] if len(parts) > idx and parts[idx] else session_from_filename(path)
            groups.setdefault(key, []).append(ln)
    else:                             # 구버전 CSV: 파일명에서
        groups[session_from_filename(path)] = lines[1:]

    ok = True
    for sess, body_lines in sorted(groups.items()):
        payload = "\n".join([lines[0]] + body_lines)
        url = (f"{server}/upload?session={urllib.parse.quote(sess)}"
               f"&vehicle={urllib.parse.quote(veh)}")
        req = urllib.request.Request(
            url, data=payload.encode("utf-8"), method="POST",
            headers={"Content-Type": "text/csv; charset=utf-8",
                     "X-Auth-Token": token})
        tag = os.path.basename(path) + (f" [{sess}]" if len(groups) > 1 else "")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode())
            print(f"  [완료] {tag} -> {veh}/{sess}: "
                  f"수신 {result.get('received')}행, 신규 {result.get('inserted')}행")
        except urllib.error.HTTPError as e:
            print(f"  [실패] {tag}: HTTP {e.code} {e.reason}"); ok = False
        except urllib.error.URLError as e:
            print(f"  [실패] 서버 연결 불가 ({server}): {e.reason}")
            print(f"         VPN(10.10.0.9) 연결과 서버 실행 상태를 확인하세요.")
            return False
        except Exception as e:
            print(f"  [실패] {tag}: {e}"); ok = False
    if len(groups) > 1:
        print(f"  (시계 점프로 세션 {len(groups)}개로 분리됨)")
    return ok


def main():
    ap = argparse.ArgumentParser(description="UNIMOTORS 텔레메트리 CSV 서버 업로드")
    ap.add_argument("csv", nargs="?", help="업로드할 telem_*.csv 경로")
    ap.add_argument("--all", action="store_true",
                    help=f"{LOG_DIR} 안의 모든 telem_*.csv 업로드")
    ap.add_argument("--session", help="세션 이름 (미지정 시 파일명에서 생성)")
    ap.add_argument("--vehicle", help="차량 ID (미지정 시 CSV에서 추출)")
    ap.add_argument("--server", default=DEFAULT_SERVER, help=f"서버 주소 (기본 {DEFAULT_SERVER})")
    ap.add_argument("--token", default=DEFAULT_TOKEN, help="인증 토큰")
    args = ap.parse_args()

    if args.all:
        files = sorted(glob.glob(os.path.join(LOG_DIR, "telem_*.csv")))
        if not files:
            print(f"업로드할 telem_*.csv 가 없습니다: {LOG_DIR}")
            sys.exit(1)
        print(f"{len(files)}개 파일 업로드 시작 -> {args.server}")
        ok = 0
        for p in files:
            if upload_one(p, args.server, args.token, args.session, args.vehicle):
                ok += 1
        print(f"\n완료: {ok}/{len(files)} 성공")
    elif args.csv:
        print(f"업로드 -> {args.server}")
        upload_one(args.csv, args.server, args.token, args.session, args.vehicle)
    else:
        ap.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
