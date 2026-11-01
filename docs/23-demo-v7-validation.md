# v7 데모 통합 검증 · 2026-11-01

추가 구현: Project CARS 1 UDP 어댑터와 선택형 Claude 테마를 반영한 현재 전체 시험은 **101개**다. [수신·테마·추가 검증 범위](24-pcars-and-themes.md)를 참고한다. 아래 81개와 표본 건수는 앞선 데모 통합 단계의 기록이다.

PR #2의 v6 데모 커밋 `4e95d68`에 최신 v7 브랜치 `035ae82`의 가상 센서 연동·공통 모듈을 통합하고, 설치 설정 보존·검증 실패 처리와 자동 검증을 보완했다. 현재 코드는 호환되지 않는 조합을 막는 데 그치지 않고 실제 v7 런타임·새 화면·로깅·전송을 사용한다.

## 수정 내용

- 설치기는 `gps_server.py`, `race_runtime.py`, `telemetry_protocol.py`, `driver_dashboard.html`과 역할별 파일을 함께 설치한다. 임시 폴더에서 문법·v7 import·화면·데모 계산을 확인하고 성공한 경우에만 기존 파일을 교체한다.
- `/etc/default/unimotors`의 기존 키·주석·다중행 값은 보존한다. 유효 토큰은 마지막 관리 블록에서 갱신하고 원자적 교체·권한 600을 적용한다.
- 데모 GPS/BMS 입력은 RaceRuntime으로 들어간다. 전류 부호·배율은 합성 모델의 방전 + 기준으로 초기화하고, 실제 Daly 설정과 상태 DB를 분리한다. 데이터 출처는 `demo_sim v2.1 (synthetic)`이다.
- 새 운전자 화면에 DEMO 표시가 있고, 기본·내구·정비 화면과 `/legacy` 비교 화면을 제공한다.
- `--bind`, `--port`, `--duration`을 추가해 PC의 임시 포트에서도 시험하고 정상 종료할 수 있다. 잘못된 표본 주기·종료 시간·포트는 시작 전에 거부한다.
- 제어 API가 인증 실패 시에도 크기가 제한된 요청 본문을 소비하도록 해 HTTP keep-alive 연결에서 남은 JSON이 다음 요청으로 해석되지 않게 했다. 실제 연결을 재사용하는 회귀 시험을 추가했다.

## 실행한 검증

`python -m pytest -q`: **81개 통과**. 기존 복원·극성·랩·서버·전송 시험과 새 설치기·데모 시험을 포함한다. 로컬 설치 폴더에 v6 데모만 남아 있는 혼합 구성도 교체 전에 거부한다.

새 시험은 기존 설정 보존, 특수문자·다중행 값, 토큰 갱신 재실행, 잘못된 토큰 입력, dry-run의 무변경, 누락 파일·문법 오류·계산 실패 시 기존 파일 보존, demo/car의 평면 v7 설치·재실행, 실제 HTTP/SSE/CSV, 실차 DB 분리와 잘못된 실행 옵션을 확인한다. 설치기 시험에서는 실제 sudo/APT/systemd/WireGuard를 실행하지 않는다.

`ruff check vehicle server shared tests scripts/demo-browser-smoke.py --select F`: 통과. 설치 스크립트 3개의 `bash -n`: 통과.

`python scripts/demo-browser-smoke.py`: 실제 데모 프로세스·로컬 v7 피트 서버·Chromium으로 확인했다.

- 800×480 기본·내구·정비 화면, DEMO 표시, SSE 갱신. JavaScript 오류 없음.
- 실제 로컬 WebSocket batch와 서버 커밋 후 ACK.
- 서버를 중단했을 때 미전송 표본이 DB에 남고, 같은 서버 DB로 재시작한 뒤 전송 복구.
- 이번 고정 길이 실행에서는 단절 중 쌓인 **10개 표본**을 복구했고 서버의 **236개 레코드가 모두 고유 표본 ID**였다. 건수는 실행 타이밍에 따라 바뀌므로 고정된 성능 기준으로 해석하지 않는다.
- 정상 종료 후 Windows에서도 읽기 DB 연결과 자식 프로세스를 닫아 임시 파일을 정리한다.

자세한 개발 증거는 `.qa/demo-browser-results.json`, `.qa/demo-browser.log`, `.qa/demo-v7-*.png`에 생성된다. 합성 데이터이며 실제 연구 참여자의 응답이 아니다. CI에도 설치기 시험·데모 브라우저 시험을 포함했다. CI의 원격 실행 결과는 GitHub Actions에서 별도로 확인한다.

## 수행하지 않은 검증

실제 Raspberry Pi 3B+·5인치 DSI·CAN/Daly·WireGuard·교내 Wi-Fi·물리 전원 차단·실차 배포는 이번 수정에서 실행하지 않았다. Project CARS 1은 수신 어댑터 구현과 가상 UDP 통신 검증까지 진행했고, 사용자 보유 게임의 직접 실행·실제 패킷 수집·학생 실험은 아직 수행하지 않았다.
