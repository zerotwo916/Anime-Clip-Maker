import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import zipfile
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urlparse

import requests
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
GOFILE_API = "https://api.gofile.io"

app = FastAPI(title="Anime Clip Maker")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def run_command(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="FFmpeg/FFprobe is missing on the server.")
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        command = " ".join(str(part) for part in args)
        detail = stderr or stdout or "The media tool exited without returning an error message."
        detail = (
            f"Command: {command}\n"
            f"Exit code: {exc.returncode}\n"
            f"Output: {detail}"
        )[-6000:]
        raise HTTPException(status_code=422, detail=detail)


async def save_upload(upload: UploadFile, destination: Path) -> int:
    size = 0
    with destination.open("wb") as output:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="File is larger than the 2 GB app limit.")
            output.write(chunk)
    return size


def gofile_share_code(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    if parsed.scheme != "https" or parsed.hostname not in {"gofile.io", "www.gofile.io"}:
        raise HTTPException(status_code=400, detail="Please enter a public https://gofile.io/d/ share link.")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "d" or not parts[1].isalnum():
        raise HTTPException(status_code=400, detail="Invalid Gofile share link. Expected https://gofile.io/d/SHARE_CODE.")
    return parts[1]


def download_gofile_video(share_url: str, destination: Path) -> str:
    code = gofile_share_code(share_url)
    session = requests.Session()
    session.headers.update({"User-Agent": "AnimeClipMaker/1.0"})

    try:
        # Current Gofile API creates guest accounts with POST /accounts.
        account_response = session.post(
            f"{GOFILE_API}/accounts",
            json={},
            timeout=(10, 30),
        )
        account_response.raise_for_status()
        account_payload = account_response.json()
        if account_payload.get("status") != "ok":
            raise ValueError(f"Gofile account creation failed: {account_payload.get('status', 'unknown error')}")
        token = account_payload.get("data", {}).get("token")
        if not token:
            raise ValueError("Gofile did not return an account token.")

        # The current API resolves public share codes through /contents/{contentId}.
        content_response = session.get(
            f"{GOFILE_API}/contents/{code}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=(10, 45),
        )
        content_response.raise_for_status()
        payload = content_response.json()
        if payload.get("status") != "ok":
            status = payload.get("status", "unknown error")
            if status == "error-notPremium":
                raise ValueError(
                    "Gofile's current API restricts direct folder listings to Premium accounts. "
                    "This public share cannot be listed through the free API."
                )
            raise ValueError(f"Gofile could not open this share: {status}")

        data = payload.get("data", {})
        items = []
        if data.get("type") == "file":
            items.append(data)
        else:
            children = data.get("children", {})
            if isinstance(children, dict):
                items.extend(children.values())
            elif isinstance(children, list):
                items.extend(children)

        videos = []
        for item in items:
            if item.get("type") != "file":
                continue
            name = Path(item.get("name") or "video").name
            if Path(name).suffix.lower() in {".mp4", ".mkv", ".mov", ".avi", ".webm"}:
                link = item.get("directLink") or item.get("link")
                if link:
                    videos.append((name, link))

        if not videos:
            raise ValueError("No supported video file was found in this Gofile share.")
        if len(videos) > 1:
            names = ", ".join(name for name, _ in videos[:8])
            raise ValueError(
                f"More than one video was found ({names}). "
                "Please use a Gofile share containing only one video."
            )

        filename, download_url = videos[0]
        parsed_download = urlparse(download_url)
        host = parsed_download.hostname or ""
        if parsed_download.scheme != "https" or not (
            host == "gofile.io" or host.endswith(".gofile.io")
        ):
            raise ValueError("Gofile returned an unexpected download address.")

        total = 0
        with session.get(
            download_url, stream=True, timeout=(15, 90), allow_redirects=True
        ) as response:
            response.raise_for_status()
            final_host = urlparse(response.url).hostname or ""
            if urlparse(response.url).scheme != "https" or not (
                final_host == "gofile.io" or final_host.endswith(".gofile.io")
            ):
                raise ValueError("Gofile redirected to an unexpected download address.")
            with destination.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise ValueError("Gofile video exceeds the app's 2 GB limit.")
                    output.write(chunk)
        if total == 0:
            raise ValueError("Gofile returned an empty file.")
        return filename
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not download the Gofile video: {exc}")
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def mega_share_url(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"mega.nz", "www.mega.nz", "mega.co.nz", "www.mega.co.nz"}
        or len([part for part in parsed.path.split("/") if part]) != 2
        or [part for part in parsed.path.split("/") if part][0] != "file"
        or not parsed.fragment
    ):
        raise HTTPException(
            status_code=400,
            detail="Please enter a complete public MEGA file link, including the key after #.",
        )
    return raw_url.strip()


