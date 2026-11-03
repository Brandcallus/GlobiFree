import os
import re
import json
import uuid
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from curl_cffi import requests as cffi_requests

app = FastAPI(title="GlobiFree Media Downloader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length"],
)

TEMP_DIR = Path("/tmp/globifree_downloads")
TEMP_DIR.mkdir(parents=True, exist_ok=True)
COOKIES_FILE = Path("cookies.txt")


def sanitize_filename(name: str) -> str:
    # Remove filesystem illegal chars
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return cleaned[:100] if cleaned else "GlobiFree_Media"


def ascii_safe_filename(name: str) -> str:
    # Safe ASCII header filename to prevent Uvicorn latin-1 header crash
    cleaned = sanitize_filename(name)
    ascii_name = cleaned.encode("ascii", "ignore").decode("ascii").strip()
    ascii_name = re.sub(r"\s+", " ", ascii_name)
    return ascii_name if len(ascii_name) > 2 else "GlobiFree_Media"


def cleanup_dir(path: Path):
    try:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "HD Ready"
    seconds = int(seconds)
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def extract_youtube_id(url: str) -> Optional[str]:
    patterns = [
        r"(?:v=|\/videos\/|embed\/|youtu\.be\/|\/v\/|\/e\/|watch\?v=|&v=|\/shorts\/)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def fast_fetch_info(url: str) -> dict:
    is_yt = "youtube.com" in url.lower() or "youtu.be" in url.lower()

    # 1. INSTANT 0.2s YouTube Fetch via Official oEmbed (Untouched & Super Fast)
    if is_yt:
        vid_id = extract_youtube_id(url)
        clean_yt_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else url
        oembed_urls = [
            f"https://www.youtube.com/oembed?url={quote(clean_yt_url)}&format=json",
            f"https://noembed.com/embed?url={quote(clean_yt_url)}",
        ]
        for oembed_endpoint in oembed_urls:
            try:
                resp = cffi_requests.get(oembed_endpoint, impersonate="chrome", timeout=4)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("title"):
                        thumb = data.get("thumbnail_url") or (
                            f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg" if vid_id else ""
                        )
                        return {
                            "title": data.get("title"),
                            "uploader": data.get("author_name") or "YouTube Creator",
                            "duration": 0,
                            "duration_string": "HD Ready",
                            "thumbnail": thumb,
                            "platform": "YOUTUBE",
                            "max_quality": "1080P HD",
                        }
            except Exception:
                pass

    # 2. Fast CLI yt-dlp dump-json for Instagram or fallback
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-playlist",
        "--no-warnings",
        "--no-check-certificates",
        "--geo-bypass",
        "--impersonate", "chrome",
    ]
    if is_yt:
        cmd += [
            "--extractor-args",
            "youtube:player_client=android_vr,tv;player_skip=webpage,configs",
        ]
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]

    cmd.append(url)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        if proc.returncode == 0 and proc.stdout.strip():
            info = json.loads(proc.stdout.splitlines()[0])
            duration = info.get("duration") or 0
            return {
                "title": info.get("title") or "GlobiFree Media",
                "uploader": info.get("uploader") or info.get("channel") or "Creator",
                "duration": duration,
                "duration_string": format_duration(duration),
                "thumbnail": info.get("thumbnail") or "",
                "platform": "INSTAGRAM" if "instagram.com" in url.lower() else "YOUTUBE",
                "max_quality": "1080P HD",
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch details: {str(e)}")

    raise HTTPException(status_code=400, detail="Unable to fetch link details. Please check the URL.")


@app.get("/", response_class=HTMLResponse)
def serve_home():
    html_path = Path("index.html")
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>index.html not found</h1>"


@app.api_route("/api/info", methods=["GET", "POST"])
def api_info(
    url_form: Optional[str] = Form(None, alias="url"),
    url_query: Optional[str] = Query(None, alias="url"),
):
    target_url = (url_form or url_query or "").strip()
    if not target_url:
        raise HTTPException(status_code=400, detail="URL is required.")
    return fast_fetch_info(target_url)


def run_ytdlp_cli_download(
    url: str,
    job_dir: Path,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool,
    start_time: Optional[str],
    end_time: Optional[str],
    client_arg: Optional[str] = None,
) -> Optional[Path]:
    output_template = str(job_dir / "media.%(ext)s")
    is_trimmed = bool(start_time and end_time)

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-check-certificates",
        "--geo-bypass",
        "--impersonate", "chrome",
        "--concurrent-fragments", "12",
        "--http-chunk-size", "10M",
        "--socket-timeout", "15",
        "--retries", "2",
        "--fragment-retries", "2",
        "-o", output_template,
    ]

    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]

    if client_arg:
        cmd += ["--extractor-args", client_arg]

    if mode == "audio":
        cmd += [
            "-f", "bestaudio[protocol^=http]/bestaudio/best",
            "-x",
            "--audio-format", format_type,
            "--audio-quality", f"{quality}K" if quality.isdigit() else "0",
        ]
    else:
        if is_muted:
            fmt = (
                f"bestvideo[height<={quality}][ext=mp4][protocol^=http]/"
                f"bestvideo[height<={quality}][protocol^=http]/"
                f"bestvideo[height<={quality}]/bestvideo/best"
            )
        else:
            # Prefer direct HTTP MP4/M4A streams over slow HLS m3u8 segments
            fmt = (
                f"bestvideo[height<={quality}][ext=mp4][protocol^=http]+bestaudio[ext=m4a][protocol^=http]/"
                f"best[height<={quality}][ext=mp4][protocol^=http]/"
                f"bestvideo[height<={quality}]+bestaudio/"
                f"best[height<={quality}]/best"
            )
        cmd += ["-f", fmt, "--merge-output-format", format_type]

    if is_trimmed:
        cmd += ["--download-sections", f"*{start_time}-{end_time}"]

    if is_muted and mode == "video":
        cmd += ["--postprocessor-args", "ffmpeg:-an"]

    cmd.append(url)

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=65)
    if proc.returncode == 0:
        files = [f for f in job_dir.glob("media.*") if not f.name.endswith(".part")]
        if files and files[0].stat().st_size > 0:
            return files[0]

    for f in job_dir.glob("*"):
        f.unlink(missing_ok=True)

    err_lines = [line for line in (proc.stderr or "").splitlines() if line.strip()]
    raise RuntimeError(err_lines[-1] if err_lines else "Direct stream failed")


