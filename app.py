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
PAUSING: set[str] = set()
CONTROL_LOCKS: dict[str, threading.RLock] = {}
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


def _control_lock(rec_id: str) -> threading.RLock:
    with LOCK:
        return CONTROL_LOCKS.setdefault(rec_id, threading.RLock())


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

    unknown = [f for f in available
               if (f.get("vcodec") is None or f.get("acodec") is None)
               and f.get("vcodec") != "none"
               and (f.get("height") or f.get("width") or f.get("vcodec"))]
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
            args += ["-map", "0:v:0", "-map", "0:a:0?"]
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


def _has_video(path: str | Path) -> bool | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v",
                                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                                capture_output=True, text=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def _save_audio_check(rec_id: str, file_path: str | Path) -> bool | None:
    has_audio = _has_audio(file_path)
    audio_warning = "La grabaci\u00f3n no tiene pista de audio. Revisa ffmpeg.log y formats.txt."
    video_warning = "La grabaci\u00f3n no tiene pista de video. Revisa formats.txt y ffmpeg.log."
    has_video = _has_video(file_path)
    row = _get(rec_id)
    warnings = str(row.get("warning") or "")
    for result, flag, warning in ((has_audio, "has_audio", audio_warning),
                                  (has_video, "has_video", video_warning)):
        if result is False:
            if warning not in warnings:
                warnings = " ".join(filter(None, (warnings, warning)))
            row[flag] = False
        elif result is True:
            warnings = warnings.replace(warning, "").strip()
            row[flag] = True
    _update(rec_id, **{key: row[key] for key in ("has_audio", "has_video") if key in row},
            warning=warnings or None)
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
    with LOCK:
        rows = _read()
        row = next((item for item in rows if item["id"] == rec_id), None)
    if row and row.get("status") in {"grabando", "pausada", "reconectando"}:
        segment_name = row.get("current_segment") or (row.get("segments") or [None])[-1]
        segment_path = Path(row["folder"]) / segment_name if segment_name else Path(file_path)
        _save_audio_check(rec_id, segment_path)


def _watch_progress(rec_id: str, proc: subprocess.Popen) -> None:
    """Track one segment, then reconnect or finalize when its FFmpeg exits."""
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

    with _control_lock(rec_id):
        with LOCK:
            stopping = rec_id in STOPPING
            pausing = rec_id in PAUSING
            rows = _read()
            row = next((item for item in rows if item.get("id") == rec_id), None)
            if PROCESSES.get(rec_id) is not proc:
                return
            if (stopping or pausing or row is None or row.get("status") != "grabando") \
                    and PROCESSES.get(rec_id) is proc:
                PROCESSES.pop(rec_id, None)
        if stopping or pausing or row is None or row.get("status") != "grabando":
            return

        now = time.time()
        started = row.get("capture_started_at")
        elapsed = float(row.get("elapsed_seconds", 0))
        if started is not None:
            elapsed = max(elapsed, float(row.get("capture_base_seconds", elapsed)
                                          + max(0, now - started)))
        _update(rec_id, elapsed_seconds=elapsed, capture_started_at=None,
                capture_base_seconds=elapsed, current_segment=None, status="reconectando")
        last_error = _ffmpeg_error_tail(row.get("folder", ""))
        normal_end = False
        for attempt in range(1, 4):
            with LOCK:
                if rec_id in STOPPING or rec_id in PAUSING:
                    return
            _update(rec_id, reconnect_attempts=attempt)
            try:
                resolved = _resolve(row["url"])
                with LOCK:
                    if rec_id in STOPPING or rec_id in PAUSING:
                        return
                _start_segment(rec_id, resolved)
                current = _get(rec_id)
                _update(rec_id, reconnections=int(current.get("reconnections", 0)) + 1,
                        reconnect_attempts=0)
                return
            except Exception as exc:
                last_error = str(getattr(exc, "detail", exc))[:400]
                if "no corresponde a un live activo" in last_error.lower():
                    normal_end = True
                    break
                with LOCK:
                    if rec_id in STOPPING or rec_id in PAUSING:
                        return
                if attempt < 3:
                    time.sleep(attempt)
        with LOCK:
            if rec_id in STOPPING or rec_id in PAUSING:
                return
        current = _get(rec_id)
        if current.get("segments"):
            warning = None if normal_end else f"Se agotaron los 3 intentos de reconexion automatica. {last_error[:250]}"
            _finish_recording(rec_id, warning=warning)
        else:
            _update(rec_id, status="error", ended_at=time.time(), capture_started_at=None,
                    error=last_error[-400:] or _ffmpeg_error_tail(row.get("folder", "")))
        if PROCESSES.get(rec_id) is proc:
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
            if not info.get("url") or info.get("vcodec") == "none":
                raise HTTPException(
                    400,
                    "YouTube no ofreci\u00f3 un stream de video accesible (\u00bffalta runtime de JS o token?). "
                    "Actualiza yt-dlp con pip install -U yt-dlp e instala un runtime de JavaScript (Deno o Node).",
                )
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


