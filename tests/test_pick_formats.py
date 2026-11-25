import pytest

from app import _build_ffmpeg_args, _pick_formats


def test_prefers_best_muxed_format():
    formats = [
        {"format_id": "96", "url": "u96", "protocol": "m3u8_native",
         "vcodec": "avc1", "acodec": "mp4a.40.2", "height": 1080, "tbr": 4000},
        {"format_id": "137", "url": "u137", "vcodec": "avc1", "acodec": "none",
         "height": 1080, "tbr": 4500},
        {"format_id": "140", "url": "u140", "vcodec": "none", "acodec": "mp4a.40.2",
         "abr": 128},
    ]
    selected = _pick_formats(formats)
    assert selected["kind"] == "muxed"
    assert selected["media_url"] == "u96"
    assert "url" not in selected["debug"][0]


def test_selects_separate_video_and_audio_only_when_no_muxed():
    selected = _pick_formats([
        {"format_id": "137", "url": "video", "vcodec": "avc1", "acodec": "none",
         "height": 1080, "tbr": 4500},
        {"format_id": "140", "url": "audio", "vcodec": "none", "acodec": "mp4a.40.2",
         "abr": 128},
        {"format_id": "18", "url": "video-audio", "vcodec": "avc1", "acodec": "none",
         "height": 720, "tbr": 1800},
    ])
    assert selected["kind"] == "pair"
    assert selected["media_url"] == "video"
    assert selected["audio_url"] == "audio"


def test_selects_unknown_codec_format_as_possible_muxed():
    selected = _pick_formats([
        {"format_id": "live", "url": "unknown", "vcodec": "avc1", "acodec": None,
         "height": 720, "tbr": 1500},
    ])
    assert selected["kind"] == "unknown"
    assert selected["media_url"] == "unknown"


def test_empty_formats_raise_controlled_error():
    with pytest.raises(ValueError, match="formatos de video compatibles"):
        _pick_formats([])


@pytest.mark.parametrize(
    ("kind", "expected_maps", "input_count"),
    [
        ("muxed", ["0:v:0", "0:a:0"], 1),
        ("pair", ["0:v:0", "1:a:0"], 2),
        ("unknown", ["0:v:0?", "0:a:0?"], 1),
    ],
)
def test_ffmpeg_args_put_reconnect_before_each_input(kind, expected_maps, input_count):
    resolved = {"kind": kind, "media_url": "video-url", "audio_url": "audio-url",
                "headers": {"User-Agent": "ua"}, "audio_headers": {"User-Agent": "ua"}}
    args = _build_ffmpeg_args(resolved, "capture.mkv")
    inputs = [index for index, value in enumerate(args) if value == "-i"]
    reconnects = [index for index, value in enumerate(args) if value == "-reconnect"]
    assert len(inputs) == input_count
    assert len(reconnects) == input_count
    for position, input_index in enumerate(inputs):
        lower_bound = inputs[position - 1] if position else -1
        assert any(lower_bound < reconnect < input_index for reconnect in reconnects)
    assert [args[index + 1] for index, value in enumerate(args) if value == "-map"] == expected_maps
