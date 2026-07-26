import json
import pytest

from paneglow.protocol import (
    USB, BLE, thstatus, rgbcfg, status_request, frame, FrameDecoder,
)


def test_thstatus_is_a_notification_without_id():
    """Putting an id on a v.oai.* message returns 404 Method not found."""
    msg = thstatus([0xFF0000] + [None] * 5)
    assert msg["m"] == "v.oai.thstatus"
    assert "id" not in msg


def test_thstatus_has_six_entries_with_ids():
    msg = thstatus([0xFF0000] + [None] * 5)
    assert [e["id"] for e in msg["p"]] == [0, 1, 2, 3, 4, 5]


def test_thstatus_none_is_dark_not_dim():
    """An empty slot must be off, not dim -- only then can the count be read."""
    entry = thstatus([None] * 6)["p"][0]
    assert entry["c"] == 0 and entry["b"] == 0 and entry["e"] == 0


def test_thstatus_colour_is_solid_full_brightness():
    entry = thstatus([0x304FFE] + [None] * 5)["p"][0]
    assert entry["c"] == 0x304FFE and entry["e"] == 1 and entry["b"] == 1


def test_thstatus_rejects_wrong_length():
    with pytest.raises(ValueError):
        thstatus([0xFF0000])


def test_status_request_has_an_id():
    """device.status is a request, so it needs an id."""
    assert status_request(9)["id"] == 9


def test_usb_framing_prefix_and_length():
    packets = frame({"m": "x"}, USB)
    assert len(packets) == 1
    assert packets[0][0] == 0x02
    assert len(packets[0]) == 63


def test_ble_framing_has_report_id_prefix_and_64_bytes():
    packets = frame({"m": "x"}, BLE)
    assert packets[0][0] == 0x06 and packets[0][1] == 0x02
    assert len(packets[0]) == 64


def test_framing_carries_the_json():
    packets = frame({"m": "x"}, USB)
    length = packets[0][1]
    assert json.loads(packets[0][2:2 + length].decode().rstrip("\r\n")) == {"m": "x"}


def test_long_message_spans_several_packets():
    big = {"m": "v.oai.thstatus", "p": [{"id": i, "c": 0xFFFFFF, "b": 1, "e": 1, "s": 0}
                                        for i in range(6)]}
    assert len(frame(big, USB)) >= 2


def test_decoder_reassembles_a_message():
    dec = FrameDecoder()
    out = []
    for packet in frame({"m": "hello", "p": {"a": 1}}, USB):
        out += dec.feed(packet)
    assert out == [{"m": "hello", "p": {"a": 1}}]


def test_decoder_ignores_garbage():
    assert FrameDecoder().feed(b"\x00" * 63) == []


@pytest.mark.parametrize("chunk", [b"", b"\x06", b"\x02", b"\x06\x02", b"\x02\x05"])
def test_decoder_survives_a_truncated_report(chunk):
    """A short read must not be indexed into. feed(b"\\x06\\x02") used to raise
    IndexError and take the read loop down with it."""
    assert FrameDecoder().feed(chunk) == []


def test_decoder_drops_a_report_that_overstates_its_length():
    """Declaring 200 bytes while carrying 7 -- taking the partial payload would
    contaminate the next message."""
    dec = FrameDecoder()
    assert dec.feed(bytes([0x02, 200]) + b'{"a":1}') == []
    assert len(dec._buf) == 0

    out = []
    for packet in frame({"m": "next"}, USB):
        out += dec.feed(packet)
    assert out == [{"m": "next"}]


def test_frame_rejects_an_unknown_transport():
    """Falling through to BLE would produce packets the device drops silently."""
    with pytest.raises(ValueError):
        frame({"m": "x"}, "usb")
    with pytest.raises(ValueError):
        frame({"m": "x"}, "")


def test_decoder_buffer_stays_bounded():
    """The daemon runs for days. A dropped packet leaves a fragment that never
    terminates, and without a cap every resync appends behind it forever."""
    dec = FrameDecoder()
    for _ in range(1000):
        dec.feed(bytes([0x02, 60]) + b"x" * 61)
    assert len(dec._buf) <= 4096


def test_decoder_resyncs_after_a_flood():
    """A desync always costs the one message the leftover is glued to. What must
    not happen is staying broken: everything after that has to decode again."""
    dec = FrameDecoder()
    for _ in range(1000):
        dec.feed(bytes([0x02, 60]) + b"x" * 61)

    out = []
    for message in ({"m": "first"}, {"m": "second"}):
        for packet in frame(message, USB):
            out += dec.feed(packet)

    assert {"m": "second"} in out, "decoder never resynced"
    assert len(dec._buf) == 0


def test_rgbcfg_touches_only_the_zone_you_name():
    assert set(rgbcfg(ambient=0xFF6D00)["p"]) == {"ambient"}
    assert set(rgbcfg(keys=None)["p"]) == {"keys"}


def test_rgbcfg_needs_a_zone():
    with pytest.raises(ValueError):
        rgbcfg()