def download_with_pytubefix(
    url: str,
    job_dir: Path,
    mode: str,
    format_type: str,
    quality: str,
    mute_video: bool,
    start_time: Optional[str],
    end_time: Optional[str],
) -> Path:
    from pytubefix import YouTube

    yt = None
    last_e = None
    # ANDROID_VR is the fastest & most reliable client on cloud IPs
    for client in ["ANDROID_VR", "WEB_EMBED", "IOS", "MWEB", "TV"]:
        try:
            yt = YouTube(url, client=client)
            _ = yt.streams
            break
        except Exception as e:
            last_e = e

    if not yt:
        raise RuntimeError(f"Fallback failed: {str(last_e)}")

    out_file = job_dir / f"output.{format_type}"

    if mode == "audio":
        audio_stream = yt.streams.get_audio_only()
        if not audio_stream:
            audio_stream = yt.streams.filter(only_audio=True).first()
        raw_audio = Path(audio_stream.download(output_path=str(job_dir), filename="raw_audio"))
        cmd = ["ffmpeg", "-y", "-i", str(raw_audio)]
        if start_time and end_time:
            cmd += ["-ss", start_time, "-to", end_time]
        if format_type == "mp3":
            cmd += ["-vn", "-ab", f"{quality}k", str(out_file)]
        else:
            cmd += ["-vn", str(out_file)]
        subprocess.run(cmd, check=True, timeout=45)
        return out_file

    target_res = f"{quality}p"
    # 1. For fast 720p/360p, prefer progressive stream (video+audio already merged = instant download)
    vid_stream = None
    if not mute_video:
        vid_stream = yt.streams.filter(res=target_res, file_extension="mp4", progressive=True).first()

    if not vid_stream:
        vid_stream = (
            yt.streams.filter(res=target_res, file_extension="mp4").first()
            or yt.streams.filter(progressive=True, file_extension="mp4").order_by("resolution").desc().first()
            or yt.streams.get_highest_resolution()
        )

    raw_vid = Path(vid_stream.download(output_path=str(job_dir), filename="raw_video.mp4"))

    # If no extra processing is needed, return the downloaded MP4 directly!
    if vid_stream.is_progressive and not mute_video and not (start_time and end_time) and format_type == "mp4":
        return raw_vid

    cmd = ["ffmpeg", "-y", "-i", str(raw_vid)]
    if not vid_stream.is_progressive and not mute_video:
        aud_stream = yt.streams.get_audio_only()
        if aud_stream:
            raw_aud = Path(aud_stream.download(output_path=str(job_dir), filename="raw_audio"))
            cmd += ["-i", str(raw_aud), "-c:v", "copy", "-c:a", "aac", "-shortest"]
        else:
            cmd += ["-c", "copy"]
    elif mute_video:
        cmd += ["-an", "-c:v", "copy"]
    else:
        cmd += ["-c", "copy"]

    if start_time and end_time:
        cmd += ["-ss", start_time, "-to", end_time]

    cmd.append(str(out_file))
    subprocess.run(cmd, check=True, timeout=60)
    return out_file


