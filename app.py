from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import psutil
import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
OUTPUT = Path(os.environ.get("MYRECORDER_OUTPUT", ROOT / "recordings")).expanduser().resolve()
STATE = OUTPUT / "recordings.json"
OUTPUT.mkdir(parents=True, exist_ok=True)
LOCK = threading.RLock()
PROCESSES: dict[str, subprocess.Popen] = {}
STOPPING: set[str] = set()
app = FastAPI(title="MyRecorder")


def _read() -> list[dict]:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write(rows: list[dict]) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE)


def _clean(row: dict) -> dict:
    result = {k: v for k, v in row.items() if k not in {"pid", "media_url", "cover_file"}}
    if row.get("cover_file") and Path(row["cover_file"]).is_file():
        result["cover_url"] = f"/api/recordings/{row['id']}/cover"
    return result


def _headers_arg(headers: dict) -> list[str]:
    if not headers:
        return []
    value = "".join(f"{key}: {item}\r\n" for key, item in headers.items())
    return ["-headers", value]


def _format_debug(fmt: dict) -> dict:
    """Keep useful format metadata while deliberately excluding expiring URLs."""
    return {key: fmt.get(key) for key in
            ("format_id", "protocol", "vcodec", "acodec", "height", "ext")}


def _pick_formats(formats: list[dict]) -> dict:
    available = [f for f in formats if isinstance(f, dict) and f.get("url")]
    muxed = [f for f in available if f.get("vcodec") not in (None, "none")
             and f.get("acodec") not in (None, "none")]
    if muxed:
        chosen = max(muxed, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        return {"kind": "muxed", "media_url": chosen["url"], "audio_url": None,
                "headers": chosen.get("http_headers") or {}, "audio_headers": {},
                "debug": [_format_debug(chosen)]}

    videos = [f for f in available if f.get("vcodec") not in (None, "none")
              and f.get("acodec") == "none"]
    audios = [f for f in available if f.get("acodec") not in (None, "none")
              and f.get("vcodec") in (None, "none")]
    if videos and audios:
        video = max(videos, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        audio = max(audios, key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0))
        return {"kind": "pair", "media_url": video["url"], "audio_url": audio["url"],
                "headers": video.get("http_headers") or {},
                "audio_headers": audio.get("http_headers") or {},
                "debug": [_format_debug(video), _format_debug(audio)]}

    unknown = [f for f in available if f.get("vcodec") is None or f.get("acodec") is None]
    if unknown:
        chosen = max(unknown, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        return {"kind": "unknown", "media_url": chosen["url"], "audio_url": None,
                "headers": chosen.get("http_headers") or {}, "audio_headers": {},
                "debug": [_format_debug(chosen)]}
    raise ValueError("YouTube no ofreció formatos de video compatibles.")


def _build_ffmpeg_args(resolved: dict, target: str | Path) -> list[str]:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-stats_period", "0.5",
            "-progress", "pipe:1"]
    reconnect = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    if resolved["kind"] == "pair":
        args += ["-thread_queue_size", "1024", *reconnect,
                 *_headers_arg(resolved.get("headers") or {}), "-i", resolved["media_url"],
                 "-thread_queue_size", "1024", *reconnect,
                 *_headers_arg(resolved.get("audio_headers") or {}), "-i", resolved["audio_url"],
                 "-map", "0:v:0", "-map", "1:a:0"]
    else:
        args += [*reconnect, *_headers_arg(resolved.get("headers") or {}), "-i", resolved["media_url"]]
        if resolved["kind"] == "muxed":
            args += ["-map", "0:v:0", "-map", "0:a:0"]
        else:
            args += ["-map", "0:v:0?", "-map", "0:a:0?"]
    args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
             "-af", "aresample=async=1:first_pts=0", "-f", "matroska", str(target)]
    return args


