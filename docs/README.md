# UNIMOTORS 텔레메트리 — 문서

Raspberry Pi 기반 전기 레이싱카 텔레메트리 시스템의 전체 설계·구현·배포·운영 기록.
장별로 나누어 두었다. 통합본이 필요하면 [MASTER.md](MASTER.md)를 본다.

## 1부 — 설계와 구현

| 문서 | 내용 |
|---|---|
| [프로젝트 개요와 시스템 아키텍처](01-overview.md) | 목적 · 보유 하드웨어 · 이중 네트워크 전략 · 데이터 흐름과 레이트 설계 |
| [라즈베리파이 초기 세팅 (헤드리스)](02-pi-setup.md) | OS 굽기 · 국가코드 · 첫 부팅 주의사항 |
| [LTE 통신 (KT / SIM7600G-H)](03-lte.md) | HAT 결합 · AT+NDIS Raw IP · lte_auto.sh · systemd |
| [GPS 10Hz 계기판](04-gps.md) | NMEA 설정 · 버퍼 적체 해결 · 속도/측위 판정 로직 |
| [로컬 핫스팟 + 캡티브 포털](05-hotspot-captive.md) | nmcli AP 모드 · 인터넷 확인 요청 대응 3가지 방식 |
| [WireGuard VPN 메시](06-wireguard.md) | OCI 허브 · 피어 구성 · CGNAT keepalive · 방화벽 |
| [Daly BMS CAN 통신](07-bms-can.md) | **배터리 팩·BMS 사양** · 배선 · CAN 자동 up · DataID 0x90~0x98 |
| [텔레메트리 서버 + 실시간 대시보드](08-telemetry-server.md) | 멀티차량 · 3층 저장구조 · API · backfill |
| [하드웨어 이관과 배포 체크리스트](09-hardware-migration.md) | Zero W → 3B+/4B · 파일 배치 · 세팅 순서 |

## 2부 — 인프라 실배포

| 문서 | 내용 |
|---|---|
| [서버 인프라 실배포 (VPN · nginx · TLS)](10-infrastructure.md) | 네트워크 지형도 · 폰 풀터널 · 리버스 프록시 · 와일드카드 인증서 |

## 3부 — 데이터 모델과 분석

| 문서 | 내용 |
|---|---|
| [데이터 모델 (32필드 스키마 · 시계 점프)](11-data-model.md) | TELEM_FIELDS · 누적 사용량 · DB 마이그레이션 · monotonic 시간축 |
| [주행 분석 · 랩 분석 · 배터리 분석](12-analysis.md) | 궤적 그라데이션 · 출발선 랩 감지 · 충전 세션 · 대회 수집 권장 데이터 |

## 4부 — 초기 설정 총정리

| 문서 | 내용 |
|---|---|
| [차량 Pi 전체 세팅 순서](13-setup-vehicle.md) | 패키지 · systemd 5종 · CAN 자동 up · backfill 타이머 · DSI 키오스크 |
| [서버 Pi4 전체 세팅 순서](14-setup-server.md) | WireGuard · telemetry.service · DB 백업 cron |
| [OCI VPS 전체 세팅 순서](15-setup-oci.md) | WireGuard 허브 · 방화벽 · nginx · certbot |

## 5부 — 트러블슈팅과 레퍼런스

| 문서 | 내용 |
|---|---|
| [트러블슈팅 종합](16-troubleshooting.md) | 실제로 겪은 모든 문제 — 증상 → 원인 → 해결 |
| [레퍼런스 (명령 · 경로 · 백업 · 설계근거)](17-reference.md) | 핵심 명령 요약 · 기기별 파일 위치 · 접속 정보 · 백업 · 참고 URL |
| [현재 상태와 남은 작업](18-status-todo.md) | 완료 항목 · 진행 중 · TODO |

## 6부 — 졸업연구 V7

| 문서 | 내용 |
|---|---|
| [클러스터 연구 개선](19-cluster-research.md) | 포스터 장단점 융합 · 재시동 복원 · 유효성 · 에너지 예산 · TEAM 오더 · 검증·배포·제한 |

## 통합본

| 문서 | 내용 |
|---|---|
| [MASTER.md](MASTER.md) | 위 문서 전체를 하나로 합친 원본 통합본 |