def download_mega_video(share_url: str, destination: Path) -> str:
    url = mega_share_url(share_url)
    staging_dir = destination.parent / (destination.name + "_mega_staging")
    try:
        from mega_client import MegaClient

        # mega-public-client expects a DIRECTORY for normal downloads.
        # Passing the old extensionless destination path caused its internal
        # path handling to treat the output as both source and destination.
        shutil.rmtree(staging_dir, ignore_errors=True)
        staging_dir.mkdir(parents=True, exist_ok=True)

        with MegaClient() as client:
            info = client.get_file_info(url)
            filename = Path(info.name or "mega_video").name
            suffix = Path(filename).suffix.lower()
            if suffix not in {".mp4", ".mkv", ".mov", ".avi", ".webm"}:
                raise HTTPException(
                    status_code=422,
                    detail=f"MEGA file is not a supported video: {filename}",
                )
            if info.size and info.size > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="MEGA video is larger than the app's 2 GB limit.",
                )

            result = client.download_file(url, str(staging_dir), resume=False)
            downloaded_path = Path(result.path) if getattr(result, "path", None) else staging_dir / filename

            if not downloaded_path.is_file() or downloaded_path.stat().st_size == 0:
                raise ValueError("MEGA download finished but the downloaded video file is missing or empty.")
            if downloaded_path.stat().st_size > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="MEGA video is larger than the app's 2 GB limit.",
                )

            # Move from a separate staging directory to the exact path
            # expected by process_job; source and destination cannot be equal.
            if destination.exists():
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            shutil.move(str(downloaded_path), str(destination))
            return filename
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not download the MEGA video: {type(exc).__name__}: {exc}",
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

def get_duration(path: Path) -> float:
    result = run_command([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ])
    try:
        duration = float(result.stdout.strip())
        if duration <= 0:
            raise ValueError()
        return duration
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Could not determine the video duration.")


def parse_ranges(raw: str, duration: float) -> List[Tuple[float, float]]:
    parsed = []
    if not raw.strip():
        return parsed
    for item in raw.split(","):
        try:
            start_text, end_text = item.strip().split("-", 1)
            start, end = float(start_text.strip()), float(end_text.strip())
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid time range. Use seconds like 80-165, 1320-1410.")
        if start < 0 or end <= start or end > duration:
            raise HTTPException(status_code=400, detail=f"Invalid range '{item}'. Start/end must be within the video duration.")
        parsed.append((start, end))
    return parsed


def merge_ranges(ranges: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def remaining_segments(duration: float, removed: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    kept = []
    cursor = 0.0
    for start, end in removed:
        if start > cursor:
            kept.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        kept.append((cursor, duration))
    return kept


def render_clip(source: Path, start: float, length: float, target: Path) -> None:
    # "superfast" is a practical speed/quality compromise for CPU-only hosts.
    # CRF 20 keeps the existing visual-quality target; output files may be larger.
    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{length:.3f}",
        "-map", "0:v:0", "-map", "0:a?", "-sn", "-dn",
        "-c:v", "libx264", "-preset", "superfast", "-crf", "20", "-threads", "0",
        "-c:a", "aac", "-b:a", "192k",
        "-max_muxing_queue_size", "2048", str(target)
    ])


def remux_clip(source: Path, target: Path) -> None:
    # H.264/AAC can be placed in MP4 without encoding the streams a second time.
    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
        "-c", "copy", "-movflags", "+faststart", str(target)
    ])