def _append_format_debug(folder: str | Path, segment: str, resolved: dict) -> None:
    target = Path(folder) / "formats.txt"
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    entries = existing.get("segments", []) if isinstance(existing, dict) else []
    entries.append({"segment": segment, "kind": resolved["kind"],
                    "formats": resolved.get("debug", [])})
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"segments": entries}, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    os.replace(temporary, target)


def _concat_segments(row: dict) -> bool:
    names = row.get("segments") or []
    final_file = Path(row["file"])
    if not names:
        return final_file.is_file() and final_file.stat().st_size > 1024
    folder = Path(row["folder"]).resolve()
    parts = []
    for name in names:
        if not isinstance(name, str) or Path(name).name != name:
            continue
        part = (folder / name).resolve()
        if part.parent == folder and part.is_file() and part.stat().st_size > 1024:
            parts.append(part)
    if not parts:
        return final_file.is_file() and final_file.stat().st_size > 1024
    if len(parts) == 1:
        if parts[0] != final_file:
            final_file.unlink(missing_ok=True)
            os.replace(parts[0], final_file)
        return final_file.is_file() and final_file.stat().st_size > 1024
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    concat_list = folder / "list.txt"
    temporary = final_file.with_name(f"{final_file.stem}.joined.mkv")
    def quote(path: Path) -> str:
        return path.as_posix().replace("'", "'\\''")
    concat_list.write_text("".join(f"file '{quote(part)}'\n" for part in parts), encoding="utf-8")
    try:
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                        "-safe", "0", "-i", str(concat_list), "-c", "copy", str(temporary)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if temporary.is_file() and temporary.stat().st_size > 1024:
            os.replace(temporary, final_file)
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    temporary.unlink(missing_ok=True)
    return False


def _start_segment(rec_id: str, resolved: dict) -> dict:
    row = _get(rec_id)
    folder = Path(row["folder"])
    segments = list(row.get("segments") or [])
    number = len(segments) + 1
    while (folder / f"part{number:02d}.mkv").exists():
        number += 1
    segment_name = f"part{number:02d}.mkv"
    segment_path = folder / segment_name
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("No se encontró FFmpeg.")
    _append_format_debug(folder, segment_name, resolved)
    args = _build_ffmpeg_args(resolved, segment_path)
    args[0] = ffmpeg
    with (folder / "ffmpeg.log").open("ab") as log_file:
        log_file.write(f"\n=== {segment_name} ===\n".encode("ascii"))
        log_file.flush()
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log_file,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    segments.append(segment_name)
    try:
        updated = _update(rec_id, segments=segments, current_segment=segment_name, pid=proc.pid,
                          status="grabando", capture_started_at=None,
                          capture_base_seconds=float(row.get("elapsed_seconds", 0)),
                          format_kind=resolved["kind"])
        PROCESSES[rec_id] = proc
    except Exception:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        raise
    threading.Thread(target=_watch_progress, args=(rec_id, proc), daemon=True).start()
    return updated


def _finish_recording(rec_id: str, *, warning: str | None = None,
                      error: str | None = None) -> dict:
    row = _get(rec_id)
    merged = _concat_segments(row)
    if not merged:
        return _update(rec_id, status="error", ended_at=time.time(), capture_started_at=None,
                       error=error or "No se pudieron unir las partes de la grabación. Revisa ffmpeg.log.")
    changes = {"status": "finalizada", "ended_at": time.time(), "capture_started_at": None,
               "current_segment": None}
    if warning:
        changes["warning"] = warning
    updated = _update(rec_id, **changes)
    _save_audio_check(rec_id, updated["file"])
    updated = _get(rec_id)
    cover_path = _make_cover(updated)
    if cover_path:
        updated = _update(rec_id, cover_file=cover_path)
    return updated


