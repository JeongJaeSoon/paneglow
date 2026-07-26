"""Vendor JSON-RPC messages and HID framing.

Framing differing per transport is this device's worst trap. A wrongly framed
write still returns success and is silently dropped, so a mistake here shows up
only as "nothing happened".
"""
from __future__ import annotations

import json

USB = "USB"
BLE = "BLE"

_METHOD_THSTATUS = "v.oai.thstatus"
_METHOD_RGBCFG = "v.oai.rgbcfg"

_EFFECT_OFF = 0
_EFFECT_SOLID = 1

#: Room for the payload. USB is [0x02][len]; BLE prefixes one more report id byte.
_USB_SIZE, _BLE_SIZE = 63, 64
_KEY_COUNT = 6


def _entry(index: int, color: int | None) -> dict:
    if color is None:
        return {"id": index, "c": 0, "b": 0, "e": _EFFECT_OFF, "s": 0}
    return {"id": index, "c": color, "b": 1, "e": _EFFECT_SOLID, "s": 0}


def thstatus(colors: list[int | None]) -> dict:
    """Paint the six agent keys. A notification, so it carries no id."""
    if len(colors) != _KEY_COUNT:
        raise ValueError(f"colors must have {_KEY_COUNT} entries, got {len(colors)}")
    return {"m": _METHOD_THSTATUS,
            "p": [_entry(i, c) for i, c in enumerate(colors)]}


def _side(color: int | None) -> dict:
    if color is None:
        return {"e": _EFFECT_OFF, "b": 0, "s": 0, "c": 0}
    return {"e": _EFFECT_SOLID, "b": 1, "s": 0, "c": color}


#: Needed to tell "leave this zone alone" apart from "turn this zone off" (None).
_UNSET = object()


def rgbcfg(keys: int | None | object = _UNSET,
           ambient: int | None | object = _UNSET) -> dict:
    """C-key backlight (keys) and border (ambient). Omitted zones are untouched."""
    params: dict = {}
    if keys is not _UNSET:
        params["keys"] = _side(keys)          # type: ignore[arg-type]
    if ambient is not _UNSET:
        params["ambient"] = _side(ambient)    # type: ignore[arg-type]
    if not params:
        raise ValueError("rgbcfg needs at least one of keys / ambient")
    return {"m": _METHOD_RGBCFG, "p": params}


def status_request(req_id: int = 1) -> dict:
    """The only trustworthy health check. A reply proves the framing is right."""
    return {"m": "device.status", "id": req_id}


def frame(message: dict, transport: str) -> list[bytes]:
    """Cut a message into report-sized packets. The message ends with \\r\\n."""
    if transport not in (USB, BLE):
        # Falling through to BLE framing would produce packets the device drops
        # without a word -- the one failure mode this file exists to prevent.
        raise ValueError(f"unsupported transport: {transport!r}")

    body = (json.dumps(message, separators=(",", ":")) + "\r\n").encode()
    prefix = b"" if transport == USB else b"\x06"
    size = _USB_SIZE if transport == USB else _BLE_SIZE
    room = size - len(prefix) - 2          # minus the 0x02 and the length byte

    packets = []
    for i in range(0, len(body), room):
        chunk = body[i:i + room]
        packet = prefix + bytes([0x02, len(chunk)]) + chunk
        packets.append(packet.ljust(size, b"\x00"))
    return packets


#: A full thstatus is about 300 bytes, so anything past this is a lost terminator,
#: not a real message. Without a cap the daemon leaks: one dropped packet leaves a
#: fragment that never completes, and every resync appends behind it.
_MAX_BUFFER = 4096


class FrameDecoder:
    """Reassembles split reports back into messages.

    A dropped packet costs exactly one message: the leftover fragment glues onto
    the front of the next one and that line fails to parse. Everything after it
    decodes normally. Retries cover the loss, so it is not worth real resync
    machinery here.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[dict]:
        if not chunk:
            return []
        start = 1 if chunk[0] == 0x06 else 0
        # A short read must not be indexed into -- header bytes may be missing.
        if len(chunk) < start + 2:
            return []
        # Input reports start with [0x02][len] too. Anything else is not ours.
        if chunk[start] != 0x02:
            return []

        length = chunk[start + 1]
        end = start + 2 + length
        if len(chunk) < end:
            # Declared more than it carries. Taking the partial payload would
            # contaminate the next message, so drop the whole report.
            return []
        self._buf += chunk[start + 2:end]

        out = []
        while b"\r\n" in self._buf:
            line, _, rest = bytes(self._buf).partition(b"\r\n")
            self._buf = bytearray(rest)
            try:
                out.append(json.loads(line.decode()))
            except Exception:
                pass          # drop lines we cannot read

        # Whatever is left has no terminator by construction. Past the cap it
        # never will, so drop it whole -- keeping part of it would glue garbage
        # onto the front of the next good message and lose that one too.
        if len(self._buf) > _MAX_BUFFER:
            self._buf.clear()
        return out