@app.on_event("startup")
async def mark_interrupted_jobs():
    # In-process background work cannot survive a container restart.
    # Mark unfinished jobs clearly instead of leaving them stuck at "processing".
    for folder in DATA_DIR.iterdir():
        if not folder.is_dir():
            continue
        metadata_path = folder / "job.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("status") in {"queued", "processing"}:
                metadata.update({
                    "status": "failed",
                    "message": "The server restarted before this job finished. Please start it again.",
                    "error": "Server restarted; in-process background jobs cannot resume automatically."
                })
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        except (OSError, ValueError):
            continue


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/health")
async def health():
    return {"status": "ok"}



# Jobs are stored on Railway's persistent volume when /data is mounted.
# Without a volume, /tmp is used and jobs are lost if the container restarts.
DATA_DIR = Path(os.getenv("JOBS_DIR", "/data/anime-clip-jobs" if Path("/data").is_dir() else str(Path(tempfile.gettempdir()) / "anime-clip-jobs")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{24,80}$")
JOB_LOCK = threading.Lock()
MAX_JOB_AGE_SECONDS = 24 * 60 * 60


def job_path(job_id: str) -> Path:
    if not JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="Job not found.")
    return DATA_DIR / job_id


def write_job(job_id: str, **updates):
    folder = job_path(job_id)
    metadata_path = folder / "job.json"
    with JOB_LOCK:
        current = {}
        if metadata_path.exists():
            try:
                current = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = {}
        current.update(updates)
        temporary = metadata_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(current), encoding="utf-8")
        temporary.replace(metadata_path)


def read_job(job_id: str):
    folder = job_path(job_id)
    metadata_path = folder / "job.json"
    if not metadata_path.is_file():
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status_code=500, detail="Could not read job status.")


def cleanup_old_jobs():
    now = time.time()
    for folder in DATA_DIR.iterdir():
        if not folder.is_dir():
            continue
        try:
            metadata = json.loads((folder / "job.json").read_text(encoding="utf-8"))
            created = float(metadata.get("created_at", now))
            if now - created > MAX_JOB_AGE_SECONDS:
                shutil.rmtree(folder, ignore_errors=True)
        except (OSError, ValueError, TypeError):
            # Leave incomplete/unreadable folders alone rather than deleting user data.
            continue


