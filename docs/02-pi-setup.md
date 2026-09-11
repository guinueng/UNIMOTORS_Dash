# 라즈베리파이 초기 세팅 (헤드리스)

> OS 굽기 · 국가코드 · 첫 부팅 주의사항

---

## 3. 1단계: 라즈베리파이 초기 세팅 (헤드리스)

### 3-1. OS 굽기 (Raspberry Pi Imager)
1. CHOOSE DEVICE: 해당 Pi 모델
2. OS: Raspberry Pi OS Lite (Zero는 32-bit Bookworm)
   - Trixie(테스팅) 피할 것 — Zero W에서 NetworkManager 충돌
3. 설정(톱니바퀴)에서 반드시:
   - SSH 활성화 (비밀번호 인증)
   - WiFi SSID/비밀번호 (**2.4GHz** — Zero W는 5GHz 미지원)
   - **무선 LAN 국가 = KR** ← 누락 시 WiFi 안 잡히는 최대 원인

### 3-2. 부팅 주의
- **LTE HAT은 초기 세팅 끝난 뒤 결합** (USB 라인 점유로 OTG 충돌 방지)
- `config.txt`/`cmdline.txt` 건드리지 말 것 (수동 수정이 부팅 실패 유발)
- Zero W 첫 부팅 3~5분 대기

---

← [프로젝트 개요와 시스템 아키텍처](01-overview.md) · [문서 목차](README.md) · [LTE 통신 (KT / SIM7600G-H)](03-lte.md) →
