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
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return cleaned[:120] if cleaned else "GlobiFree_Media"


def cleanup_dir(path: Path):
    try:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "Ready"
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

    # 1. INSTANT 0.2s YouTube Fetch via Official oEmbed (Zero 403 / Zero Rate-Limit)
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
            "youtube:player_client=tv_embedded,android_vr,ios,mweb;player_skip=webpage,configs",
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
async def serve_home():
    html_path = Path("index.html")
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>index.html not found</h1>"


@app.api_route("/api/info", methods=["GET", "POST"])
async def api_info(
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
        "--concurrent-fragments", "8",
        "-o", output_template,
    ]

    # Use aria2c for blazing fast 16-connection downloads when not trimming
    if not is_trimmed and shutil.which("aria2c"):
        cmd += [
            "--downloader", "aria2c",
            "--downloader-args", "aria2c:-x 16 -s 16 -k 1M --summary-interval=0",
        ]

    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]

    if client_arg:
        cmd += ["--extractor-args", client_arg]

    if mode == "audio":
        cmd += [
            "-f", "bestaudio/best",
            "-x",
            "--audio-format", format_type,
            "--audio-quality", f"{quality}K" if quality.isdigit() else "0",
        ]
    else:
        if is_muted:
            fmt = f"bestvideo[height<={quality}]/bestvideo/best"
        else:
            fmt = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
        cmd += ["-f", fmt, "--merge-output-format", format_type]

    if is_trimmed:
        cmd += ["--download-sections", f"*{start_time}-{end_time}"]

    if is_muted and mode == "video":
        cmd += ["--postprocessor-args", "ffmpeg:-an"]

    cmd.append(url)

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode == 0:
        files = list(job_dir.glob("media.*"))
        if files:
            return files[0]

    # Clean partial files on failure
    for f in job_dir.glob("*"):
        f.unlink(missing_ok=True)

    raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr else "yt-dlp failed")


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
    for client in ["WEB_EMBED", "IOS", "MWEB", "TV", "ANDROID_VR"]:
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
        raw_audio = Path(audio_stream.download(output_path=str(job_dir), filename="raw_audio"))
        cmd = ["ffmpeg", "-y", "-i", str(raw_audio)]
        if start_time and end_time:
            cmd += ["-ss", start_time, "-to", end_time]
        if format_type == "mp3":
            cmd += ["-vn", "-ab", f"{quality}k", str(out_file)]
        else:
            cmd += ["-vn", str(out_file)]
        subprocess.run(cmd, check=True)
        return out_file

    target_res = f"{quality}p"
    vid_stream = (
        yt.streams.filter(res=target_res, file_extension="mp4", progressive=True).first()
        or yt.streams.filter(res=target_res, file_extension="mp4").first()
        or yt.streams.get_highest_resolution()
    )
    raw_vid = Path(vid_stream.download(output_path=str(job_dir), filename="raw_video.mp4"))

    cmd = ["ffmpeg", "-y", "-i", str(raw_vid)]
    if not vid_stream.is_progressive and not mute_video:
        aud_stream = yt.streams.get_audio_only()
        if aud_stream:
            raw_aud = Path(aud_stream.download(output_path=str(job_dir), filename="raw_audio"))
            cmd += ["-i", str(raw_aud), "-c:v", "copy", "-c:a", "aac"]
        else:
            cmd += ["-c", "copy"]
    elif mute_video:
        cmd += ["-an", "-c:v", "copy"]
    else:
        cmd += ["-c", "copy"]

    if start_time and end_time:
        cmd += ["-ss", start_time, "-to", end_time]

    cmd.append(str(out_file))
    subprocess.run(cmd, check=True)
    return out_file


@app.post("/api/download")
async def api_download(
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

    safe_title = sanitize_filename(custom_title or "GlobiFree_Media")
    downloaded_file: Optional[Path] = None
    last_error = None

    # Fast strategies without any sleep delays
    strategies = (
        [
            "youtube:player_client=tv_embedded,ios,android_vr;player_skip=webpage,configs",
            "youtube:player_client=mweb,web_embedded,tv",
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

    # Fast fallback to pytubefix WEB_EMBED/IOS for YouTube
    if not downloaded_file and is_yt:
        try:
            downloaded_file = download_with_pytubefix(
                url, job_dir, mode, format_type, quality, is_muted, start_time, end_time
            )
        except Exception as e:
            last_error = e

    if not downloaded_file or not downloaded_file.exists():
        cleanup_dir(job_dir)
        raise HTTPException(
            status_code=500,
            detail=f"Download error: {str(last_error) if last_error else 'Failed to process media'}",
        )

    final_ext = downloaded_file.suffix.lstrip(".") or format_type
    final_filename = f"{safe_title}.{final_ext}"
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
