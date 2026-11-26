from pathlib import Path
from io import BytesIO

import app


def test_concat_renames_a_single_part(tmp_path):
    part = tmp_path / "part01.mkv"
    target = tmp_path / "capture.mkv"
    part.write_bytes(b"mkv-data" * 300)
    row = {"file": str(target), "folder": str(tmp_path), "segments": [part.name]}

    assert app._concat_segments(row) is True
    assert target.is_file()
    assert not part.exists()


def test_concat_uses_concat_demuxer_for_multiple_parts(tmp_path, monkeypatch):
    parts = [tmp_path / "part01.mkv", tmp_path / "part02.mkv"]
    for part in parts:
        part.write_bytes(b"mkv-data" * 300)
    target = tmp_path / "capture.mkv"
    row = {"file": str(target), "folder": str(tmp_path),
           "segments": [part.name for part in parts]}
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b"joined-data" * 300)

    monkeypatch.setattr(app.shutil, "which", lambda _name: "ffmpeg.exe")
    monkeypatch.setattr(app.subprocess, "run", fake_run)

    assert app._concat_segments(row) is True
    assert target.is_file()
    assert calls[0][calls[0].index("-f") + 1] == "concat"
    assert calls[0][calls[0].index("-safe") + 1] == "0"
    list_content = (tmp_path / "list.txt").read_text(encoding="utf-8")
    assert "part01.mkv" in list_content
    assert "part02.mkv" in list_content


def test_pause_resume_creates_parts_and_stop_joins_them(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT", tmp_path)
    monkeypatch.setattr(app, "STATE", tmp_path / "recordings.json")
    monkeypatch.setattr(app, "_watch_progress", lambda *_args: None)
    monkeypatch.setattr(app, "_verify_audio_after_start", lambda *_args: None)
    monkeypatch.setattr(app, "_has_audio", lambda _path: True)
    monkeypatch.setattr(app, "_has_video", lambda _path: True)
    monkeypatch.setattr(app, "_make_cover", lambda _row: None)
    monkeypatch.setattr(app.shutil, "which", lambda _name: "ffmpeg.exe")
    app.PROCESSES.clear()
    app.STOPPING.clear()
    app.PAUSING.clear()

    resolve_count = 0

    def fake_resolve(_url):
        nonlocal resolve_count
        resolve_count += 1
        return {"title": "Live", "kind": "muxed", "media_url": f"stream-{resolve_count}",
                "audio_url": None, "headers": {}, "audio_headers": {},
                "debug": [{"format_id": str(resolve_count)}]}

    class FakePopen:
        next_pid = 5000

        def __init__(self, args, **_kwargs):
            self.args = args
            self.pid = FakePopen.next_pid
            FakePopen.next_pid += 1
            self.stdin = BytesIO()
            self.stdout = BytesIO()
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            Path(self.args[-1]).write_bytes(b"part-data" * 300)
            self.returncode = 0
            return 0

        def terminate(self):
            self.returncode = -1

        def kill(self):
            self.returncode = -9

    def fake_run(args, **_kwargs):
        Path(args[-1]).write_bytes(b"joined-data" * 300)

    monkeypatch.setattr(app, "_resolve", fake_resolve)
    monkeypatch.setattr(app.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(app.subprocess, "run", fake_run)

    started = app.start(app.StartRequest(url="https://youtube.com/watch?v=live"))
    assert started["segments"] == ["part01.mkv"]
    paused = app.pause(started["id"])
    assert paused["status"] == "pausada"
    resumed = app.resume(started["id"])
    assert resumed["segments"] == ["part01.mkv", "part02.mkv"]
    finished = app.stop(started["id"])

    assert finished["status"] == "finalizada"
    assert Path(finished["file"]).is_file()
    assert resolve_count == 2


def test_unexpected_exit_tries_reconnect_at_most_three_times(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "STATE", tmp_path / "recordings.json")
    monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)
    rec_id = "test-reconnect"
    folder = tmp_path / "capture"
    folder.mkdir()
    row = {"id": rec_id, "url": "https://youtube.com/watch?v=live", "status": "grabando",
           "folder": str(folder), "file": str(folder / "capture.mkv"), "segments": [],
           "elapsed_seconds": 0, "capture_base_seconds": 0, "capture_started_at": None,
           "reconnections": 0}
    app._write([row])

    class ExitedPopen:
        stdout = BytesIO()

        def wait(self):
            return 1

        def poll(self):
            return 1

    proc = ExitedPopen()
    app.PROCESSES[rec_id] = proc
    app.STOPPING.discard(rec_id)
    app.PAUSING.discard(rec_id)
    attempts = 0

    def failed_resolve(_url):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("temporary stream error")

    monkeypatch.setattr(app, "_resolve", failed_resolve)
    app._watch_progress(rec_id, proc)

    assert attempts == 3
    assert app._get(rec_id)["status"] == "error"


def test_audio_check_reports_missing_video(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "STATE", tmp_path / "recordings.json")
    monkeypatch.setattr(app, "_has_audio", lambda _path: True)
    monkeypatch.setattr(app, "_has_video", lambda _path: False)
    app._write([{"id": "video-check", "status": "finalizada"}])

    app._save_audio_check("video-check", tmp_path / "capture.mkv")

    row = app._get("video-check")
    assert row["has_audio"] is True
    assert row["has_video"] is False
    assert row["warning"] == "La grabaci\u00f3n no tiene pista de video. Revisa formats.txt y ffmpeg.log."
