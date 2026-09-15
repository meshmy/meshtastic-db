"""FrameReader/encode_frame: the Meshtastic local-API wire framing used by
tcp-poller (§1/§5), tested as a pure byte-buffer state machine — no socket
or Docker needed."""

from meshdb_common.stream_framing import (
    HEADER_LEN,
    MAX_FRAME_SIZE,
    START1,
    START2,
    FrameReader,
    encode_frame,
)


def test_encode_frame_prefixes_magic_bytes_and_big_endian_length():
    framed = encode_frame(b"hello")
    assert framed[0] == START1
    assert framed[1] == START2
    assert framed[2:4] == (5).to_bytes(2, "big")
    assert framed[HEADER_LEN:] == b"hello"


def test_frame_reader_yields_one_frame_fed_whole():
    reader = FrameReader()
    frames = reader.feed(encode_frame(b"abc"))
    assert frames == [b"abc"]


def test_frame_reader_reassembles_a_frame_split_across_chunks():
    reader = FrameReader()
    framed = encode_frame(b"reassembled")
    assert reader.feed(framed[:2]) == []
    assert reader.feed(framed[2:5]) == []
    assert reader.feed(framed[5:]) == [b"reassembled"]


def test_frame_reader_yields_multiple_frames_fed_together():
    reader = FrameReader()
    frames = reader.feed(encode_frame(b"one") + encode_frame(b"two"))
    assert frames == [b"one", b"two"]


def test_frame_reader_resyncs_past_garbage_bytes():
    reader = FrameReader()
    frames = reader.feed(b"\x00\x01garbage" + encode_frame(b"payload"))
    assert frames == [b"payload"]


def test_frame_reader_rejects_oversized_length_and_resyncs():
    reader = FrameReader()
    bogus_header = bytes([START1, START2, 0xFF, 0xFF])  # length far past MAX_FRAME_SIZE
    frames = reader.feed(bogus_header + encode_frame(b"ok"))
    assert frames == [b"ok"]


def test_frame_reader_accepts_max_size_frame():
    reader = FrameReader()
    payload = b"x" * MAX_FRAME_SIZE
    assert reader.feed(encode_frame(payload)) == [payload]
