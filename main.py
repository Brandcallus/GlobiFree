import os
import re
import json
import time
import uuid
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from curl_cffi import requests as cffi_requests

app = FastAPI(title="GlobiFree Media Downloader Pro")

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
    return cleaned[:100] if cleaned else "GlobiFree_Media"

def ascii_safe_filename(name: str) -> str:
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

def time_str_to_seconds(t_str: Optional[str]) -> Optional[int]:
    if not t_str:
        return None
    parts = [int(p) for p in str(t_str).strip().split(":") if p.isdigit()]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 1:
        return parts[0]
    return None

def extract_youtube_id(url: str) -> Optional[str]:
    patterns = [
        r"(?:v=|\/videos\/|embed\/|youtu\.be\/|\/v\/|\/e\/|watch\?v=|&v=|\/shorts\/)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

# =====================================================================
# FAST FETCH INFO (0.2s Instant oEmbed)
# =====================================================================
def fast_fetch_info(url: str) -> dict:
    is_yt = "youtube.com" in url.lower() or "youtu.be" in url.lower()

    if is_yt:
        vid_id = extract_youtube_id(url)
        clean_yt_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else url
        oembed_urls = [
            f"https://www.youtube.com/oembed?url={quote(clean_yt_url)}&format=json",
            f"https://noembed.com/embed?url={quote(clean_yt_url)}",
        ]
        for oembed_endpoint in oembed_urls:
            try:
                resp = cffi_requests.get(oembed_endpoint, impersonate="chrome", timeout=3)
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

    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-playlist",
        "--no-warnings",
        "--no-check-certificates",
        "--geo-bypass",
        "--impersonate", "chrome",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
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
        raise HTTPException(status_code=400, detail="Could not fetch details. Check link.")

    raise HTTPException(status_code=400, detail="Unable to fetch link details.")


# =====================================================================
# ENGINE 1: ULTRA-FAST DIRECT CHROME DOWNLOAD URL GENERATOR
# =====================================================================
def get_cloud_cdn_direct_link(
    url: str,
    mode: str,
    format_type: str,
    quality: str,
    start_time: Optional[str],
    end_time: Optional[str],
) -> Optional[str]:
    vid_id = extract_youtube_id(url)
    clean_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else url

    if mode == "audio":
        cloud_fmt = format_type if format_type in ["mp3", "m4a", "wav"] else "mp3"
    else:
        cloud_fmt = quality if quality in ["1080", "720", "480", "360"] else "720"

    start_sec = time_str_to_seconds(start_time)
    end_sec = time_str_to_seconds(end_time)
    has_trim = start_sec is not None and end_sec is not None and end_sec > start_sec

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0",
        "Referer": "https://ddownr.com/",
        "Origin": "https://ddownr.com",
    }

    api_hosts = ["https://p.oceansaver.in", "https://p.savenow.to"]

    for host in api_hosts:
        try:
            params = {
                "button": "1",
                "start": str(start_sec) if has_trim else "1",
                "end": str(end_sec) if has_trim else "1",
                "format": cloud_fmt,
                "iframe_source": "https://ddownr.com/",
                "url": clean_url,
            }
            init_resp = cffi_requests.get(
                f"{host}/ajax/download.php",
                params=params,
                headers=headers,
                impersonate="chrome",
                timeout=5,
            )
            if init_resp.status_code != 200:
                continue

            init_data = init_resp.json()
            job_id = init_data.get("id")
            if not job_id:
                continue

            progress_urls = [
                init_data.get("progress_url"),
                f"{host}/ajax/progress.php?id={job_id}",
            ]
            progress_urls = [u for u in progress_urls if u]

            # Fast Poll (Reduced to 12s total to avoid hanging at 90%)
            for _ in range(12):
                time.sleep(1.0)
                for p_url in progress_urls:
                    try:
                        p_resp = cffi_requests.get(p_url, headers=headers, impersonate="chrome", timeout=4)
                        if p_resp.status_code == 200:
                            p_data = p_resp.json()
                            dl_url = p_data.get("download_url")
                            if dl_url and (p_data.get("success") == 1 or p_data.get("progress") == 1000):
                                return dl_url
                    except Exception:
                        continue
        except Exception:
            continue

    return None