def _has_audio(path: str | Path) -> bool | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a",
                                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
                                capture_output=True, text=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def _save_audio_check(rec_id: str, file_path: str | Path) -> bool | None:
    has_audio = _has_audio(file_path)
    if has_audio is False:
        _update(rec_id, has_audio=False,
                warning="La grabación no tiene pista de audio. Revisa ffmpeg.log y formats.txt.")
    elif has_audio is True:
        _update(rec_id, has_audio=True)
    return has_audio


def _ffmpeg_error_tail(folder: str | Path) -> str:
    log_path = Path(folder) / "ffmpeg.log"
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
    except OSError:
        lines = []
    message = "\n".join(lines).strip() or "FFmpeg terminó inesperadamente sin dejar detalles en el registro."
    return message[-400:]


def _verify_audio_after_start(rec_id: str, file_path: str | Path) -> None:
    while True:
        with LOCK:
            rows = _read()
            row = next((item for item in rows if item["id"] == rec_id), None)
            if not row or row.get("status") not in {"grabando", "pausada"}:
                return
            started_at = row.get("capture_started_at")
        if started_at is not None:
            break
        time.sleep(0.5)
    delay = max(0, float(started_at) + 20 - time.time())
    if delay:
        time.sleep(delay)
    _save_audio_check(rec_id, file_path)


def _watch_progress(rec_id: str, proc: subprocess.Popen) -> None:
    """Track capture time and settle recording state when FFmpeg exits."""
    if proc.stdout is None:
        return
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("out_time_us="):
                continue
            try:
                media_time = int(line.partition("=")[2]) / 1_000_000
            except ValueError:
                continue
            if media_time <= 0:
                continue
            now = time.time()
            with LOCK:
                rows = _read()
                row = next((item for item in rows if item["id"] == rec_id), None)
                if not row or row.get("status") != "grabando":
                    continue
                if row.get("capture_started_at") is None:
                    row["capture_started_at"] = now
                    row["capture_base_seconds"] = float(row.get("elapsed_seconds", 0))
                row["elapsed_seconds"] = max(float(row.get("elapsed_seconds", 0)), float(
                    row.get("capture_base_seconds", 0) + max(0, now - row["capture_started_at"])))
                _write(rows)
    except OSError:
        pass
    try:
        return_code = proc.wait()
    except (OSError, subprocess.SubprocessError):
        return_code = proc.poll() if proc.poll() is not None else -1
    with LOCK:
        user_stopped = rec_id in STOPPING
        if user_stopped:
            STOPPING.discard(rec_id)
        rows = _read()
        row = next((item for item in rows if item.get("id") == rec_id), None)
    if user_stopped or row is None or row.get("status") not in {"grabando", "pausada"}:
        return
    media_file = Path(row.get("file", ""))
    valid_file = media_file.is_file() and media_file.stat().st_size > 1024
    if return_code == 0 or valid_file:
        _update(rec_id, status="finalizada", ended_at=time.time(), capture_started_at=None)
        _save_audio_check(rec_id, media_file)
        updated = _get(rec_id)
        cover_path = _make_cover(updated)
        if cover_path:
            _update(rec_id, cover_file=cover_path)
    else:
        _update(rec_id, status="error", ended_at=time.time(), capture_started_at=None,
                error=_ffmpeg_error_tail(row.get("folder", "")))
    PROCESSES.pop(rec_id, None)


