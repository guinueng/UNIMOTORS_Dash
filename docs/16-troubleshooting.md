# 트러블슈팅 종합

> 실제로 겪은 모든 문제 — 증상 → 원인 → 해결

---

## 빠른 참조 — 증상별 요약표

| 증상 | 원인 / 해결 |
|---|---|
| 헤드리스 WiFi 안 잡힘 | 국가코드 KR 확인, 2.4GHz, WPA2 |
| cmdline.txt 수정 후 부팅 멈춤 | 한 줄이어야 함 (줄바꿈 금지) |
| dhclient Unsupported device 65534 | Raw IP를 못 다룸 → udhcpc 사용 |
| qmicli CID/timeout | ModemManager 점유 → stop/disable, 안 되면 AT+NDIS 방식 |
| GPS 계기판 30초 지연 | 버퍼 적체(in_waiting=4095) → NMEA 문장 최소화(CGPSNMEA=17) + fsync 제거 |
| GPS 설정 변경 ERROR | GPS 켜진 상태에선 변경 불가 → AT+CGPS=0 후 설정 |
| candump 조용함 | 앱 통신방식이 RS485 → CAN으로 변경, BMS 절전 시 앱으로 깨우기 |
| CAN `Network is down` | can0 는 있는데 DOWN. `sudo ip link set can0 up type can bitrate 250000`. 매번 반복되면 자동화 미적용 → 8-3 (udev 또는 can0.service) |
| VPN 핸드셰이크 안 됨 | OCI Security List UDP 51820 개방, iptables INPUT 확인 |
| PC↔Pi Destination Host Prohibited | 서버 FORWARD 미개방 → iptables FORWARD wg0 wg0 ACCEPT |
| Pi 원격 몇 분 뒤 끊김 | conf에 PersistentKeepalive=25 누락 |
| 재부팅 후 방화벽 사라짐 | netfilter-persistent save 안 함 |
| 폰이 핫스팟 자동 해제 / "인터넷 없음" | **캡티브 체크가 포트 80 + DNS 때문에 코드에 도달 못 함.** 6-2절: DNS 하이재킹 + 80→8080 리다이렉트, 또는 LTE NAT 공유 |
| SSH 키 분실 | OCI Cloud Shell / 직렬 콘솔 / 부팅볼륨 분리로 복구. **개인키 백업 필수** |

---


## 27. 초기 세팅 / OS

| 증상 | 원인 / 해결 |
|---|---|
| 헤드리스 WiFi 안 잡힘 | **국가코드 KR 미설정**이 최다 원인. 2.4GHz, WPA2 확인 |
| Zero W가 5GHz WiFi 못 잡음 | Zero W는 2.4GHz만 지원 (하드웨어 한계) |
| `cmdline.txt` 수정 후 부팅 멈춤 | 한 줄이어야 함 (줄바꿈 금지). config.txt/cmdline.txt 수동 수정 지양 |
| trixie(Debian 13)에서 NetworkManager 충돌 | Zero W는 bookworm 권장. Pi4 서버(유선)는 trixie도 무방 |
| `sudo: unable to resolve host <name>` | hostname 변경 후 `/etc/hosts`의 127.0.1.1 미갱신 |

### 27-1. ⭐ Zero W 헤드리스 WiFi가 안 잡힐 때 (실제 해결 이력)

초기 세팅에서 가장 많은 시간을 잡아먹은 문제. **원인이 여러 겹**이었다.

#### ① 실제 근본 원인 — `cmdline.txt`의 국가 코드가 GB
Imager가 생성한 `cmdline.txt` 끝에 이런 항목이 있었다:
```
... rootwait ds=nocloud;i=rpi-imager-... cfg80211.ieee80211_regdom=GB
```
**국가 코드가 영국(GB)으로 강제 지정**되어 있었다. 한국 핫스팟은 KR 주파수/채널 규격을
쓰는데 Pi는 GB 규격으로 스캔하니 **핫스팟을 아예 찾지 못한다.**
```
cfg80211.ieee80211_regdom=KR      ← KR 로 수정 (이것만으로 해결될 수 있음)
```
> Pi OS는 **국가 코드가 세팅되기 전까지 `rfkill`로 WiFi 칩을 강제 비활성화**한다.
> Imager에서 "무선 LAN 국가 = KR"이 왜 그토록 중요한지가 여기에 있다.

