# LTE 통신 (KT / SIM7600G-H)

> HAT 결합 · AT+NDIS Raw IP · lte_auto.sh · systemd

---

## 4. 2단계: LTE 통신 (KT / SIM7600G-H)

> 최종 채택: **AT 명령어(NDIS) + Raw IP + udhcpc**.
> (QMI/qmicli는 이 모듈 펌웨어에서 실패.)

### 4-1. HAT 결합 절차
```bash
sudo poweroff              # 핫스왑 금지
```
1. LED 꺼진 뒤 전원 분리 (전원 인가 상태 결합은 GPIO 합선 위험)
2. **USIM 삽입** (전원 off 필수)
3. 포고핀-GPIO 맞춰 결합
4. 전원 재인가

### 4-2. 인식 확인
```bash
lsusb                      # 1e0e:9001 SimTech (SIM7600 계열)
ls -l /dev/ttyUSB*         # ttyUSB0~4 (5개)
```
> ttyUSB1 = GPS NMEA 출력 / ttyUSB2 = AT 커맨드

### 4-3. 필수 패키지 + ModemManager 비활성화
```bash
sudo apt update
sudo apt install -y minicom udhcpc
sudo systemctl stop ModemManager
sudo systemctl disable ModemManager
```
> `udhcpc` 필수 (구형 dhclient는 Raw IP 못 다룸)

### 4-4. 자동화 스크립트 `/home/unimotors/lte_auto.sh`
> ⭐ LTE 연결 + **GPS 활성화까지 한 스크립트에서 처리**한다.
> GPS 설정은 반드시 `AT+CGPS=0`(끄기) → 설정 → `AT+CGPS=1,1`(켜기) 순서여야 반영된다.
```bash
#!/bin/bash
# 1. 차량 전원 인가 시 모뎀 포트가 올라올 때까지 대기
while [ ! -e /dev/ttyUSB2 ]; do sleep 1; done
sleep 3

# 2. 시리얼 포트 통신 설정
stty -F /dev/ttyUSB2 115200 raw -echo

# 3. KT APN 설정 및 NDIS 데이터 콜
echo -e "AT+CGDCONT=1,\"IP\",\"lte.ktfwing.com\"\r\n" > /dev/ttyUSB2
sleep 1
echo -e "AT\$QCRMCALL=1,1\r\n" > /dev/ttyUSB2
sleep 5

# 4. 인터페이스 초기화 및 Raw IP 모드 적용
ip link set wwan0 down
echo 'Y' > /sys/class/net/wwan0/qmi/raw_ip
ip link set wwan0 up
sleep 2

# 5. 백그라운드에서 IP 할당 요청
udhcpc -b -i wwan0

# 6. GPS 활성화 + 10Hz + NMEA 마스크
#    ⭐ 설정 변경은 GPS를 껐다 켜야 반영됨. 순서 필수.
echo -e "AT+CGPS=0\r\n" > /dev/ttyUSB2
sleep 2
echo -e "AT+CGPSNMEARATE=1\r\n" > /dev/ttyUSB2   # 1 = 10Hz (0 = 1Hz)
sleep 1
echo -e "AT+CGNSSMODE=15,1\r\n" > /dev/ttyUSB2    # GPS+GLONASS+BeiDou+Galileo (위성 수 ↑)
sleep 1
echo -e "AT+CGPSNMEA=17\r\n" > /dev/ttyUSB2       # 이 개체선 GGA+VTG만. GSV 제외 → 버퍼 적체 방지(필수!)
sleep 1
echo -e "AT+CGPS=1,1\r\n" > /dev/ttyUSB2          # stand-alone 재시작
sleep 2
```
```bash
sudo chmod +x /home/unimotors/lte_auto.sh
```
> - 서비스 재시작/부팅 직후엔 GPS가 껐다 켜지며 **웜스타트 재측위**를 하므로 수 초~수 분간
>   NMEA에 빈 값이 나올 수 있음(정상).
> - 로그의 `stty: unable to perform all requested operations` 경고는 무시 가능.
> - `CGNSSMODE=15`로 위성계를 늘려도 `CGPSNMEA=17`로 GSV(위성목록)를 막았으므로
>   Zero W에서도 버퍼 적체가 없다. (실측: `in_waiting` 0~34B 유지)

### 4-5. systemd 서비스 `/etc/systemd/system/lte-auto.service`
```ini
[Unit]
Description=Auto LTE Connection for SIM7600
After=network.target

[Service]
Type=oneshot
ExecStart=/home/unimotors/lte_auto.sh
RemainAfterExit=yes
User=root

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable lte-auto.service
```

### 4-6. 검증 (WiFi 끄지 말 것!)
```bash
ping -I wwan0 -c 4 8.8.8.8
curl --interface wwan0 ifconfig.me    # KT 대역 공인 IP 확인
```

---

← [라즈베리파이 초기 세팅 (헤드리스)](02-pi-setup.md) · [문서 목차](README.md) · [GPS 10Hz 계기판](04-gps.md) →