class StartRequest(BaseModel):
    url: str


@app.get("/api/recordings")
def recordings():
    completed = []
    with LOCK:
        rows = _read()
        before = json.dumps(rows, ensure_ascii=False, sort_keys=True)
        for row in rows:
            if row.get("status") not in {"grabando", "reconectando", "deteniendo"} \
                    or row["id"] in PROCESSES:
                continue
            try:
                alive = psutil.Process(int(row["pid"])).is_running()
            except (psutil.Error, KeyError, ValueError):
                alive = False
            if not alive:
                folder = Path(row.get("folder", ""))
                has_parts = any((folder / name).is_file() and (folder / name).stat().st_size > 1024
                                for name in row.get("segments", []) if Path(name).name == name)
                final_file = Path(row.get("file", ""))
                has_final = final_file.is_file() and final_file.stat().st_size > 1024
                if has_parts or has_final:
                    row.update(status="deteniendo", ended_at=row.get("ended_at") or time.time())
                    completed.append(row["id"])
                else:
                    row.update(status="error", ended_at=row.get("ended_at") or time.time(),
                               capture_started_at=None, error=_ffmpeg_error_tail(row.get("folder", "")))
        if json.dumps(rows, ensure_ascii=False, sort_keys=True) != before:
            _write(rows)
    for rec_id in completed:
        _finish_recording(rec_id, warning="La aplicacion se reinicio mientras grababa; se guardo el material disponible.")
    with LOCK:
        visible = sorted(_read(), key=lambda x: x.get("created_at", 0), reverse=True)
    return {"recordings": [_clean(row) for row in visible]}


@app.post("/api/recordings", status_code=202)
def start(body: StartRequest):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(500, "No se encontro FFmpeg. Instalalo y vuelve a iniciar MyRecorder.")
    resolved = _resolve(body.url.strip())
    rec_id = str(uuid.uuid4())
    title = re.sub(r"[^\w .()-]+", "", resolved["title"]).strip()[:90].rstrip(" .") or "Live"
    folder = OUTPUT / f"{time.strftime('%Y%m%d-%H%M%S')}_{title}"
    folder.mkdir(parents=True, exist_ok=False)
    target = folder / f"{title}.mkv"
    row = {"id": rec_id, "title": resolved["title"], "url": body.url.strip(),
           "file": str(target), "folder": str(folder), "status": "grabando",
           "created_at": time.time(), "capture_started_at": None,
           "capture_base_seconds": 0, "elapsed_seconds": 0, "segments": [],
           "reconnections": 0, "reconnect_attempts": 0}
    try:
        with LOCK:
            rows = _read()
            rows.append(row)
            _write(rows)
        with _control_lock(rec_id):
            current = _start_segment(rec_id, resolved)
        threading.Thread(target=_verify_audio_after_start,
                         args=(rec_id, current["file"]), daemon=True).start()
        return _clean(_get(rec_id))
    except Exception as exc:
        proc = PROCESSES.pop(rec_id, None)
        if isinstance(proc, subprocess.Popen):
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                try:
                    proc.kill()
                except OSError:
                    pass
        with LOCK:
            _write([item for item in _read() if item.get("id") != rec_id])
        shutil.rmtree(folder, ignore_errors=True)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(500, f"No se pudo iniciar la grabacion: {str(exc)[:200]}") from exc