def _make_cover(row: dict) -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    source = Path(row.get("file", ""))
    if not ffmpeg or not source.is_file() or source.stat().st_size < 1024:
        return None
    cover = source.parent / "cover.jpg"
    for seek in (["-sseof", "-10"], ["-ss", "1"], []):
        try:
            cover.unlink(missing_ok=True)
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *seek,
                            "-i", str(source), "-frames:v", "1", "-q:v", "3", str(cover)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if cover.is_file() and cover.stat().st_size > 0:
                return str(cover)
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _validate_url(url: str) -> None:
    parts = urlparse(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in {"http", "https"} or not (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")):
        raise HTTPException(400, "Introduce un enlace válido de YouTube.")


def _resolve(url: str) -> dict:
    _validate_url(url)
    try:
        # Do not constrain formats at extraction time: some YouTube live
        # manifests expose only formats outside yt-dlp's generic `best` filter.
        # Inspect the returned formats below and choose a compatible stream.
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info.get("is_live") and info.get("live_status") != "is_live":
            raise HTTPException(400, "El enlace no corresponde a un live activo.")
        try:
            selected = _pick_formats(info.get("formats", []))
        except ValueError:
            if not info.get("url"):
                raise
            selected = {"kind": "unknown", "media_url": info["url"], "audio_url": None,
                        "headers": info.get("http_headers") or {}, "audio_headers": {},
                        "debug": [{"format_id": info.get("format_id"), "protocol": info.get("protocol"),
                                   "vcodec": info.get("vcodec"), "acodec": info.get("acodec"),
                                   "height": info.get("height"), "ext": info.get("ext")}]}
        selected["headers"] = selected.get("headers") or info.get("http_headers") or {}
        selected["audio_headers"] = selected.get("audio_headers") or info.get("http_headers") or {}
        return {"title": info.get("title") or "Live de YouTube", **selected}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"No se pudo abrir el live: {str(exc)[:220]}") from exc


def _update(rec_id: str, **changes) -> dict:
    with LOCK:
        rows = _read()
        for row in rows:
            if row["id"] == rec_id:
                row.update(changes)
                _write(rows)
                return row
    raise HTTPException(404, "Grabación no encontrada.")



def _get(rec_id: str) -> dict:
    with LOCK:
        row = next((item for item in _read() if item.get("id") == rec_id), None)
    if row is None:
        raise HTTPException(404, "Grabaci\u00f3n no encontrada.")
    return row


def _resume_pid(pid: int | str) -> bool:
    try:
        psutil.Process(int(pid)).resume()
        return True
    except psutil.Error:
        return False


def _repair_mkv(file_path: str | Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    source = Path(file_path)
    if not ffmpeg or not source.is_file():
        return False
    fixed = source.with_name(f"{source.stem}.fixed.mkv")
    try:
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                        "-c", "copy", str(fixed)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=120, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if fixed.is_file() and fixed.stat().st_size > 1024:
            os.replace(fixed, source)
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    fixed.unlink(missing_ok=True)
    return False


class StartRequest(BaseModel):
    url: str


@app.get("/api/recordings")
def recordings():
    completed = []
    with LOCK:
        rows = _read()
        before = json.dumps(rows, ensure_ascii=False, sort_keys=True)
        for row in rows:
            if row.get("status") != "grabando" or row["id"] in PROCESSES:
                continue
            try:
                alive = psutil.Process(int(row["pid"])).is_running()
            except (psutil.Error, KeyError, ValueError):
                alive = False
            if not alive:
                media_file = Path(row.get("file", ""))
                if media_file.is_file() and media_file.stat().st_size > 1024:
                    row.update(status="finalizada", ended_at=row.get("ended_at") or time.time(),
                               capture_started_at=None)
                    completed.append((row["id"], True))
                else:
                    row.update(status="error", ended_at=row.get("ended_at") or time.time(),
                               capture_started_at=None, error=_ffmpeg_error_tail(row.get("folder", "")))
                    completed.append((row["id"], False))
        if json.dumps(rows, ensure_ascii=False, sort_keys=True) != before:
            _write(rows)
    for rec_id, valid in completed:
        if not valid:
            continue
        row = _get(rec_id)
        _save_audio_check(rec_id, row["file"])
        row = _get(rec_id)
        cover_path = _make_cover(row)
        if cover_path:
            _update(rec_id, cover_file=cover_path)
    with LOCK:
        visible = sorted(_read(), key=lambda x: x.get("created_at", 0), reverse=True)
    return {"recordings": [_clean(row) for row in visible]}


@app.post("/api/recordings", status_code=202)
def start(body: StartRequest):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(500, "No se encontr\u00f3 FFmpeg. Inst\u00e1lalo y vuelve a iniciar MyRecorder.")
    resolved = _resolve(body.url.strip())
    rec_id = str(uuid.uuid4())
    title = re.sub(r"[^\w .()-]+", "", resolved["title"]).strip()[:90].rstrip(" .") or "Live"
    folder = OUTPUT / f"{time.strftime('%Y%m%d-%H%M%S')}_{title}"
    folder.mkdir(parents=True, exist_ok=False)
    target = folder / f"{title}.mkv"
    proc = None
    try:
        debug_info = {"kind": resolved["kind"], "formats": resolved.get("debug", [])}
        (folder / "formats.txt").write_text(json.dumps(debug_info, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
        args = _build_ffmpeg_args(resolved, target)
        args[0] = ffmpeg
        with (folder / "ffmpeg.log").open("wb") as log_file:
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log_file,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        started_at = time.time()
        row = {"id": rec_id, "title": resolved["title"], "url": body.url.strip(),
               "file": str(target), "folder": str(folder), "status": "grabando",
               "created_at": started_at, "capture_started_at": None,
               "capture_base_seconds": 0, "elapsed_seconds": 0, "pid": proc.pid,
               "format_kind": resolved["kind"]}
        with LOCK:
            rows = _read()
            rows.append(row)
            _write(rows)
        PROCESSES[rec_id] = proc
        threading.Thread(target=_watch_progress, args=(rec_id, proc), daemon=True).start()
        threading.Thread(target=_verify_audio_after_start, args=(rec_id, target), daemon=True).start()
        return _clean(row)
    except Exception as exc:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                try:
                    proc.kill()
                except OSError:
                    pass
        shutil.rmtree(folder, ignore_errors=True)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(500, f"No se pudo iniciar la grabaci\u00f3n: {str(exc)[:200]}") from exc


@app.post("/api/recordings/{rec_id}/pause")
def pause(rec_id: str):
    row = _get(rec_id)
    if row["status"] != "grabando":
        raise HTTPException(409, "La grabaci\u00f3n no est\u00e1 activa.")
    try:
        psutil.Process(int(row["pid"])).suspend()
    except psutil.Error as exc:
        raise HTTPException(409, f"No se pudo pausar: {exc}") from exc
    now = time.time()
    base = float(row.get("capture_base_seconds", row.get("elapsed_seconds", 0)))
    started = row.get("capture_started_at")
    elapsed = max(float(row.get("elapsed_seconds", 0)), base + (max(0, now - started) if started else 0))
    return _clean(_update(rec_id, status="pausada", paused_at=now, elapsed_seconds=elapsed,
                          capture_base_seconds=elapsed, capture_started_at=None))


@app.post("/api/recordings/{rec_id}/resume")
def resume(rec_id: str):
    row = _get(rec_id)
    if row["status"] != "pausada":
        raise HTTPException(409, "La grabaci\u00f3n no est\u00e1 pausada.")
    try:
        psutil.Process(int(row["pid"])).resume()
    except psutil.Error as exc:
        raise HTTPException(409, f"No se pudo reanudar: {exc}") from exc
    return _clean(_update(rec_id, status="grabando", paused_at=None, capture_started_at=None,
                          capture_base_seconds=float(row.get("elapsed_seconds", 0))))


@app.post("/api/recordings/{rec_id}/stop")
def stop(rec_id: str):
    row = _get(rec_id)
    elapsed = float(row.get("elapsed_seconds", 0))
    if row["status"] == "grabando" and row.get("capture_started_at"):
        elapsed = max(elapsed, float(row.get("capture_base_seconds", elapsed)
                                      + max(0, time.time() - row["capture_started_at"])))
    pid = int(row.get("pid", 0))
    forced = False
    with LOCK:
        STOPPING.add(rec_id)
    proc = PROCESSES.get(rec_id)
    try:
        if isinstance(proc, subprocess.Popen):
            if row["status"] == "pausada":
                _resume_pid(pid)
            try:
                if proc.stdin is None:
                    raise OSError("No se puede enviar el comando de cierre a FFmpeg")
                proc.stdin.write(b"q\n")
                proc.stdin.flush()
                proc.wait(timeout=12)
            except (OSError, subprocess.TimeoutExpired):
                forced = True
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        else:
            try:
                orphan = psutil.Process(pid)
                if row["status"] == "pausada":
                    orphan.resume()
                orphan.terminate()
                try:
                    orphan.wait(timeout=5)
                except psutil.TimeoutExpired:
                    forced = True
                    orphan.kill()
                    try:
                        orphan.wait(timeout=5)
                    except psutil.TimeoutExpired:
                        pass
                forced = True
            except psutil.NoSuchProcess:
                pass
            except psutil.Error:
                forced = True
                try:
                    psutil.Process(pid).kill()
                except psutil.NoSuchProcess:
                    pass
                except psutil.Error:
                    pass
        if forced:
            _repair_mkv(row["file"])
    finally:
        PROCESSES.pop(rec_id, None)
        if not isinstance(proc, subprocess.Popen):
            with LOCK:
                STOPPING.discard(rec_id)
    updated = _update(rec_id, status="finalizada", ended_at=time.time(), elapsed_seconds=elapsed,
                      capture_started_at=None)
    _save_audio_check(rec_id, updated["file"])
    updated = _get(rec_id)
    cover = _make_cover(updated)
    if cover:
        updated = _update(rec_id, cover_file=cover)
    if isinstance(proc, subprocess.Popen):
        # The watcher removes this marker after it consumes FFmpeg's EOF.
        for _ in range(20):
            with LOCK:
                if rec_id not in STOPPING:
                    break
            time.sleep(0.05)
        with LOCK:
            STOPPING.discard(rec_id)
    return _clean(updated)


@app.post("/api/recordings/{rec_id}/folder")
def open_folder(rec_id: str):
    row = _get(rec_id)
    os.startfile(row["folder"])
    return {"ok": True}


@app.delete("/api/recordings/{rec_id}")
def delete_recording(rec_id: str):
    with LOCK:
        rows = _read()
        row = next((item for item in rows if item["id"] == rec_id), None)
        if not row:
            raise HTTPException(404, "Grabación no encontrada.")
        if row.get("status") not in {"finalizada", "error"}:
            raise HTTPException(409, "Detén la grabación antes de eliminarla.")
        folder = Path(row.get("folder", "")).resolve()
        if folder == OUTPUT or not folder.is_relative_to(OUTPUT):
            raise HTTPException(400, "La carpeta de grabación no está dentro de la ubicación de MyRecorder.")
        if folder.exists():
            shutil.rmtree(folder)
        _write([item for item in rows if item["id"] != rec_id])
    return {"ok": True}


@app.get("/api/recordings/{rec_id}/file")
def media(rec_id: str):
    row = next((r for r in _read() if r["id"] == rec_id), None)
    if not row or not Path(row["file"]).is_file(): raise HTTPException(404, "Archivo aún no disponible.")
    return FileResponse(row["file"], media_type="video/x-matroska", filename=Path(row["file"]).name)


@app.get("/api/recordings/{rec_id}/cover")
def cover(rec_id: str):
    row = next((r for r in _read() if r["id"] == rec_id), None)
    if not row or not row.get("cover_file") or not Path(row["cover_file"]).is_file():
        raise HTTPException(404, "Portada no disponible.")
    return FileResponse(row["cover_file"], media_type="image/jpeg")


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
