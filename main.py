import os
import re
import json
import time
import uuid
import shutil
import threading
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any
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

# In-memory Turbo Cache for Pre-Warmed Cloud Jobs
JOB_CACHE: Dict[str, Dict[str, Any]] = {}
JOBS_BY_ID: Dict[str, Dict[str, Any]] = {}
CACHE_LOCK = threading.Lock()


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
# 1. INSTANT 0.2s FETCH INFO
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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
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
    except Exception:
        pass

    raise HTTPException(status_code=400, detail="Unable to fetch link details. Please check the URL.")


# =====================================================================
# 2. TURBO CLOUD ENGINE WITH BACKGROUND PRE-WARMING
# =====================================================================
CLOUD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://ddownr.com/",
    "Origin": "https://ddownr.com",
}


def resolve_cloud_format(mode: str, format_type: str, quality: str) -> str:
    if mode == "audio":
        return format_type if format_type in ["mp3", "m4a", "wav", "ogg", "opus"] else "mp3"
    if format_type == "webm":
        return "webm"
    return quality if quality in ["1080", "720", "480", "360"] else "720"


def _background_poll_worker(internal_id: str):
    with CACHE_LOCK:
        entry = JOBS_BY_ID.get(internal_id)
    if not entry:
        return

    progress_urls = entry.get("progress_urls", [])
    # Poll rapidly (every 0.45s) up to 75 times (~34 seconds max for huge videos)
    for _ in range(75):
        if entry.get("status") == "ready":
            return
        for p_url in progress_urls:
            try:
                p_resp = cffi_requests.get(
                    p_url, headers=CLOUD_HEADERS, impersonate="chrome", timeout=5
                )
                if p_resp.status_code == 200:
                    p_data = p_resp.json()
                    raw_prog = p_data.get("progress")
                    if isinstance(raw_prog, (int, float)):
                        pct = min(98, max(15, int(raw_prog / 10)))
                        entry["progress"] = pct

                    dl_url = p_data.get("download_url")
                    if dl_url and (p_data.get("success") == 1 or raw_prog == 1000 or dl_url.startswith("http")):
                        entry["download_url"] = dl_url
                        entry["progress"] = 100
                        entry["status"] = "ready"
                        return

                    if raw_prog == 1000 and not dl_url and p_data.get("success") == 0:
                        entry["status"] = "failed"
                        entry["error"] = p_data.get("text") or "Cloud conversion failed."
                        return
            except Exception:
                continue
        time.sleep(0.45)

    if entry.get("status") != "ready":
        entry["status"] = "failed"
        entry["error"] = "Conversion timed out."


def start_or_get_cloud_job(
    url: str,
    mode: str,
    format_type: str,
    quality: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    vid_id = extract_youtube_id(url)
    clean_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else url
    cloud_fmt = resolve_cloud_format(mode, format_type, quality)

    start_sec = time_str_to_seconds(start_time)
    end_sec = time_str_to_seconds(end_time)
    has_trim = start_sec is not None and end_sec is not None and end_sec > start_sec

    cache_key = f"{vid_id or clean_url}:{cloud_fmt}:{start_sec if has_trim else 0}:{end_sec if has_trim else 0}"

    with CACHE_LOCK:
        existing = JOB_CACHE.get(cache_key)
        if existing and (time.time() - existing["created_at"] < 600) and existing["status"] != "failed":
            return existing

    api_hosts = ["https://p.savenow.to", "https://p.oceansaver.in"]
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
                headers=CLOUD_HEADERS,
                impersonate="chrome",
                timeout=8,
            )
            if init_resp.status_code != 200:
                continue

            init_data = init_resp.json()
            remote_id = init_data.get("id")
            if not remote_id:
                continue

            progress_urls = []
            if init_data.get("progress_url"):
                progress_urls.append(init_data.get("progress_url"))
            progress_urls.append(f"https://p.savenow.to/ajax/progress.php?id={remote_id}")
            progress_urls.append(f"https://p.oceansaver.in/ajax/progress.php?id={remote_id}")
            # Deduplicate while preserving order
            progress_urls = list(dict.fromkeys(progress_urls))

            internal_id = str(uuid.uuid4())
            entry = {
                "job_id": internal_id,
                "remote_id": remote_id,
                "cache_key": cache_key,
                "status": "processing",
                "progress": 20,
                "download_url": None,
                "progress_urls": progress_urls,
                "created_at": time.time(),
                "error": None,
            }

            with CACHE_LOCK:
                JOB_CACHE[cache_key] = entry
                JOBS_BY_ID[internal_id] = entry

            threading.Thread(target=_background_poll_worker, args=(internal_id,), daemon=True).start()
            return entry
        except Exception:
            continue

    return None


