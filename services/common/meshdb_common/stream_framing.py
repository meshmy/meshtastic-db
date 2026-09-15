"""Wire framing for Meshtastic's local API stream protocol (the same
framing a TCP or serial local-API connection uses): each ToRadio/FromRadio
protobuf message is prefixed with a 4-byte header — two magic bytes, then a
big-endian uint16 payload length.

Implemented directly, rather than depending on the `meshtastic` PyPI
package's StreamInterface, which parses incoming frames with its own
(potentially stale) bundled protobuf classes before handing anything back to
a caller — the same problem flagged for BLE/serial transport: this repo's
own regenerated classes must always do the field-level decode. TCP framing
is simple and fully documented (unlike BLE GATT/pypubsub), so no upstream
client library is needed here at all.
"""

from __future__ import annotations

START1 = 0x94
START2 = 0xC3
HEADER_LEN = 4
MAX_FRAME_SIZE = 512


def encode_frame(payload: bytes) -> bytes:
    """Wrap a serialized ToRadio message in the wire header."""
    length = len(payload)
    return bytes([START1, START2, (length >> 8) & 0xFF, length & 0xFF]) + payload


class FrameReader:
    """Incremental parser: `feed()` accepts arbitrary chunks of bytes read
    off a socket and returns any complete FromRadio frame payloads found so
    far. A pure byte-buffer state machine with no I/O of its own, so it's
    testable without a real socket."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf.extend(chunk)
        frames: list[bytes] = []
        while True:
            while self._buf and self._buf[0] != START1:
                del self._buf[0]
            if len(self._buf) < 2:
                break
            if self._buf[1] != START2:
                del self._buf[0]
                continue
            if len(self._buf) < HEADER_LEN:
                break
            length = (self._buf[2] << 8) | self._buf[3]
            if length > MAX_FRAME_SIZE:
                del self._buf[0]
                continue
            if len(self._buf) < HEADER_LEN + length:
                break
            frames.append(bytes(self._buf[HEADER_LEN : HEADER_LEN + length]))
            del self._buf[: HEADER_LEN + length]
        return frames
