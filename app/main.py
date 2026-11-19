import hashlib
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

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
        detail = (exc.stderr or "Media processing failed")[-3000:]
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
    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{length:.3f}",
        "-map", "0:v:0", "-map", "0:a?", "-sn", "-dn",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-max_muxing_queue_size", "2048", str(target)
    ])


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/process")
async def process_video(
    file: UploadFile = File(None),
    video_url: str = Form(""),
    clip_seconds: int = Form(90),
    formats: str = Form("mp4"),
    opening: str = Form(""),
    ending: str = Form("")
):
    if clip_seconds < 10 or clip_seconds > 3600:
        raise HTTPException(status_code=400, detail="Clip duration must be between 10 and 3600 seconds.")
    selected_formats = {part.strip().lower() for part in formats.split(",") if part.strip()}
    if not selected_formats or not selected_formats.issubset({"mp4", "mkv"}):
        raise HTTPException(status_code=400, detail="Choose MP4, MKV, or both.")
    has_file = file is not None and bool(file.filename)
    has_url = bool(video_url.strip())
    if has_file == has_url:
        raise HTTPException(status_code=400, detail="Choose either a video file or one Gofile share link, not both.")

    job_dir = Path(tempfile.mkdtemp(prefix="anime-clip-"))
    try:
        if has_url:
            filename = download_gofile_video(video_url, job_dir / "source_video")
            suffix = Path(filename).suffix.lower()
            if suffix not in {".mp4", ".mkv", ".mov", ".avi", ".webm"}:
                raise HTTPException(status_code=422, detail="The Gofile item is not a supported video.")
            source = job_dir / ("source" + suffix)
            (job_dir / "source_video").replace(source)
        else:
            suffix = Path(file.filename).suffix.lower() or ".mp4"
            source = job_dir / ("source" + suffix)
            await save_upload(file, source)

        duration = get_duration(source)
        removed = merge_ranges(parse_ranges(opening, duration) + parse_ranges(ending, duration))
        segments = remaining_segments(duration, removed)
        if not segments:
            raise HTTPException(status_code=400, detail="The selected ranges remove the entire video.")

        created = []
        clip_index = 1
        for segment_start, segment_end in segments:
            cursor = segment_start
            while cursor < segment_end - 0.05:
                clip_length = min(float(clip_seconds), segment_end - cursor)
                for extension in sorted(selected_formats):
                    output = job_dir / f"clip_{clip_index:03d}.{extension}"
                    render_clip(source, cursor, clip_length, output)
                    created.append(output)
                cursor += clip_length
                clip_index += 1

        archive = job_dir / "anime_clips.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for output in created:
                zip_file.write(output, arcname=output.name)
        return FileResponse(
            path=archive,
            media_type="application/zip",
            filename="anime_clips.zip",
            background=BackgroundTask(shutil.rmtree, job_dir, ignore_errors=True)
        )
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