def prewarm_youtube_defaults(url: str):
    """Starts 720p MP4 and MP3 320k conversion immediately in background when user clicks Fetch!"""
    try:
        start_or_get_cloud_job(url, "video", "mp4", "720")
        start_or_get_cloud_job(url, "audio", "mp3", "320")
    except Exception:
        pass


# =====================================================================
# 3. FALLBACK ENGINE (For Instagram Reels / Muted Videos)
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
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]

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
    raise RuntimeError(err_lines[-1] if err_lines else "Unable to process media stream")


# =====================================================================
# 4. API ROUTES
# =====================================================================
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

    info = fast_fetch_info(target_url)

    # Immediately pre-warm 720p MP4 and MP3 in the background for 0.1s instant download!
    if info.get("platform") == "YOUTUBE":
        threading.Thread(target=prewarm_youtube_defaults, args=(target_url,), daemon=True).start()

    return info


@app.post("/api/prewarm")
def api_prewarm(
    url: str = Form(...),
    mode: str = Form("video"),
    format_type: str = Form("mp4"),
    quality: str = Form("720"),
):
    if "youtube.com" in url.lower() or "youtu.be" in url.lower():
        entry = start_or_get_cloud_job(url, mode, format_type, quality)
        if entry:
            return {"status": entry["status"], "progress": entry["progress"], "job_id": entry["job_id"]}
    return {"status": "skipped"}


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
    final_filename = f"{safe_header_title}.{format_type}"

    # 1. YOUTUBE TURBO CLOUD ENGINE
    if is_yt and not is_muted:
        entry = start_or_get_cloud_job(url, mode, format_type, quality, start_time, end_time)
        if entry:
            # Wait up to 2.5 seconds in case it's already pre-warmed or finishes right away
            for _ in range(10):
                if entry["status"] == "ready" and entry.get("download_url"):
                    return JSONResponse(
                        {
                            "status": "ready",
                            "download_url": entry["download_url"],
                            "filename": final_filename,
                        }
                    )
                if entry["status"] == "failed":
                    break
                time.sleep(0.25)

            # Still processing? Return job_id so frontend shows real-time progress without timing out!
            if entry["status"] == "processing":
                return JSONResponse(
                    {
                        "status": "processing",
                        "job_id": entry["job_id"],
                        "progress": entry.get("progress", 35),
                        "filename": final_filename,
                    }
                )

    # 2. INSTAGRAM / MUTED FALLBACK ENGINE
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
    local_filename = f"{safe_header_title}.{final_ext}"

    return JSONResponse(
        {
            "status": "ready",
            "download_url": f"/api/stream/{job_id}?filename={quote(local_filename)}",
            "filename": local_filename,
        }
    )


@app.get("/api/job_status")
def api_job_status(job_id: str = Query(...), filename: str = Query("GlobiFree_Media.mp4")):
    with CACHE_LOCK:
        entry = JOBS_BY_ID.get(job_id)

    if not entry:
        raise HTTPException(status_code=404, detail="Job not found.")

    if entry["status"] == "ready" and entry.get("download_url"):
        return {
            "status": "ready",
            "progress": 100,
            "download_url": entry["download_url"],
            "filename": filename,
        }

    if entry["status"] == "failed":
        raise HTTPException(status_code=500, detail=entry.get("error") or "Conversion failed.")

    return {
        "status": "processing",
        "progress": entry.get("progress", 50),
        "filename": filename,
    }


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
