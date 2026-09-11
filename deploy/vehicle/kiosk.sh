#!/bin/bash
# UNIMOTORS — DSI LCD 키오스크. 계기판을 Pi 내장 화면에 풀스크린으로 띄운다.
# /home/unimotors/kiosk.sh  (chmod +x)

# 화면 절전/블랭킹 해제 (주행 중 화면이 꺼지면 안 됨)
xset s off
xset s noblank
xset -dpms
unclutter -idle 0.1 -root &        # 마우스 커서 숨김
openbox-session &                   # 최소 윈도우 매니저

# 계기판이 뜰 때까지 대기 (gps_server 부팅 순서 방어)
until curl -s -o /dev/null http://127.0.0.1:8080/; do sleep 1; done

chromium-browser \
  --kiosk http://127.0.0.1:8080/ \
  --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
  --disable-features=Translate --no-first-run \
  --check-for-update-interval=31536000 \
  --disable-pinch --overscroll-history-navigation=0 \
  --autoplay-policy=no-user-gesture-required