# =====================================================================
# ENGINE 2: SERVER FALLBACK (For Instagram/Muted)
# =====================================================================
def run_local_fallback_download(
    url: str,
    job_dir: Path,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool,
    start_time: Optional[str],
    end_time: Optional[str],
) -> Path:
    output_template = str(job_dir / "media.%(ext)s")
    is_trimmed = bool(start_time and end_time)
    is_yt = "youtube.com" in url.lower() or "youtu.be" in url.lower()

    if is_yt:
        try:
            from pytubefix import YouTube
            for client in ["ANDROID_VR", "IOS", "WEB_EMBED"]:
                try:
                    yt = YouTube(url, client=client)
                    out_file = job_dir / f"output.{format_type}"
                    if mode == "audio":
                        stream = yt.streams.get_audio_only() or yt.streams.filter(only_audio=True).first()
                        raw = Path(stream.download(output_path=str(job_dir), filename="raw_aud"))
                        cmd = ["ffmpeg", "-y", "-i", str(raw)]
                        if is_trimmed:
                            cmd += ["-ss", start_time, "-to", end_time]
                        cmd += ["-vn", "-ab", f"{quality}k", str(out_file)] if format_type == "mp3" else ["-vn", str(out_file)]
                        subprocess.run(cmd, check=True, timeout=45)
                        return out_file
                    else:
                        stream = (
                            yt.streams.filter(res=f"{quality}p", file_extension="mp4", progressive=True).first()
                            or yt.streams.filter(progressive=True, file_extension="mp4").order_by("resolution").desc().first()
                            or yt.streams.get_highest_resolution()
                        )
                        raw_v = Path(stream.download(output_path=str(job_dir), filename="raw_vid.mp4"))
                        if stream.is_progressive and not is_muted and not is_trimmed and format_type == "mp4":
                            return raw_v
                        cmd = ["ffmpeg", "-y", "-i", str(raw_v)]
                        if is_muted:
                            cmd += ["-an", "-c:v", "copy"]
                        else:
                            cmd += ["-c", "copy"]
                        if is_trimmed:
                            cmd += ["-ss", start_time, "-to", end_time]
                        cmd.append(str(out_file))
                        subprocess.run(cmd, check=True, timeout=60)
                        return out_file
                except Exception:
                    continue
        except Exception:
            pass

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-check-certificates",
        "--geo-bypass",
        "--impersonate", "chrome",
        "--concurrent-fragments", "10",
        "-o", output_template,
    ]
    if mode == "audio":
        cmd += [
            "-f", "bestaudio/best",
            "-x",
            "--audio-format", format_type,
            "--audio-quality", f"{quality}K" if quality.isdigit() else "0",
        ]
    else:
        fmt = (
            f"bestvideo[height<={quality}]/bestvideo/best"
            if is_muted
            else f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
        )
        cmd += ["-f", fmt, "--merge-output-format", format_type]

    if is_trimmed:
        cmd += ["--download-sections", f"*{start_time}-{end_time}"]
    if is_muted and mode == "video":
        cmd += ["--postprocessor-args", "ffmpeg:-an"]

    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if proc.returncode == 0:
        files = [f for f in job_dir.glob("media.*") if not f.name.endswith(".part")]
        if files and files[0].stat().st_size > 0:
            return files[0]

    err_lines = [line for line in (proc.stderr or "").splitlines() if line.strip()]
    raise RuntimeError(err_lines[-1] if err_lines else "Unable to process stream")

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

@app.post("/api/prepare")
def api_prepare(
    url: str = Form(...),
    mode: str = Form("video"),
    format_type: str = Form("mp4"),
    quality: str = Form("720"),
    mute_video: str = Form("false"),
    custom_title: Optional[str] = Form(None),
    start_time: Optional[str] = Form(None),
    end_time: Optional[str] = Form(None),
):
    is_muted = mute_video.lower() == "true"
    is_yt = "youtube.com" in url.lower() or "youtu.be" in url.lower()

    if not custom_title:
        try:
            meta = fast_fetch_info(url)
            custom_title = meta.get("title")
        except Exception:
            custom_title = "GlobiFree_Media"

    safe_header_title = ascii_safe_filename(custom_title or "GlobiFree_Media")

    # Fast YouTube Chrome Link Generation
    if is_yt and not is_muted:
        direct_cdn_url = get_cloud_cdn_direct_link(
            url, mode, format_type, quality, start_time, end_time
        )
        if direct_cdn_url:
            return JSONResponse(
                {
                    "type": "direct",
                    "download_url": direct_cdn_url,
                    "filename": f"{safe_header_title}.{format_type}",
                }
            )

    job_id = str(uuid.uuid4())
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        downloaded_file = run_local_fallback_download(
            url, job_dir, mode, format_type, quality, is_muted, start_time, end_time
        )
    except Exception as e:
        cleanup_dir(job_dir)
        raise HTTPException(status_code=500, detail=f"Download error: {str(e)}")

    final_ext = downloaded_file.suffix.lstrip(".") or format_type
    final_filename = f"{safe_header_title}.{final_ext}"

    return JSONResponse(
        {
            "type": "local",
            "download_url": f"/api/stream/{job_id}?filename={quote(final_filename)}",
            "filename": final_filename,
        }
    )

@app.get("/api/stream/{job_id}")
def stream_prepared_file(
    job_id: str,
    background_tasks: BackgroundTasks,
    filename: str = Query("GlobiFree_Media.mp4"),
):
    job_dir = TEMP_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="File expired or not found.")

    files = [f for f in job_dir.glob("*") if f.is_file()]
    if not files:
        cleanup_dir(job_dir)
        raise HTTPException(status_code=404, detail="File not found.")

    target_file = files[0]
    safe_name = ascii_safe_filename(Path(filename).stem) + target_file.suffix
    file_size = target_file.stat().st_size

    background_tasks.add_task(cleanup_dir, job_dir)

    return FileResponse(
        path=str(target_file),
        filename=safe_name,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Content-Length": str(file_size),
        },
    )
