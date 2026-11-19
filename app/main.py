import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import List, Tuple

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024

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
            raise HTTPException(
                status_code=400,
                detail="Invalid time range. Use seconds like 80-165, 1320-1410."
            )
        if start < 0 or end <= start or end > duration:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid range '{item}'. Start/end must be within the video duration."
            )
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


@app.post("/api/probe")
async def probe(file: UploadFile = File(...)):
    suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
    with tempfile.TemporaryDirectory(prefix="anime-probe-") as temp:
        source = Path(temp) / ("upload" + suffix)
        await save_upload(file, source)
        duration = get_duration(source)
    return {"duration": duration}


@app.post("/api/process")
async def process_video(
    file: UploadFile = File(...),
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

    job_dir = Path(tempfile.mkdtemp(prefix="anime-clip-"))
    source = job_dir / ("source" + (Path(file.filename or "video.mp4").suffix or ".mp4"))

    try:
        await save_upload(file, source)
        duration = get_duration(source)
        removed = merge_ranges(
            parse_ranges(opening, duration) + parse_ranges(ending, duration)
        )
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