# Notice: Changed from `async def` to `def` so FastAPI runs downloads in a threadpool without freezing!
@app.post("/api/download")
def api_download(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    mode: str = Form("video"),
    format_type: str = Form("mp4"),
    quality: str = Form("720"),
    mute_video: str = Form("false"),
    custom_title: Optional[str] = Form(None),
    start_time: Optional[str] = Form(None),
    end_time: Optional[str] = Form(None),
):
    job_id = str(uuid.uuid4())
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    is_muted = mute_video.lower() == "true"
    is_yt = "youtube.com" in url.lower() or "youtu.be" in url.lower()

    if not custom_title:
        try:
            meta = fast_fetch_info(url)
            custom_title = meta.get("title")
        except Exception:
            custom_title = "GlobiFree_Media"

    safe_header_title = ascii_safe_filename(custom_title or "GlobiFree_Media")
    downloaded_file: Optional[Path] = None
    last_error = None

    # 1. For YouTube: Try pytubefix ANDROID_VR first (fastest direct stream without HLS/aria2c hang)
    if is_yt:
        try:
            downloaded_file = download_with_pytubefix(
                url, job_dir, mode, format_type, quality, is_muted, start_time, end_time
            )
        except Exception as e:
            last_error = e
            for f in job_dir.glob("*"):
                f.unlink(missing_ok=True)

    # 2. Try yt-dlp CLI with android_vr (for Instagram or YouTube fallback)
    if not downloaded_file or not downloaded_file.exists():
        strategies = (
            [
                "youtube:player_client=android_vr;player_skip=webpage,configs",
                "youtube:player_client=tv,web_embedded;player_skip=webpage,configs",
            ]
            if is_yt
            else [None]
        )

        for strat in strategies:
            try:
                downloaded_file = run_ytdlp_cli_download(
                    url, job_dir, mode, format_type, quality, is_muted, start_time, end_time, strat
                )
                if downloaded_file and downloaded_file.exists():
                    break
            except Exception as e:
                last_error = e

    if not downloaded_file or not downloaded_file.exists():
        cleanup_dir(job_dir)
        raise HTTPException(
            status_code=500,
            detail=f"Download error: {str(last_error) if last_error else 'Failed to process media'}",
        )

    final_ext = downloaded_file.suffix.lstrip(".") or format_type
    final_filename = f"{safe_header_title}.{final_ext}"
    file_size = downloaded_file.stat().st_size

    background_tasks.add_task(cleanup_dir, job_dir)

    return FileResponse(
        path=str(downloaded_file),
        filename=final_filename,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{final_filename}"',
            "Content-Length": str(file_size),
        },
    )
