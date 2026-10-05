#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bms_reader.py — Daly BMS CAN 리더 (UNIMOTORS)

실측으로 검증된 Daly CAN 프로토콜 (0x90~0x98) 파서.
- python-can 4.1.0 기준 (socketcan)
- 확장 프레임(29-bit): 우선순위(0x18) + DataID + 수신주소 + 송신주소
    PC -> BMS 요청 : 0x18 DD 01 40   (수신=BMS 0x01, 송신=PC 0x40)
    BMS -> PC 응답 : 0x18 DD 40 01
- 정격 용량 80Ah, 셀 14개, 온도센서 2개 (실측 확정값)

설계 원칙:
- 이 모듈은 "읽어서 dict로 반환"만 한다. 이벤트 루프/스케줄링은 gps_server가 소유.
- 동기 API(read_*)를 제공. gps_server에서 asyncio executor로 감싸 쓰면 됨.
- 주행거리: BMS 잔여용량(0x93, 쿨롱카운팅)을 신뢰 → 회생제동 자동 반영.
           전비(km/Ah)는 주행 중 학습(GPS 거리 / 소비 Ah). 보조 지표.
"""

import can
import struct
import time

# ---- 상수 (실측 확정) ---------------------------------------------------

PRIORITY = 0x18
ADDR_BMS = 0x01
ADDR_PC = 0x40

CURRENT_OFFSET = 30000  # Native protocol current; polarity configured at runtime.
TEMP_OFFSET = 40  # 0x92/0x96 온도: raw - 40 = ℃

RATED_CAPACITY_AH = 80.0  # 팩 정격 용량 (실측: SOC50% 잔여 40Ah 확인)

# 요청할 DataID
DID_SOC_VI = 0x90  # 누적전압/전류/SOC
DID_CELL_MINMAX = 0x91  # 셀 최대/최소 전압
DID_TEMP_MINMAX = 0x92  # 온도 최대/최소
DID_MOS = 0x93  # 상태 + MOS + 잔여용량(mAh)
DID_STATUS = 0x94  # 셀개수/센서개수/충전기·부하 상태
DID_CELL_V = 0x95  # 셀전압 (멀티프레임)
DID_CELL_T = 0x96  # 셀온도 (멀티프레임)
DID_BALANCE = 0x97  # 밸런싱 상태 비트맵
DID_FAULT = 0x98  # 고장/알람 비트맵


def _req_id(data_id: int) -> int:
    """PC->BMS 요청용 29-bit 확장 CAN ID 생성."""
    return (PRIORITY << 24) | (data_id << 16) | (ADDR_BMS << 8) | ADDR_PC


def _resp_id(data_id: int) -> int:
    """BMS->PC 응답 CAN ID (필터/식별용)."""
    return (PRIORITY << 24) | (data_id << 16) | (ADDR_PC << 8) | ADDR_BMS


# ---- 0x98 알람 비트 정의 -------------------------------------------------
# (byte_index, bit_index): (설명, 레벨)   level 1=주의(노랑), 2=위험(빨강)
FAULT_BITS = {
    (0, 0): ("셀 과전압 주의", 1),
    (0, 1): ("셀 과전압 위험", 2),
    (0, 2): ("셀 저전압 주의", 1),
    (0, 3): ("셀 저전압 위험", 2),
    (0, 4): ("총전압 과전압 주의", 1),
    (0, 5): ("총전압 과전압 위험", 2),
    (0, 6): ("총전압 저전압 주의", 1),
    (0, 7): ("총전압 저전압 위험", 2),
    (1, 0): ("충전 고온 주의", 1),
    (1, 1): ("충전 고온 위험", 2),
    (1, 2): ("충전 저온 주의", 1),
    (1, 3): ("충전 저온 위험", 2),
    (1, 4): ("방전 고온 주의", 1),
    (1, 5): ("방전 고온 위험", 2),
    (1, 6): ("방전 저온 주의", 1),
    (1, 7): ("방전 저온 위험", 2),
    (2, 0): ("충전 과전류 주의", 1),
    (2, 1): ("충전 과전류 위험", 2),
    (2, 2): ("방전 과전류 주의", 1),
    (2, 3): ("방전 과전류 위험", 2),
    (2, 4): ("SOC 높음 주의", 1),
    (2, 5): ("SOC 높음 위험", 2),
    (2, 6): ("SOC 낮음 주의", 1),
    (2, 7): ("SOC 낮음 위험", 2),
    (3, 0): ("전압 편차 주의", 1),
    (3, 1): ("전압 편차 위험", 2),
    (3, 2): ("온도 편차 주의", 1),
    (3, 3): ("온도 편차 위험", 2),
    (4, 0): ("충전MOS 고온", 2),
    (4, 1): ("방전MOS 고온", 2),
    (4, 2): ("충전MOS 온도센서 오류", 2),
    (4, 3): ("방전MOS 온도센서 오류", 2),
    (4, 4): ("충전MOS 융착", 2),
    (4, 5): ("방전MOS 융착", 2),
    (4, 6): ("충전MOS 개방", 2),
    (4, 7): ("방전MOS 개방", 2),
    (5, 0): ("AFE 수집칩 오류", 2),
    (5, 1): ("전압수집 이상", 2),
    (5, 2): ("셀 온도센서 오류", 2),
    (5, 3): ("EEPROM 오류", 2),
    (5, 4): ("RTC 오류", 1),
    (5, 5): ("프리차지 실패", 2),
    (5, 6): ("통신 오류", 2),
    (5, 7): ("내부통신 오류", 2),
    (6, 0): ("전류모듈 고장", 2),
    (6, 1): ("총전압 검출 고장", 2),
    (6, 2): ("단락보호 고장", 2),
    (6, 3): ("저전압 충전금지", 1),
}


class BMSReader:
    def __init__(
        self,
        channel: str = "can0",
        timeout: float = 0.2,
        rated_capacity_ah: float = RATED_CAPACITY_AH,
    ):
        self.channel = channel
        self.timeout = timeout
        self.rated_capacity_ah = rated_capacity_ah
        self.bus = None
        # 전비 학습용 상태
        self._efficiency_km_per_ah = None  # 학습된 전비 (없으면 None)

    # -- 연결 관리 --------------------------------------------------------
    def open(self):
        self.bus = can.interface.Bus(channel=self.channel, bustype="socketcan")

    def close(self):
        if self.bus is not None:
            self.bus.shutdown()
            self.bus = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- 저수준 요청/응답 -------------------------------------------------
    def _request(self, data_id: int, n_frames: int = 1):
        """DataID 요청 후 응답 프레임(들) 수집. n_frames>1이면 멀티프레임."""
        req = can.Message(
            arbitration_id=_req_id(data_id), data=[0x88] * 8, is_extended_id=True
        )
        self.bus.send(req)
        want = _resp_id(data_id)
        frames = []
        deadline = time.monotonic() + self.timeout * max(1, n_frames)
        seen = set()
        while time.monotonic() < deadline and len(frames) < n_frames:
            msg = self.bus.recv(timeout=max(0, deadline - time.monotonic()))
            if msg is None:
                break
            if (
                msg.arbitration_id == want
                and msg.is_extended_id
                and len(msg.data) == 8
                and not msg.is_error_frame
                and not msg.is_remote_frame
            ):
                if n_frames > 1 and (
                    msg.data[0] in seen or not 1 <= msg.data[0] <= n_frames
                ):
                    continue
                frames.append(bytes(msg.data))
                seen.add(msg.data[0])
        return frames

    # -- 개별 파서 --------------------------------------------------------
    def read_soc_vi(self):
        """0x90: native V/I and SOC. Polarity is normalized by RaceRuntime."""
        f = self._request(DID_SOC_VI)
        if not f:
            return None
        d = f[0]
        voltage = struct.unpack(">H", d[0:2])[0] * 0.1
        current = (struct.unpack(">H", d[4:6])[0] - CURRENT_OFFSET) * 0.1
        soc = struct.unpack(">H", d[6:8])[0] * 0.1
        return {
            "voltage": round(voltage, 1),
            "current": round(current, 1),
            "soc": round(soc, 1),
        }

    def read_cell_minmax(self):
        """0x91: 셀 최대/최소 전압."""
        f = self._request(DID_CELL_MINMAX)
        if not f:
            return None
        d = f[0]
        vmax = struct.unpack(">H", d[0:2])[0]
        vmax_cell = d[2]
        vmin = struct.unpack(">H", d[3:5])[0]
        vmin_cell = d[5]
        return {
            "cell_v_max": vmax,
            "cell_v_max_no": vmax_cell,
            "cell_v_min": vmin,
            "cell_v_min_no": vmin_cell,
            "cell_v_diff": vmax - vmin,
        }

    def read_temp_minmax(self):
        """0x92: 최대/최소 온도 (40 오프셋)."""
        f = self._request(DID_TEMP_MINMAX)
        if not f:
            return None
        d = f[0]
        return {
            "temp_max": d[0] - TEMP_OFFSET,
            "temp_max_no": d[1],
            "temp_min": d[2] - TEMP_OFFSET,
            "temp_min_no": d[3],
        }

    def read_mos(self):
        """0x93: 상태 + MOS + 잔여용량(mAh). 잔여용량은 회생 자동반영."""
        f = self._request(DID_MOS)
        if not f:
            return None
        d = f[0]
        state_map = {0: "정지", 1: "충전", 2: "방전"}
        remain_mah = struct.unpack(">I", d[4:8])[0]
        return {
            "state": state_map.get(d[0], f"미상({d[0]})"),
            "charge_mos": bool(d[1]),
            "discharge_mos": bool(d[2]),
            "bms_life": d[3],
            "remain_mah": remain_mah,
            "remain_ah": round(remain_mah / 1000.0, 2),
        }

    def read_status(self):
        """0x94: 셀개수/센서개수/충전기·부하 상태."""
        f = self._request(DID_STATUS)
        if not f:
            return None
        d = f[0]
        return {
            "cell_count": d[0],
            "temp_sensor_count": d[1],
            "charger_connected": bool(d[2]),
            "load_connected": bool(d[3]),
        }

    def read_cell_voltages(self, cell_count: int = 14):
        """0x95: 멀티프레임. 셀당 2byte, 프레임당 3셀, Byte0=프레임번호(1부터)."""
        n_frames = (cell_count + 2) // 3
        frames = self._request(DID_CELL_V, n_frames=n_frames)
        cells = {}
        for d in frames:
            fno = d[0]  # 1부터 시작 (실측 확인)
            base = (fno - 1) * 3
            for i in range(3):
                cell_no = base + i + 1
                if cell_no > cell_count:
                    break
                mv = struct.unpack(">H", d[1 + i * 2 : 3 + i * 2])[0]
                cells[cell_no] = mv
        return cells if cells else None

    def read_cell_temps(self, sensor_count: int = 2):
        """0x96: 멀티프레임. 센서당 1byte(40 오프셋), Byte0=프레임번호(1부터)."""
        n_frames = (sensor_count + 6) // 7
        frames = self._request(DID_CELL_T, n_frames=n_frames)
        temps = {}
        for d in frames:
            fno = d[0]
            base = (fno - 1) * 7
            for i in range(7):
                s_no = base + i + 1
                if s_no > sensor_count:
                    break
                temps[s_no] = d[1 + i] - TEMP_OFFSET
        return temps if temps else None

    def read_balance(self, cell_count: int = 14):
        """0x97: 셀별 밸런싱 비트맵 (0=닫힘, 1=밸런싱중)."""
        f = self._request(DID_BALANCE)
        if not f:
            return None
        d = f[0]
        active = []
        for cell_no in range(1, cell_count + 1):
            byte_i = (cell_no - 1) // 8
            bit_i = (cell_no - 1) % 8
            if d[byte_i] & (1 << bit_i):
                active.append(cell_no)
        return {"balancing_cells": active, "any_balancing": bool(active)}

    def read_faults(self):
        """0x98: 고장/알람 비트맵 → 사람이 읽는 경고 리스트."""
        f = self._request(DID_FAULT)
        if not f:
            return None
        d = f[0]
        warnings, dangers = [], []
        for (byte_i, bit_i), (desc, level) in FAULT_BITS.items():
            if byte_i < len(d) and (d[byte_i] & (1 << bit_i)):
                (dangers if level == 2 else warnings).append(desc)
        return {
            "fault_code": d[7] if len(d) > 7 else 0,
            "warnings": warnings,  # level 1
            "dangers": dangers,  # level 2
            "ok": not warnings and not dangers,
        }

    # -- 주행거리 추정 ----------------------------------------------------
    def update_efficiency(self, km_per_ah: float):
        """gps_server가 GPS거리/소비Ah로 학습한 전비를 주입."""
        if km_per_ah and km_per_ah > 0:
            # 지수이동평균으로 부드럽게
            if self._efficiency_km_per_ah is None:
                self._efficiency_km_per_ah = km_per_ah
            else:
                a = 0.2
                self._efficiency_km_per_ah = (
                    a * km_per_ah + (1 - a) * self._efficiency_km_per_ah
                )

    def estimate_range_km(self, remain_ah: float):
        """잔여용량(회생 반영됨) × 학습 전비 → 남은 주행거리(km)."""
        if self._efficiency_km_per_ah is None:
            return None  # 아직 학습 전
        return round(remain_ah * self._efficiency_km_per_ah, 1)

    # -- 통합 스냅샷 ------------------------------------------------------
    def read_fast(self):
        """빠른 그룹 (2~5Hz 권장): 전압/전류/SOC + 상태/잔여용량."""
        vi = self.read_soc_vi()
        vi_time = time.monotonic()
        mos = self.read_mos()
        out = {}
        received = {}
        if vi:
            out.update(vi)
            received.update({key: vi_time for key in vi})
        if mos:
            out.update(mos)
            received.update({key: time.monotonic() for key in mos})
            rng = self.estimate_range_km(mos["remain_ah"])
            if rng is not None:
                out["range_km"] = rng
                received["range_km"] = time.monotonic()
        out["_received"] = received
        return out

    def read_slow(self):
        """느린 그룹 (0.5~1Hz): 셀전압/온도/편차/밸런싱/상태정보."""
        out = {}
        received = {}
        for fn in (
            self.read_cell_minmax,
            self.read_temp_minmax,
            self.read_status,
            self.read_balance,
        ):
            r = fn()
            if r:
                out.update(r)
                received.update({key: time.monotonic() for key in r})
        out["cells"] = self.read_cell_voltages()
        out["cell_temps"] = self.read_cell_temps()
        received.update(
            {
                key: time.monotonic()
                for key in ("cells", "cell_temps")
                if out[key] is not None
            }
        )
        out["_received"] = received
        return out

    def read_alarms(self):
        """알람 그룹 (1~2Hz): 0x98만."""
        return self.read_faults()


# ---- 단독 실행 테스트 ----------------------------------------------------
if __name__ == "__main__":
    import json

    print("BMSReader 테스트 시작 (Ctrl+C 종료)")
    with BMSReader("can0") as bms:
        try:
            while True:
                snap = {}
                snap.update(bms.read_fast() or {})
                snap.update(bms.read_slow() or {})
                snap["faults"] = bms.read_alarms()
                print(json.dumps(snap, ensure_ascii=False, indent=2))
                print("-" * 50)
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n종료")
