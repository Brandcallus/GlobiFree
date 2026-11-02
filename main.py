import os
import re
import uuid
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Query, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp

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
        return "00:00"
    seconds = int(seconds)
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def get_yt_dlp_base_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "js_runtimes": {"deno": {}, "node": {}},
    }


def extract_info_with_fallback(url: str):
    client_strategies = [
        None,  # Default yt-dlp with Deno/Node EJS
        {"youtube": {"player_client": ["ios", "android_vr", "web"]}},
        {"youtube": {"player_client": ["tv", "mweb"]}},
    ]
    last_err = None
    for ext_args in client_strategies:
        opts = get_yt_dlp_base_opts()
        if ext_args:
            opts["extractor_args"] = ext_args
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            last_err = e

    # Fallback to pytubefix for YouTube URLs
    if "youtube.com" in url or "youtu.be" in url:
        try:
            from pytubefix import YouTube
            yt = YouTube(url)
            return {
                "title": yt.title,
                "uploader": yt.author,
                "duration": yt.length,
                "thumbnail": yt.thumbnail_url,
                "extractor": "youtube",
            }
        except Exception as e:
            last_err = e

    raise HTTPException(status_code=400, detail=f"Failed to fetch info: {str(last_err)}")


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

    info = extract_info_with_fallback(target_url)
    duration = info.get("duration") or 0
    platform = "INSTAGRAM" if "instagram.com" in target_url.lower() else "YOUTUBE"

    return {
        "title": info.get("title") or "GlobiFree Media",
        "uploader": info.get("uploader") or info.get("channel") or "Creator",
        "duration": duration,
        "duration_string": format_duration(duration),
        "thumbnail": info.get("thumbnail") or "",
        "platform": platform,
        "max_quality": "1080P HD",
    }


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

    yt = YouTube(url)
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

    # Video mode
    target_res = f"{quality}p"
    vid_stream = (
        yt.streams.filter(res=target_res, file_extension="mp4").first()
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
    safe_title = sanitize_filename(custom_title or "GlobiFree_Media")
    output_template = str(job_dir / f"media.%(ext)s")

    client_strategies = [
        None,
        {"youtube": {"player_client": ["ios", "android_vr", "web"]}},
        {"youtube": {"player_client": ["tv", "mweb"]}},
    ]

    downloaded_file: Optional[Path] = None
    last_error = None

    for ext_args in client_strategies:
        opts = get_yt_dlp_base_opts()
        opts["outtmpl"] = output_template
        if ext_args:
            opts["extractor_args"] = ext_args

        if mode == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": format_type,
                    "preferredquality": quality,
                }
            ]
        else:
            if is_muted:
                opts["format"] = f"bestvideo[height<={quality}]/bestvideo/best"
            else:
                opts["format"] = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
            opts["merge_output_format"] = format_type

        postprocessor_args = []
        if is_muted and mode == "video":
            postprocessor_args.extend(["-an"])
        if start_time and end_time:
            postprocessor_args.extend(["-ss", start_time, "-to", end_time])
        if postprocessor_args:
            opts["postprocessor_args"] = {"ffmpeg": postprocessor_args}

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not custom_title and info and info.get("title"):
                    safe_title = sanitize_filename(info["title"])
            files = list(job_dir.glob("media.*"))
            if files:
                downloaded_file = files[0]
                break
        except Exception as e:
            last_error = e
            for f in job_dir.glob("*"):
                f.unlink(missing_ok=True)

    # Fallback to pytubefix if yt-dlp failed on YouTube
    if not downloaded_file and ("youtube.com" in url or "youtu.be" in url):
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
            detail=f"Download failed: {str(last_error) if last_error else 'Unknown error'}",
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