def process_job(job_id: str, source_kind: str, video_url: str, mega_url: str,
                clip_seconds: int, selected_formats: list, opening: str, ending: str):
    folder = job_path(job_id)
    try:
        write_job(job_id, status="processing", message="Preparing video…", progress=0)
        if source_kind in {"gofile", "mega"}:
            downloaded_file = folder / "source_video"
            if source_kind == "mega":
                filename = download_mega_video(mega_url, downloaded_file)
            else:
                filename = download_gofile_video(video_url, downloaded_file)
            suffix = Path(filename).suffix.lower()
            if suffix not in {".mp4", ".mkv", ".mov", ".avi", ".webm"}:
                raise HTTPException(status_code=422, detail="The shared item is not a supported video.")
            source = folder / ("source" + suffix)
            downloaded_file.replace(source)
        else:
            source = folder / "source_upload"
            if not source.is_file() or source.stat().st_size == 0:
                raise HTTPException(status_code=422, detail="Uploaded video is missing or empty.")

        duration = get_duration(source)
        removed = merge_ranges(parse_ranges(opening, duration) + parse_ranges(ending, duration))
        segments = remaining_segments(duration, removed)
        if not segments:
            raise HTTPException(status_code=400, detail="The selected ranges remove the entire video.")

        clip_count = 0
        for segment_start, segment_end in segments:
            remaining = segment_end - segment_start
            clip_count += int(remaining // clip_seconds)
            if remaining % clip_seconds > 0.05:
                clip_count += 1
        if clip_count <= 0:
            raise HTTPException(status_code=400, detail="No clips could be created.")
        write_job(job_id, message=f"Creating clips (0/{clip_count})…", progress=0, total=clip_count)

        created = []
        clip_index = 1
        completed = 0
        for segment_start, segment_end in segments:
            cursor = segment_start
            while cursor < segment_end - 0.05:
                clip_length = min(float(clip_seconds), segment_end - cursor)
                if len(selected_formats) == 2:
                    # Encode once to MKV, then create the MP4 by stream-copy remux.
                    mkv_output = folder / f"clip_{clip_index:03d}.mkv"
                    mp4_output = folder / f"clip_{clip_index:03d}.mp4"
                    render_clip(source, cursor, clip_length, mkv_output)
                    remux_clip(mkv_output, mp4_output)
                    created.extend([mp4_output, mkv_output])
                else:
                    extension = selected_formats[0]
                    output = folder / f"clip_{clip_index:03d}.{extension}"
                    render_clip(source, cursor, clip_length, output)
                    created.append(output)
                cursor += clip_length
                clip_index += 1
                completed += 1
                write_job(job_id, message=f"Creating clips ({completed}/{clip_count})…",
                          progress=round(completed * 100 / clip_count), total=clip_count)

        archive = folder / "anime_clips.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for output in created:
                zip_file.write(output, arcname=output.name)
        write_job(job_id, status="completed", message="Clips are ready to download.",
                  progress=100, total=clip_count, download_url=f"/api/jobs/{job_id}/download")
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc) or "Unknown processing error."
        diagnostic = (
            f"{type(exc).__name__}: {detail}\n\n"
            f"Diagnostic traceback:\n{traceback.format_exc()}"
        )
        write_job(
            job_id,
            status="failed",
            message="Processing failed. See the diagnostic details below.",
            error=diagnostic[-8000:]
        )


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(None),
    video_url: str = Form(""),
    mega_url: str = Form(""),
    clip_seconds: int = Form(90),
    formats: str = Form("mp4"),
    opening: str = Form(""),
    ending: str = Form("")
):
    if clip_seconds < 10 or clip_seconds > 3600:
        raise HTTPException(status_code=400, detail="Clip duration must be between 10 and 3600 seconds.")
    selected_formats = sorted({part.strip().lower() for part in formats.split(",") if part.strip()})
    if not selected_formats or not set(selected_formats).issubset({"mp4", "mkv"}):
        raise HTTPException(status_code=400, detail="Choose MP4, MKV, or both.")
    has_file = file is not None and bool(file.filename)
    has_url = bool(video_url.strip())
    has_mega_url = bool(mega_url.strip())
    if sum((has_file, has_url, has_mega_url)) != 1:
        raise HTTPException(status_code=400, detail="Choose exactly one source: upload a video, a Gofile share link, or a MEGA file link.")
    if has_url:
        gofile_share_code(video_url)
    if has_mega_url:
        mega_share_url(mega_url)

    cleanup_old_jobs()
    job_id = secrets.token_urlsafe(32)
    folder = DATA_DIR / job_id
    folder.mkdir(parents=True, exist_ok=False)
    source_kind = "upload" if has_file else ("mega" if has_mega_url else "gofile")
    write_job(job_id, status="queued", message="Job queued.", progress=0, total=0,
              created_at=time.time(), source_kind=source_kind)

    # Finish receiving an uploaded file before returning the job ID. Once this
    # completes, leaving the page does not cancel the server-side processing.
    if has_file:
        suffix = Path(file.filename).suffix.lower() or ".mp4"
        if suffix not in {".mp4", ".mkv", ".mov", ".avi", ".webm"}:
            shutil.rmtree(folder, ignore_errors=True)
            raise HTTPException(status_code=422, detail="Unsupported video format.")
        try:
            await save_upload(file, folder / "source_upload")
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        finally:
            await file.close()

    background_tasks.add_task(
        process_job, job_id, source_kind, video_url.strip(), mega_url.strip(),
        clip_seconds, selected_formats, opening, ending
    )
    return {"job_id": job_id, "status": "queued", "message": "Job started."}


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    metadata = read_job(job_id)
    return {
        "job_id": job_id,
        "status": metadata.get("status", "queued"),
        "message": metadata.get("message", ""),
        "progress": metadata.get("progress", 0),
        "total": metadata.get("total", 0),
        "error": metadata.get("error"),
        "download_url": metadata.get("download_url")
    }


@app.get("/api/jobs/{job_id}/download")
async def download_job(job_id: str):
    metadata = read_job(job_id)
    if metadata.get("status") != "completed":
        raise HTTPException(status_code=409, detail="This job is not ready yet.")
    archive = job_path(job_id) / "anime_clips.zip"
    if not archive.is_file():
        raise HTTPException(status_code=404, detail="ZIP file is no longer available.")
    return FileResponse(path=archive, media_type="application/zip", filename="anime_clips.zip")