#### ② 공유기/핫스팟 쪽 원인
- **2.4GHz 전용**: Zero W는 5GHz를 물리적으로 지원하지 않는다.
  공유기의 **스마트 커넥트(2.4/5GHz 대역 병합)**가 켜져 있으면 연결이 거부될 확률이 높다.
  → **SSID를 분리**하거나 2.4GHz 전용 핫스팟으로 테스트
- **WPA3 미지원**: Zero W는 WPA3에서 접속 이슈가 잦다.
  → 공유기 보안을 **WPA2 Personal (AES)** 로 하향

#### ③ 첫 부팅 대기
파티션 리사이징 + 네트워크 초기화가 돌아 **최대 5~10분** 걸린다.
초록색 ACT LED가 불규칙하게 깜빡이다 안정될 때까지 전원을 끄지 않는다.

#### ④ Bookworm 이후 변경점
`bootfs`에 `wpa_supplicant.conf`를 수동으로 넣는 옛 방식은 **원칙적으로 동작하지 않는다**
(NetworkManager로 전면 교체됨). Imager의 OS Customization으로 설정해야 한다.
> 단, **최신 OS도 첫 부팅 시 `wpa_supplicant.conf`를 읽어 자기 설정으로 마이그레이션**해주므로
> 우회로로는 여전히 쓸 수 있다:
> ```
> ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
> update_config=1
> country=KR
> network={
>     ssid="핫스팟이름"
>     psk="비밀번호"
>     key_mgmt=WPA-PSK
> }
> ```

#### ⑤ 최후 수단 — USB OTG (Ethernet Gadget)로 진입
WiFi를 아예 우회해 **USB 케이블 하나로 PC와 직접 연결**해 SSH로 들어간다.

**SD 카드 설정** (PC에서 bootfs, 또는 `firmware/` 폴더 안):
1. `config.txt` 맨 아래 `[all]` 섹션에 추가:
   ```ini
   [all]
   dtoverlay=dwc2
   ```
2. `cmdline.txt`의 `rootwait` 바로 뒤에 한 칸 띄우고 추가 (**절대 줄바꿈 금지, 한 줄**):
   ```
   ... rootwait modules-load=dwc2,g_ether ds=nocloud;... cfg80211.ieee80211_regdom=KR
   ```
3. (선택) SD 최상단에 확장자 없는 빈 파일 `ssh` 생성

**연결**
- Zero의 마이크로 USB 포트 **2개 중 안쪽(USB 표기) 데이터 포트**에 연결
  (바깥쪽 `PWR IN`은 전원 전용 → 통신 불가)
- **데이터 전송 가능한 케이블** 사용 (충전 전용 케이블 불가)
- 2~3분 대기 후 `ssh 유저명@raspberrypi.local`
- 진입 성공하면 `sudo nmtui`로 GUI 메뉴에서 WiFi 직접 연결

**⚠️ 가장 중요 — LTE HAT을 반드시 분리할 것**
Zero의 **USB 데이터 라인은 물리적으로 단 하나**다. LTE HAT은 보드 밑면의
USB 테스트 패드(D+/D-)에 포고핀을 접촉시켜 **그 유일한 USB 라인을 점유**한다. 결과:
- **Host vs Device 충돌**: 보드가 HAT의 호스트 역할과 PC의 장치(OTG) 역할 사이에서
  충돌해 USB 통신 자체가 먹통이 된다.
  (증상: 장치 관리자에 **Pi가 아니라 SIMCom 등 모뎀 제조사**가 뜬다 = PC↔모뎀 직결 상태)
- **전력 부족**: PC USB는 500~900mA인데 LTE 모듈은 순간 1~2A를 끌어간다
  → 부팅 중 멈춤 또는 USB 연결/해제 반복

→ **헤드리스 초기 세팅이 끝날 때까지 HAT을 완전히 분리**하고, 보드 단독으로 진행한다.
WiFi/SSH가 무선으로 붙는 것을 확인한 뒤 전원을 끄고 재조립한다.

#### ⑥ Windows에서 RNDIS 드라이버 수동 설치
정상이면 [네트워크 어댑터]에 `USB Ethernet/RNDIS Gadget` 또는
`Remote NDIS based Internet Sharing Device`로 뜬다.
Windows 10/11은 자주 이를 **`USB Serial Device (COM)`** 로 오인하거나 노란 느낌표를 띄운다.
1. 해당 장치 우클릭 → **드라이버 업데이트**
2. **내 컴퓨터에서 드라이버 찾아보기**
3. **컴퓨터의 사용 가능한 드라이버 목록에서 직접 선택**
4. 장치 유형 → **네트워크 어댑터**
5. 제조업체 → **Microsoft**
6. 모델 → **Remote NDIS Compatible Device**
7. 경고 무시하고 설치