@app.post("/api/recordings/{rec_id}/pause")
def pause(rec_id: str):
    with LOCK:
        PAUSING.add(rec_id)
    try:
        with _control_lock(rec_id):
            row = _get(rec_id)
            if row.get("status") not in {"grabando", "reconectando"}:
                raise HTTPException(409, "La grabacion no esta activa.")
            folder = Path(row["folder"])
            segment = (folder / row["current_segment"] if row.get("current_segment") else
                       (Path(row["file"]) if not row.get("segments") else None))
            proc = PROCESSES.get(rec_id)
            forced = False
            if isinstance(proc, subprocess.Popen):
                try:
                    if proc.poll() is None:
                        if proc.stdin is None:
                            raise OSError("FFmpeg no acepta el comando de cierre")
                        proc.stdin.write(b"q\n")
                        proc.stdin.flush()
                        proc.wait(timeout=12)
                except (OSError, subprocess.TimeoutExpired):
                    forced = True
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except (OSError, subprocess.SubprocessError):
                        pass
            elif row.get("pid") and (row.get("current_segment") or not row.get("segments")):
                try:
                    orphan = psutil.Process(int(row["pid"]))
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
                    forced = bool(segment)
                except psutil.Error:
                    forced = True
            if forced and segment:
                _repair_mkv(segment)
            segments = list(row.get("segments") or [])
            if not segments:
                old_file = Path(row["file"])
                first_part = folder / "part01.mkv"
                if old_file.is_file() and old_file != first_part:
                    os.replace(old_file, first_part)
                segments = [first_part.name]
            now = time.time()
            started = row.get("capture_started_at")
            base = float(row.get("capture_base_seconds", row.get("elapsed_seconds", 0)))
            elapsed = max(float(row.get("elapsed_seconds", 0)),
                          base + (max(0, now - started) if started else 0))
            updated = _update(rec_id, status="pausada", paused_at=now, elapsed_seconds=elapsed,
                              capture_base_seconds=elapsed, capture_started_at=None,
                              segments=segments, current_segment=None)
            if PROCESSES.get(rec_id) is proc:
                PROCESSES.pop(rec_id, None)
            return _clean(updated)
    finally:
        with LOCK:
            PAUSING.discard(rec_id)


@app.post("/api/recordings/{rec_id}/resume")
def resume(rec_id: str):
    with _control_lock(rec_id):
        row = _get(rec_id)
        if row.get("status") != "pausada":
            raise HTTPException(409, "La grabacion no esta pausada.")
        with LOCK:
            if rec_id in STOPPING:
                raise HTTPException(409, "La grabacion se esta deteniendo.")
        resolved = _resolve(row["url"])
        return _clean(_start_segment(rec_id, resolved))


@app.post("/api/recordings/{rec_id}/stop")
def stop(rec_id: str):
    with LOCK:
        STOPPING.add(rec_id)
    try:
        with _control_lock(rec_id):
            row = _get(rec_id)
            if row.get("status") in {"finalizada", "error"}:
                return _clean(row)
            proc = PROCESSES.get(rec_id)
            forced = False
            if isinstance(proc, subprocess.Popen):
                try:
                    if proc.poll() is None:
                        if proc.stdin is None:
                            raise OSError("FFmpeg no acepta el comando de cierre")
                        proc.stdin.write(b"q\n")
                        proc.stdin.flush()
                        proc.wait(timeout=12)
                except (OSError, subprocess.TimeoutExpired):
                    forced = True
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except (OSError, subprocess.SubprocessError):
                        pass
            elif row.get("pid") and (row.get("current_segment") or not row.get("segments")):
                try:
                    orphan = psutil.Process(int(row["pid"]))
                    if row.get("status") == "pausada":
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
                    forced = bool(row.get("current_segment") or not row.get("segments"))
                except psutil.Error:
                    forced = True
            segment_path = None
            if row.get("current_segment"):
                segment_path = Path(row["folder"]) / row["current_segment"]
            elif not row.get("segments"):
                segment_path = Path(row["file"])
            if forced and segment_path:
                _repair_mkv(segment_path)
            now = time.time()
            started = row.get("capture_started_at")
            base = float(row.get("capture_base_seconds", row.get("elapsed_seconds", 0)))
            elapsed = max(float(row.get("elapsed_seconds", 0)),
                          base + (max(0, now - started) if started else 0))
            _update(rec_id, status="deteniendo", elapsed_seconds=elapsed,
                    capture_started_at=None, current_segment=None)
            if PROCESSES.get(rec_id) is proc:
                PROCESSES.pop(rec_id, None)
            updated = _finish_recording(rec_id)
            return _clean(updated)
    finally:
        with LOCK:
            STOPPING.discard(rec_id)


@app.post("/api/recordings/{rec_id}/folder")
def open_folder(rec_id: str):
    row = _get(rec_id)
    try:
        if sys.platform == "win32":
            os.startfile(row["folder"])
        elif sys.platform == "darwin":
            subprocess.run(["open", row["folder"]], check=False)
        else:
            subprocess.run(["xdg-open", row["folder"]], check=False)
    except OSError as exc:
        raise HTTPException(500, f"No se pudo abrir la carpeta: {exc}") from exc
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
