"""Invented UDP data, independent of the user's game or participant data."""
import struct


def telemetry(sequence=0, game=2, race=2, speed=20.0, completed=2, lap=3, viewed=0):
    b = bytearray(1367)
    struct.pack_into("<H", b, 0, 1122)
    b[2], b[3], b[4], b[5] = (sequence % 64) << 2, (5 << 4) | game, viewed, 1
    b[10], b[11] = race, 10
    for offset, value in [(12, 81.5), (16, 83.25), (20, 35.4), (116, 0.65),
                          (120, speed), (132, 124.0), (184, 0.98), (192, -1.96)]:
        struct.pack_into("<f", b, offset, value)
    struct.pack_into("<h", b, 104, 87)
    struct.pack_into("<HH", b, 124, 4500, 8000)
    b[128] = (6 << 4) | 3
    b[464 + 8], b[464 + 9], b[464 + 10] = 0x81, completed, lap
    return bytes(b)


def context():
    b = bytearray(1347)
    struct.pack_into("<H", b, 0, 1122)
    b[2] = 1
    for start, value in [(3, b"Synthetic GT"), (67, b"GT3"), (131, b"Study circuit"), (195, b"Short")]:
        b[start:start + len(value)] = value
    # Participant-name section deliberately contains data that must never be read.
    b[259:272] = b"PRIVATE-NAME!"
    return bytes(b)
