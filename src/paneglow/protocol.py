"""벤더 JSON-RPC 메시지와 HID 프레이밍.

프레이밍이 전송별로 다른 것이 이 기기의 가장 큰 함정이다. 잘못 프레이밍한
write 도 성공을 반환하고 조용히 버려지므로, 여기서 틀리면 증상이 "아무 일도
안 일어남"으로만 나타난다.
"""
from __future__ import annotations

import json

USB = "USB"
BLE = "BLE"

_METHOD_THSTATUS = "v.oai.thstatus"
_METHOD_RGBCFG = "v.oai.rgbcfg"

_EFFECT_OFF = 0
_EFFECT_SOLID = 1

#: 페이로드가 들어갈 자리. USB 는 [0x02][len], BLE 는 앞에 리포트 id 가 하나 더 붙는다.
_USB_SIZE, _BLE_SIZE = 63, 64
_KEY_COUNT = 6


def _entry(index: int, color: int | None) -> dict:
    if color is None:
        return {"id": index, "c": 0, "b": 0, "e": _EFFECT_OFF, "s": 0}
    return {"id": index, "c": color, "b": 1, "e": _EFFECT_SOLID, "s": 0}


def thstatus(colors: list[int | None]) -> dict:
    """Agent 키 6개를 각각 칠한다. notification 이므로 id 를 넣지 않는다."""
    if len(colors) != _KEY_COUNT:
        raise ValueError(f"colors must have {_KEY_COUNT} entries, got {len(colors)}")
    return {"m": _METHOD_THSTATUS,
            "p": [_entry(i, c) for i, c in enumerate(colors)]}


def _side(color: int | None) -> dict:
    if color is None:
        return {"e": _EFFECT_OFF, "b": 0, "s": 0, "c": 0}
    return {"e": _EFFECT_SOLID, "b": 1, "s": 0, "c": color}


#: "생략" 과 "끄기(None)" 를 구분해야 해서 별도 센티널이 필요하다.
_UNSET = object()


def rgbcfg(keys: int | None | object = _UNSET,
           ambient: int | None | object = _UNSET) -> dict:
    """C키 백라이트(keys)와 테두리(ambient). 생략한 존은 건드리지 않는다."""
    params: dict = {}
    if keys is not _UNSET:
        params["keys"] = _side(keys)          # type: ignore[arg-type]
    if ambient is not _UNSET:
        params["ambient"] = _side(ambient)    # type: ignore[arg-type]
    if not params:
        raise ValueError("rgbcfg needs at least one of keys / ambient")
    return {"m": _METHOD_RGBCFG, "p": params}


def status_request(req_id: int = 1) -> dict:
    """유일하게 믿을 수 있는 건강 확인. 응답이 와야 프레이밍이 맞는 것이다."""
    return {"m": "device.status", "id": req_id}


def frame(message: dict, transport: str) -> list[bytes]:
    """메시지를 리포트 크기로 자른다. 메시지는 \\r\\n 으로 끝난다."""
    body = (json.dumps(message, separators=(",", ":")) + "\r\n").encode()
    prefix = b"" if transport == USB else b"\x06"
    size = _USB_SIZE if transport == USB else _BLE_SIZE
    room = size - len(prefix) - 2          # 0x02 와 길이 바이트를 뺀 나머지

    packets = []
    for i in range(0, len(body), room):
        chunk = body[i:i + room]
        packet = prefix + bytes([0x02, len(chunk)]) + chunk
        packets.append(packet.ljust(size, b"\x00"))
    return packets


class FrameDecoder:
    """조각난 리포트를 다시 메시지로 잇는다."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[dict]:
        if len(chunk) < 2:
            return []
        # 입력 리포트도 [0x02][len] 로 시작한다. 아니면 우리 것이 아니다.
        start = 1 if chunk[0] == 0x06 else 0
        if chunk[start] != 0x02:
            return []
        length = chunk[start + 1]
        self._buf += chunk[start + 2:start + 2 + length]

        out = []
        while b"\r\n" in self._buf:
            line, _, rest = bytes(self._buf).partition(b"\r\n")
            self._buf = bytearray(rest)
            try:
                out.append(json.loads(line.decode()))
            except Exception:
                pass          # 못 읽는 줄은 버린다
        return out
