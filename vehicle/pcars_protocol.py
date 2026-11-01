"""Minimal, dependency-free Project CARS 1 UDP v1 decoder.

Wire-layout facts cross-checked against the pcars client's packet definitions:
https://github.com/jamesremuscat/pcars/tree/ccd1d72e2237879de1d401c5c9ec7c24e0bc715e
No third-party implementation or captured participant/name data is bundled.
This is not the Project CARS 2 twelve-byte-header protocol.
"""
import math
import struct

TELEMETRY_BYTES = 1367
STRINGS_BYTES = 1347
ADDITIONAL_BYTES = 1028
UDP_PORT = 5606
GAME_NAMES = {0: "exited", 1: "menu", 2: "playing", 3: "paused", 4: "restarting", 5: "replay"}


def number(data, offset, lower, upper):
    value = struct.unpack_from("<f", data, offset)[0]
    return value if math.isfinite(value) and lower <= value <= upper else None


def text(data):
    # Car/track labels only. Participant-name blocks are never decoded or stored.
    value = data.split(b"\0", 1)[0].decode("latin-1", errors="replace")
    return "".join(c if c.isprintable() else " " for c in value).strip()[:64]


def decode_packet(data):
    if len(data) < 3:
        raise ValueError("short PCARS1 header")
    build, = struct.unpack_from("<H", data)
    kind, sequence = data[2] & 3, data[2] >> 2
    if not build:
        raise ValueError("invalid build")
    if kind == 1:
        if len(data) != STRINGS_BYTES:
            raise ValueError("unsupported PCARS1 strings length")
        return {"type": "context", "build": build, "sequence": sequence,
                "car": text(data[3:67]), "car_class": text(data[67:131]),
                "track": text(data[131:195]), "variation": text(data[195:259])}
    if kind == 2:
        if len(data) != ADDITIONAL_BYTES:
            raise ValueError("unsupported PCARS1 additional length")
        return {"type": "ignored"}
    if kind != 0 or len(data) != TELEMETRY_BYTES:
        raise ValueError("unsupported packet; select Project CARS 1 UDP v1")
    viewed, participants = struct.unpack_from("<bb", data, 4)
    game_state, session_state = data[3] & 7, data[3] >> 4
    race_state = data[10] & 7
    if not 0 <= participants <= 56 or not -1 <= viewed < 56:
        raise ValueError("invalid participant bounds")
    selected = participants > 0 and 0 <= viewed < participants
    speed = number(data, 120, 0, 600 / 3.6)
    playing = game_state == 2 and session_state in range(1, 7) and race_state in (1, 2)
    if playing and (not selected or speed is None):
        raise ValueError("invalid active player/speed")
    offset = 464 + 16 * viewed if selected else None
    laps_completed = data[offset + 9] & 127 if selected else None
    current_lap = data[offset + 10] if selected else None
    position = data[offset + 8] & 127 if selected else None
    gear_bits = data[128]
    gear, gears = gear_bits & 15, gear_bits >> 4
    gear_text = "R" if gear == 15 else "N" if gear == 0 else str(gear) if gear <= gears else None
    water, = struct.unpack_from("<h", data, 104)
    return dict(
        type="telemetry", build=build, sequence=sequence,
        game_state=game_state, session_state=session_state, race_state=race_state,
        status=GAME_NAMES.get(game_state, "unknown"), active=playing,
        viewed=viewed, participants=participants,
        speed_kmh=None if speed is None else round(speed * 3.6, 4),
        rpm=struct.unpack_from("<H", data, 124)[0],
        max_rpm=struct.unpack_from("<H", data, 126)[0], gear=gear_text,
        fuel_fraction=number(data, 116, 0, 1),
        water_c=water if -40 <= water <= 180 else None,
        current_lap=current_lap, laps_completed=laps_completed,
        race_position=position, total_laps=data[11] or None,
        lap_invalid=bool(data[10] & 8) or bool(selected and data[offset + 9] & 128),
        current_lap_s=number(data, 20, 0, 86400),
        last_lap_s=number(data, 16, 0.001, 86400),
        best_lap_s=number(data, 12, 0.001, 86400),
        odometer_km=number(data, 132, 0, 10_000_000),
        # SDK local axes, not asserted to be geographic/vehicle IMU measurements.
        local_g_x=None if (x := number(data, 184, -200, 200)) is None else x / 9.80665,
        local_g_z=None if (z := number(data, 192, -200, 200)) is None else z / 9.80665,
    )


class SequenceWindow:
    """Six-bit counter, with a short window because it wraps at high packet rates."""
    def __init__(self):
        self.last = None
        self.at = None

    def accept(self, sequence, at):
        if self.at is not None and at < self.at:
            return False
        if self.last is None or at - self.at > 0.4:
            accepted = True
        else:
            delta = (sequence - self.last) % 64
            accepted = 0 < delta < 32
        if accepted:
            self.last, self.at = sequence, at
        return accepted