#### ⑦ 부팅 자체가 안 될 때 — ACT LED 진단
| LED 상태 | 의미 |
|---|---|
| 불규칙하게 깜빡임 | **정상** (부팅 진행 중, 2~3분 대기) |
| 아예 안 들어옴 | SD 카드 미인식 또는 전원 불량 |
| 켜진 채로 멈춤 | **Kernel Panic** — `cmdline.txt`에 숨은 줄바꿈이 1순위 원인 |

> `cmdline.txt`는 **무조건 한 줄**이어야 한다. 메모장 복붙 시 보이지 않는 엔터가 들어가면
> 그 지점에서 부팅이 멈춘다. 끝에서 백스페이스로 한 줄임을 확인할 것.

#### ⑧ 변수가 너무 많을 때 (권장 리셋 경로)
trixie(테스팅) + 수동 파일 수정이 겹치면 원인 격리가 어렵다. 가장 빠른 길:
1. Imager로 **안정 버전**(Bookworm, 또는 호환성 최우선이면 Bullseye 32-bit) 굽기
2. Imager 설정에서 SSID/비번/**무선 LAN 국가 KR** 확실히 지정
3. `config.txt`/`cmdline.txt` **수동 수정 없이** 그대로 부팅
4. 3~5분 후 공유기 관리 페이지에서 Pi의 IP 확인 → SSH
5. 통신이 뚫린 뒤 필요하면 내부에서 배포판 업그레이드

> trixie의 USB OTG는 커널에선 지원하지만, NetworkManager가 가상 이더넷(`usb0`)에
> IP를 자동 할당하지 않아 `raspberrypi.local` 접속이 실패하는 사례가 잦다.
> 또한 trixie에서 `config.txt`/`cmdline.txt`가 SD 루트가 아닌 **`firmware/` 폴더 안**으로
> 이동했을 수 있으니 그쪽을 수정해야 한다.

## 28. 전원

| 증상 | 원인 / 해결 |
|---|---|
| Pi 4가 PD 보조배터리/충전기로 부팅 안 됨 | **e-marker 내장 케이블(60W 등)이 rev1.1에서 문제.** 평범한 USB-C 케이블 사용 |
| 언더볼티지(번개 아이콘), 성능 저하 | `vcgencmd get_throttled` → `0x0`이면 정상. 5V/**3A** 이상 어댑터 필요 |
| Pi 4는 PD 협상을 하나 | 정식 PD 협상 안 함. PD 충전기의 5V 출력을 그냥 사용 |

## 29. LTE

| 증상 | 원인 / 해결 |
|---|---|
| `dhclient: Unsupported device type 65534` | dhclient가 Raw IP를 못 다룸 → **udhcpc 사용** |
| `qmicli` CID/timeout 에러 | ModemManager 점유 → stop/disable. 안 되면 **AT+NDIS 방식**으로 전환 |
| ttyUSB* 안 보임 | HAT 결합 불량, USIM 미삽입(전원 off 상태에서 삽입), 전원 부족 |
| LTE 연결됐는데 인터넷 안 됨 | `udhcpc -b -i wwan0` 실행 확인, `curl --interface wwan0 ifconfig.me` |
| 이관 후 GPS 포트 번호가 바뀜 | `ls /dev/ttyUSB*` 재확인. 필요 시 udev 규칙으로 고정 |

## 30. GPS

| 증상 | 원인 / 해결 |
|---|---|
| **계기판이 최대 30초 지연, 감속 반대 표시** | **커널 버퍼 적체.** `in_waiting`이 4095B 고정. → `CGPSNMEA=17`로 GSV 제거 + `os.fsync()` 제거 + 스레드 분리 |
| GPS 설정 명령이 ERROR | GPS 켜진 상태에선 변경 불가 → `AT+CGPS=0` 후 설정, `AT+CGPS=1,1`로 재시작 |
| `AT+CGPSNMEARATE=10` → ERROR | 값은 0(1Hz)/1(10Hz). '10'이 아님 |
| `AT+CGNSSMODE` → ERROR | 조회는 `AT+CGNSSMODE?`, 설정은 `AT+CGNSSMODE=15,1` |
| 위성이 4~6개뿐 | `CGNSSMODE=3`(GPS+GLONASS만) → **15로 변경**. 안테나를 하늘 보이는 곳에 |
| 정지 중인데 좌표가 미세하게 흔들림 | GPS 특성(오차 2~5m). 마지막 자리 흔들림은 정상. 좌표 필터로 완화 가능 |
| 급감속 시 속도 반영 2~3초 지연 | GPS 도플러 특성(물리적 한계). IMU 융합 외 해결 불가 |
| VTG 속도 필드가 비어 있음 | 정지 시 정상. **'VTG 비었음 = 측위 실패'로 오판 금지.** fix 판정은 GGA로 |
| `cat /dev/ttyUSB1`이 빈 출력 | `gps_server.py`가 포트 점유 중 (정상). 서비스 중지 후 확인 |
| 궤적이 실제 도로를 안 따라감 | 위성 부족 + 필터 없음. 23-1 참고 |

## 31. CAN / BMS

| 증상 | 원인 / 해결 |
|---|---|
| `candump can0` → `Network is down` | `sudo ip link set can0 up type can bitrate 250000` (또는 80-can0.network로 자동화) |
| `[BMS] 오류: [Errno 19] No such device` | can0 인터페이스 없음. USB-CAN 미연결이면 **정상** (BMS 없어도 나머지는 동작) |
| `candump`이 조용함 | ① BMS 앱에서 통신방식이 RS485 → **CAN으로 변경** ② BMS 절전 → 앱으로 깨우기 ③ 속도 250kbps 확인 |
| CAN H/L 외 A/B 단자 연결 | **RS485 단자이므로 절대 연결 금지** |
| 전류값이 30000 근처의 큰 수 | **30000 오프셋** 적용 필요: `(raw - 30000) * 0.1` A |
| 온도가 40 이상 큰 값 | **40 오프셋**: `raw - 40` °C |
| 배터리 데이터가 대시보드에 안 나옴 | ① BMS 미연결 → 정상(`--` 표시) ② `track` 테이블 구버전(배터리 컬럼 없음) → 서버 갱신 |

## 32. VPN (WireGuard)

| 증상 | 원인 / 해결 |
|---|---|
| 핸드셰이크 안 됨 | **OCI Security List UDP 51820 미개방** (TCP 아님!), 또는 iptables INPUT |
| PC↔Pi `Destination Host Prohibited` | 서버 FORWARD 미개방 → `iptables -I FORWARD 1 -i wg0 -o wg0 -j ACCEPT` |
| Pi 원격 접속이 몇 분 뒤 끊김 | Pi conf에 `PersistentKeepalive = 25` 누락 (CGNAT NAT 매핑 만료) |
| 재부팅 후 방화벽 규칙 사라짐 | `sudo netfilter-persistent save` 안 함 |
| **폰 풀터널에서 VPN은 붙는데 인터넷 안 됨** | **MASQUERADE 규칙 없음.** 15-4 참고. `wg show`에 handshake 있으면 이게 원인 확정 |
| 풀터널에서 사이트 이름 해석 안 됨 | conf `[Interface]`에 `DNS = 1.1.1.1` 누락 |
| SSH `Permission denied (publickey)` | 서버 등록 공개키와 다른 키 사용. `-i`로 개인키 명시 |
| SSH `Connection timed out` | OCI Security List 22/TCP 미개방 |

## 33. nginx / TLS / 도메인

| 증상 | 원인 / 해결 |
|---|---|
| certbot 403 (`.well-known/acme-challenge`) | **Cloudflare 프록시(주황구름)** 때문. 회색(DNS only)으로 바꾸거나 **DNS-01 챌린지** 사용 |
| 와일드카드 인증서가 HTTP 챌린지로 안 됨 | 와일드카드는 **DNS-01 필수**. `--dns-cloudflare` + API 토큰 |
| `*.example.com`이 `data.unimotors.example.com`을 커버 못 함 | 별표는 **한 레벨만**. `*.unimotors.example.com` 필요 |
| 루트 도메인이 와일드카드에 안 포함 | `-d "unimotors.example.com" -d "*.unimotors.example.com"` **둘 다** 지정 |
| 502 Bad Gateway | 백엔드(기숙사 Pi / 차량 Pi)가 꺼져 있거나 VPN 끊김. OCI에서 `curl http://10.10.0.9:8090` 확인 |
| **계기판이 도메인에서 동작 안 함(직접 접속은 됨)** | **HTTP/1.0 응답 + 길이 미확정.** `protocol_version="HTTP/1.1"` + chunked + `X-Accel-Buffering: no` (23-2). nginx `proxy_buffering off`만으로는 해결 안 됨 |
| WebSocket 연결 실패 | `proxy_set_header Upgrade $http_upgrade;` + `Connection "upgrade";` 누락 |
| `nginx -t` 인증서 파일 없음 에러 | 인증서 발급 **전에** SSL 설정을 활성화함. certbot 먼저 실행 |
| 서브도메인 접속 안 됨 | Cloudflare DNS에 A 레코드 없음. `*.unimotors` 와일드카드 하나로 커버 가능 |

## 34. 웹 / 대시보드

| 증상 | 원인 / 해결 |
|---|---|
| **한글이 깨져 보임** | `<meta charset="utf-8">` 누락. 파일이 UTF-8이어도 선언이 없으면 브라우저가 오판 |
| `L is not defined`, 지도 안 뜸 | **Leaflet integrity 해시 불일치**로 브라우저가 스크립트 차단. 17-5 참고 |
| integrity 해시 정답 찾기 | 브라우저 콘솔의 `computed SHA-256 integrity '...'` 값이 실제 정답. 또는 `curl -s <url> \| openssl dgst -sha256 -binary \| openssl base64 -A` |
| **G-G 미터 dot이 좌상단 고정** | `.gg .dot`에 `position:absolute` 누락 (23-3) |
| 주행 선택이 날짜별로만 보임 | ① 구버전 dashboard.html ② **기존 데이터에 `session`이 없음**(NULL) → 새 gps_server로 주행해야 세션 생성 |
| 세션 통계의 주행시간이 몇 시간으로 뻥튀기 | **시계 점프.** 21장 참고 (`t_mono` 도입으로 해결) |
| 누적 상승고도가 0m | 샘플간 delta 방식의 노이즈 문턱 문제 → 히스테리시스로 변경 (22-9) |
| 배터리 차트가 안 보임 | 데이터에 배터리 값 없으면 **자동 숨김**(정상). BMS 연결 확인 |
| "차량 신호 없음" 오버레이 | 600초 무수신. 차량 gps_server / VPN 상태 확인 |

## 35. 서버 / DB

| 증상 | 원인 / 해결 |
|---|---|
| `Address already in use` (8080/8090) | 이미 서비스로 실행 중. `sudo systemctl stop gps-dashboard` 후 수동 실행 |
| backfill `수신 N행, 신규 0행` | 이미 업로드된 데이터 (**정상**, 중복 무시됨) |
| backfill 서버 연결 불가 | VPN 끊김 또는 서버 꺼짐. 타이머가 15분 후 재시도 |
| CSV 컬럼 수 불일치로 조용히 실패 | 차량 `TELEM_FIELDS` ↔ 서버 `FIELDS` 불일치. **반드시 동일해야 함** |
| 기존 DB에 새 컬럼이 없음 | 서버 시작 시 `ALTER TABLE` 자동 마이그레이션 (수동 작업 불필요) |
| 테스트 데이터 정리 | `sqlite3 telemetry.db "DELETE FROM summary WHERE vehicle='car1'; DELETE FROM track WHERE vehicle='car1';"` |

## 36. 접근 불가 시 최후 수단 (OCI)

SSH 키/비밀번호를 모두 잃어도 **서버를 새로 팔 필요 없다**:
1. **Cloud Shell**: OCI 콘솔 우상단 터미널 아이콘 → `ssh ubuntu@<공인IP>`
2. **Console Connection(직렬 콘솔)**: 인스턴스 → Console connection → Launch
   - 재부팅 후 GRUB 진입(ESC 연타) → 커널 라인 `ro`→`rw`, ` init=/bin/bash` 추가 →
     root 셸에서 `authorized_keys`에 공개키 추가
   - ⚠️ OCI 우분투는 GRUB 타임아웃이 0이라 잡기 어려울 수 있음
3. **부팅볼륨 분리 → 임시 인스턴스에 연결 → `authorized_keys` 수정 → 되돌리기** (가장 확실)

---

← [OCI VPS 전체 세팅 순서](15-setup-oci.md) · [문서 목차](README.md) · [레퍼런스 (명령 · 경로 · 백업 · 설계근거)](17-reference.md) →
