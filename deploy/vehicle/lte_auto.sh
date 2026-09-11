#!/bin/bash
# UNIMOTORS — LTE(KT/SIM7600G-H) 연결 + GPS 10Hz 활성화
# /home/unimotors/lte_auto.sh  (chmod +x)
#
# ⭐ GPS 설정은 반드시  AT+CGPS=0 → 설정 → AT+CGPS=1,1  순서여야 반영된다.

APN="${UNIMOTORS_APN:-lte.ktfwing.com}"
AT_PORT="${UNIMOTORS_AT_PORT:-/dev/ttyUSB2}"

# 1. 차량 전원 인가 시 모뎀 포트가 올라올 때까지 대기
while [ ! -e "$AT_PORT" ]; do sleep 1; done
sleep 3

# 2. 시리얼 포트 통신 설정
stty -F "$AT_PORT" 115200 raw -echo

# 3. APN 설정 및 NDIS 데이터 콜
echo -e "AT+CGDCONT=1,\"IP\",\"$APN\"\r\n" > "$AT_PORT"
sleep 1
echo -e "AT\$QCRMCALL=1,1\r\n" > "$AT_PORT"
sleep 5

# 4. 인터페이스 초기화 및 Raw IP 모드 적용
ip link set wwan0 down
echo 'Y' > /sys/class/net/wwan0/qmi/raw_ip
ip link set wwan0 up
sleep 2

# 5. 백그라운드에서 IP 할당 요청 (dhclient 는 Raw IP 를 못 다룸 → udhcpc)
udhcpc -b -i wwan0

# 6. GPS 활성화 + 10Hz + NMEA 마스크
echo -e "AT+CGPS=0\r\n"          > "$AT_PORT"; sleep 2
echo -e "AT+CGPSNMEARATE=1\r\n"  > "$AT_PORT"; sleep 1   # 1 = 10Hz (0 = 1Hz)
echo -e "AT+CGNSSMODE=15,1\r\n"  > "$AT_PORT"; sleep 1   # GPS+GLONASS+BeiDou+Galileo
echo -e "AT+CGPSNMEA=17\r\n"     > "$AT_PORT"; sleep 1   # GGA+VTG 만. GSV 제외(버퍼 적체 방지, 필수)
echo -e "AT+CGPS=1,1\r\n"        > "$AT_PORT"; sleep 2   # stand-alone 재시작
