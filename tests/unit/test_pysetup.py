"""GDB-side helper ``tgdb_pysetup.py`` loaded under a stub ``gdb`` module.

Covers the pure parts that run inside GDB: the UTF-8 octal-escape decoder
and the side-channel frame encoder, checked against the tgdb-side decoder.
"""

import importlib.util
import json
import sys
import types
import zlib
from pathlib import Path

import pytest

from tgdb.gdb_controller.socket_data import SocketDataMixin, _decode_varint

from .test_value_format import UTF8_OCTAL_CASES

PYSETUP = Path(__file__).resolve().parents[2] / "tgdb" / "tgdb_pysetup.py"


class _StubFunction:
    def __init__(self, name):
        self.name = name


@pytest.fixture
def pysetup(monkeypatch):
    stub = types.ModuleType("gdb")
    stub.parameter = lambda name: 0
    stub.execute = lambda *args, **kwargs: ""
    stub.Function = _StubFunction
    monkeypatch.setitem(sys.modules, "gdb", stub)
    spec = importlib.util.spec_from_file_location("_tgdb_pysetup_under_test", PYSETUP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("raw", "expected"), UTF8_OCTAL_CASES)
def test_gdb_side_utf8_decoder_matches_tgdb_side(pysetup, raw, expected):
    """Regression: the GDB-side regex matched ``\\\\NNN`` and raised ValueError."""
    assert pysetup._decode_utf8_octal_escapes(raw) == expected


@pytest.mark.parametrize("value", [0, 1, 127, 128, 300, 2**31, 2**63])
def test_varint_round_trip(pysetup, value):
    encoded = pysetup._encode_varint(value)
    assert _decode_varint(encoded, 0) == (value, len(encoded))
    assert pysetup._decode_varint(encoded, 0) == (value, len(encoded))


class _CapturingSocket:
    def __init__(self):
        self.data = b""


    def sendall(self, buf):
        self.data += buf


class _Decoder(SocketDataMixin):
    def __init__(self):
        self._sock_buf = b""


def _decode_frames(data: bytes, chunk: int) -> list[tuple]:
    decoder = _Decoder()
    frames: list[tuple] = []
    for start in range(0, len(data), chunk):
        decoder._sock_buf += data[start:start + chunk]
        while True:
            frame = decoder._extract_one_sock_frame()
            if frame is None:
                break
            frames.append(frame)
    assert decoder._sock_buf == b""
    return frames


@pytest.mark.parametrize("chunk", [1, 7, 1 << 20])
def test_payload_frames_decode_on_tgdb_side(pysetup, monkeypatch, chunk):
    """Frames written by GDB must decode identically however reads split them."""
    sock = _CapturingSocket()
    monkeypatch.setattr(pysetup, "_sock_obj", sock)
    small = [{"name": "x", "value": "1"}]
    large = [{"name": f"v{i}", "value": "é" * 20} for i in range(50)]
    assert pysetup._send_sock_payload("l", small, token=5)
    assert pysetup._send_sock_payload("s", large, token=300)
    assert pysetup._send_sock_frame("D", b"log line\n")

    frames = _decode_frames(sock.data, chunk)
    assert [chr(frame[0]) for frame in frames] == ["l", "s", "D"]

    decoded = []
    for _tag, ctl, payload in frames[:2]:
        if ctl & 0x01:
            payload = zlib.decompress(payload)
        token, offset = _decode_varint(payload, 0)
        decoded.append((token, json.loads(payload[offset:].decode("utf-8"))))
    assert decoded == [(5, small), (300, large)]
    assert frames[1][1] & 0x01, "large payload should be compressed"
    assert frames[2][2] == b"log line\n"
