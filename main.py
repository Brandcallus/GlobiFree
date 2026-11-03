import os
import re
import json
import time
import uuid
import shutil
import threading
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List
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
FEEDBACK_FILE = Path("/tmp/globifree_feedback.json")

# Persistent Keep-Alive Chrome Session
HTTP_SESSION = cffi_requests.Session(impersonate="chrome")

# In-memory Turbo Caches
INFO_CACHE: Dict[str, Dict[str, Any]] = {}
JOB_CACHE: Dict[str, Dict[str, Any]] = {}
JOBS_BY_ID: Dict[str, Dict[str, Any]] = {}
CACHE_LOCK = threading.Lock()
FEEDBACK_LOCK = threading.Lock()


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


def format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "HD Ready"
    seconds = int(float(seconds))
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def time_str_to_seconds(t_str: Optional[str]) -> Optional[float]:
    if t_str is None:
        return None
    clean = str(t_str).strip()
    if not clean:
        return None
    try:
        if ":" not in clean:
            return max(0.0, float(clean))
        parts = [float(p) for p in clean.split(":")]
        if len(parts) == 2:
            return max(0.0, parts[0] * 60 + parts[1])
        if len(parts) == 3:
            return max(0.0, parts[0] * 3600 + parts[1] * 60 + parts[2])
    except Exception:
        return None
    return None


def detect_platform_name(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "YOUTUBE"
    if "instagram.com" in u:
        return "INSTAGRAM"
    if "pinterest." in u or "pin.it" in u:
        return "PINTEREST"
    if "twitter.com" in u or "x.com" in u or "t.co" in u:
        return "X / TWITTER"
    if "facebook.com" in u or "fb.watch" in u or "fb.com" in u:
        return "FACEBOOK"
    if "reddit.com" in u or "redd.it" in u:
        return "REDDIT"
    if "tiktok.com" in u:
        return "TIKTOK"
    if "threads.net" in u:
        return "THREADS"
    if "soundcloud.com" in u:
        return "SOUNDCLOUD"
    if "twitch.tv" in u:
        return "TWITCH"
    if "vimeo.com" in u:
        return "VIMEO"
    return "UNIVERSAL"


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
# 1. INSTANT 0.2s FETCH INFO (YouTube + 10+ Universal Platforms)
# =====================================================================
def fast_fetch_info(url: str) -> dict:
    platform = detect_platform_name(url)
    is_yt = platform == "YOUTUBE"
    vid_id = extract_youtube_id(url) if is_yt else None
    cache_key = vid_id or url.strip()

    with CACHE_LOCK:
        cached_info = INFO_CACHE.get(cache_key)
        if cached_info and (time.time() - cached_info.get("_ts", 0) < 1800):
            return {k: v for k, v in cached_info.items() if k != "_ts"}

    # A. Instant 0.2s YouTube oEmbed (Untouched & Fast)
    if is_yt:
        clean_yt_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else url
        oembed_urls = [
            f"https://www.youtube.com/oembed?url={quote(clean_yt_url)}&format=json",
            f"https://noembed.com/embed?url={quote(clean_yt_url)}",
        ]
        for oembed_endpoint in oembed_urls:
            try:
                resp = HTTP_SESSION.get(oembed_endpoint, timeout=3)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("title"):
                        thumb = (
                            f"https://i.ytimg.com/vi/{vid_id}/maxresdefault.jpg"
                            if vid_id
                            else data.get("thumbnail_url", "")
                        )
                        fallback_thumb = data.get("thumbnail_url") or (
                            f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg" if vid_id else ""
                        )
                        result = {
                            "title": data.get("title"),
                            "uploader": data.get("author_name") or "YouTube Creator",
                            "duration": 0,
                            "duration_string": "HD Ready",
                            "thumbnail": fallback_thumb,
                            "hd_thumbnail": thumb,
                            "platform": "YOUTUBE",
                            "max_quality": "1080P HD",
                        }
                        with CACHE_LOCK:
                            INFO_CACHE[cache_key] = {**result, "_ts": time.time()}
                        return result
            except Exception:
                pass

    # B. Universal yt-dlp metadata extraction for Instagram, Pinterest, X, FB, Reddit, TikTok, etc.
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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        if proc.returncode == 0 and proc.stdout.strip():
            info = json.loads(proc.stdout.splitlines()[0])
            duration = info.get("duration") or 0
            thumb = info.get("thumbnail") or ""
            result = {
                "title": info.get("title") or f"{platform.title()} Media",
                "uploader": info.get("uploader") or info.get("channel") or platform.title(),
                "duration": duration,
                "duration_string": format_duration(duration),
                "thumbnail": thumb,
                "hd_thumbnail": thumb,
                "platform": platform,
                "max_quality": "HD READY",
            }
            with CACHE_LOCK:
                INFO_CACHE[cache_key] = {**result, "_ts": time.time()}
            return result
    except Exception:
        pass

    # C. Noembed Universal Fallback (for TikTok, X, SoundCloud, Vimeo if yt-dlp metadata is slow)
    try:
        resp = HTTP_SESSION.get(f"https://noembed.com/embed?url={quote(url)}", timeout=4)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("title"):
                thumb = data.get("thumbnail_url") or ""
                result = {
                    "title": data.get("title"),
                    "uploader": data.get("author_name") or platform.title(),
                    "duration": 0,
                    "duration_string": "HD Ready",
                    "thumbnail": thumb,
                    "hd_thumbnail": thumb,
                    "platform": platform,
                    "max_quality": "HD READY",
                }
                with CACHE_LOCK:
                    INFO_CACHE[cache_key] = {**result, "_ts": time.time()}
                return result
    except Exception:
        pass

    raise HTTPException(status_code=400, detail="Unable to fetch link details. Ensure the link is public.")


# =====================================================================
# 2. YOUTUBE TURBO CLOUD ENGINE + EXACT FFMPEG CLIP TRIMMER
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


def cut_remote_or_local_media_exact(
    source_url_or_path: str,
    out_file: Path,
    start_sec: float,
    end_sec: float,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool = False,
) -> Path:
    """
    Uses FFmpeg fast HTTP Range seeking (-ss before -i) so ONLY the requested
    1s - 5m segment is downloaded and cut with millisecond precision!
    """
    clip_duration = max(0.5, round(end_sec - start_sec, 2))
    cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(start_sec),
        "-i", source_url_or_path,
        "-t", str(clip_duration),
    ]

    if mode == "audio":
        cmd += ["-vn"]
        if format_type == "mp3":
            bitrate = f"{quality}k" if str(quality).isdigit() else "320k"
            cmd += ["-c:a", "libmp3lame", "-b:a", bitrate]
        elif format_type == "m4a":
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        elif format_type == "wav":
            cmd += ["-c:a", "pcm_s16le"]
    else:
        if is_muted:
            cmd += ["-an"]
        # Ultrafast re-encode guarantees exact 1-second frame-accurate cuts without black screens
        if format_type == "webm":
            cmd += ["-c:v", "libvpx", "-b:v", "1M"]
            if not is_muted:
                cmd += ["-c:a", "libvorbis"]
        else:
            cmd += [
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "23",
                "-movflags", "+faststart",
            ]
            if not is_muted:
                cmd += ["-c:a", "aac", "-b:a", "128k"]

    cmd.append(str(out_file))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=95)
    if proc.returncode == 0 and out_file.exists() and out_file.stat().st_size > 0:
        return out_file

    # Fallback: stream copy if codec re-encode failed
    fallback_cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(start_sec),
        "-i", source_url_or_path,
        "-t", str(clip_duration),
    ]
    if is_muted and mode == "video":
        fallback_cmd += ["-an", "-c:v", "copy"]
    else:
        fallback_cmd += ["-c", "copy"]
    fallback_cmd.append(str(out_file))
    subprocess.run(fallback_cmd, check=True, timeout=60)
    return out_file


def _background_poll_worker(internal_id: str):
    with CACHE_LOCK:
        entry = JOBS_BY_ID.get(internal_id)
    if not entry:
        return

    progress_urls = entry.get("progress_urls", [])
    has_trim = entry.get("has_trim", False)

    for attempt in range(90):
        if entry.get("status") == "ready":
            return
        for p_url in progress_urls:
            try:
                p_resp = HTTP_SESSION.get(p_url, headers=CLOUD_HEADERS, timeout=4)
                if p_resp.status_code == 200:
                    p_data = p_resp.json()
                    raw_prog = p_data.get("progress")
                    if isinstance(raw_prog, (int, float)):
                        max_p = 85 if has_trim else 98
                        pct = min(max_p, max(20, int(raw_prog / 10)))
                        entry["progress"] = pct

                    dl_url = p_data.get("download_url")
                    if dl_url and (p_data.get("success") == 1 or raw_prog == 1000 or dl_url.startswith("http")):
                        # If user did NOT request trim, return the direct CDN link immediately!
                        if not has_trim:
                            entry["download_url"] = dl_url
                            entry["progress"] = 100
                            entry["status"] = "ready"
                            return

                        # If user DID request trim, slice the exact clip via FFmpeg HTTP Range seeking!
                        try:
                            entry["progress"] = 92
                            trim_dir = TEMP_DIR / internal_id
                            trim_dir.mkdir(parents=True, exist_ok=True)
                            ext = entry["format_type"]
                            out_path = trim_dir / f"trimmed.{ext}"
                            cut_remote_or_local_media_exact(
                                dl_url,
                                out_path,
                                entry["start_sec"],
                                entry["end_sec"],
                                entry["mode"],
                                entry["format_type"],
                                entry["quality"],
                                entry.get("is_muted", False),
                            )
                            entry["download_url"] = f"/api/stream/{internal_id}?filename={quote(entry['filename'])}"
                            entry["progress"] = 100
                            entry["status"] = "ready"
                            return
                        except Exception as e:
                            entry["status"] = "failed"
                            entry["error"] = f"Clip trimming failed: {str(e)}"
                            return

                    if raw_prog == 1000 and not dl_url and p_data.get("success") == 0:
                        entry["status"] = "failed"
                        entry["error"] = p_data.get("text") or "Cloud conversion failed."
                        return
            except Exception:
                continue
        time.sleep(0.25 if attempt < 25 else 0.45)

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
    is_muted: bool = False,
    filename: str = "GlobiFree_Media.mp4",
) -> Optional[Dict[str, Any]]:
    vid_id = extract_youtube_id(url)
    clean_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else url
    cloud_fmt = resolve_cloud_format(mode, format_type, quality)

    start_sec = time_str_to_seconds(start_time)
    end_sec = time_str_to_seconds(end_time)
    has_trim = start_sec is not None and end_sec is not None and end_sec > start_sec

    cache_key = f"{vid_id or clean_url}:{cloud_fmt}:{start_sec if has_trim else 0}:{end_sec if has_trim else 0}:{is_muted}"

    with CACHE_LOCK:
        existing = JOB_CACHE.get(cache_key)
        if existing and (time.time() - existing["created_at"] < 600) and existing["status"] != "failed":
            # If a pre-warmed base stream already has a download_url and we need a trim:
            return existing

        # Check if an untrimmed pre-warmed job is already ready that we can slice immediately!
        if has_trim:
            base_key = f"{vid_id or clean_url}:{cloud_fmt}:0:0:False"
            base_job = JOB_CACHE.get(base_key)
            if base_job and base_job.get("status") == "ready" and base_job.get("download_url"):
                internal_id = str(uuid.uuid4())
                entry = {
                    "job_id": internal_id,
                    "remote_id": base_job.get("remote_id"),
                    "cache_key": cache_key,
                    "status": "processing",
                    "progress": 88,
                    "download_url": None,
                    "progress_urls": base_job.get("progress_urls", []),
                    "created_at": time.time(),
                    "error": None,
                    "has_trim": True,
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "mode": mode,
                    "format_type": format_type,
                    "quality": quality,
                    "is_muted": is_muted,
                    "filename": filename,
                }
                JOB_CACHE[cache_key] = entry
                JOBS_BY_ID[internal_id] = entry

                def _slice_ready_stream(base_dl_url: str, job_entry: dict):
                    try:
                        trim_dir = TEMP_DIR / job_entry["job_id"]
                        trim_dir.mkdir(parents=True, exist_ok=True)
                        out_path = trim_dir / f"trimmed.{job_entry['format_type']}"
                        cut_remote_or_local_media_exact(
                            base_dl_url,
                            out_path,
                            job_entry["start_sec"],
                            job_entry["end_sec"],
                            job_entry["mode"],
                            job_entry["format_type"],
                            job_entry["quality"],
                            job_entry["is_muted"],
                        )
                        job_entry["download_url"] = f"/api/stream/{job_entry['job_id']}?filename={quote(job_entry['filename'])}"
                        job_entry["progress"] = 100
                        job_entry["status"] = "ready"
                    except Exception as e:
                        job_entry["status"] = "failed"
                        job_entry["error"] = f"Trimming error: {str(e)}"

                threading.Thread(
                    target=_slice_ready_stream, args=(base_job["download_url"], entry), daemon=True
                ).start()
                return entry

    api_hosts = ["https://p.savenow.to", "https://p.oceansaver.in"]
    for host in api_hosts:
        try:
            # Always request full base stream from CDN (fast cache hit) and slice exact timestamps with FFmpeg if trimmed
            params = {
                "button": "1",
                "start": "1",
                "end": "1",
                "format": cloud_fmt,
                "iframe_source": "https://ddownr.com/",
                "url": clean_url,
            }
            init_resp = HTTP_SESSION.get(
                f"{host}/ajax/download.php",
                params=params,
                headers=CLOUD_HEADERS,
                timeout=7,
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
            progress_urls = list(dict.fromkeys(progress_urls))

            internal_id = str(uuid.uuid4())
            entry = {
                "job_id": internal_id,
                "remote_id": remote_id,
                "cache_key": cache_key,
                "status": "processing",
                "progress": 25,
                "download_url": None,
                "progress_urls": progress_urls,
                "created_at": time.time(),
                "error": None,
                "has_trim": has_trim,
                "start_sec": start_sec,
                "end_sec": end_sec,
                "mode": mode,
                "format_type": format_type,
                "quality": quality,
                "is_muted": is_muted,
                "filename": filename,
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
    """Fires 720p MP4 and MP3 320k simultaneously in parallel threads on Fetch!"""
    threading.Thread(
        target=start_or_get_cloud_job, args=(url, "video", "mp4", "720"), daemon=True
    ).start()
    threading.Thread(
        target=start_or_get_cloud_job, args=(url, "audio", "mp3", "320"), daemon=True
    ).start()


# =====================================================================
# 3. UNIVERSAL MULTI-PLATFORM ENGINE (Instagram, Pinterest, X, FB, Reddit, TikTok, etc.)
# =====================================================================
def run_universal_platform_download(
    url: str,
    job_dir: Path,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool,
    start_time: Optional[str],
    end_time: Optional[str],
) -> Path:
    raw_template = str(job_dir / "raw_media.%(ext)s")
    start_sec = time_str_to_seconds(start_time)
    end_sec = time_str_to_seconds(end_time)
    has_trim = start_sec is not None and end_sec is not None and end_sec > start_sec

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-check-certificates",
        "--geo-bypass",
        "--impersonate", "chrome",
        "--concurrent-fragments", "10",
        "-o", raw_template,
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

    if is_muted and mode == "video" and not has_trim:
        cmd += ["--postprocessor-args", "ffmpeg:-an"]

    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        err_lines = [line for line in (proc.stderr or "").splitlines() if line.strip()]
        raise RuntimeError(err_lines[-1] if err_lines else "Platform stream extraction failed")

    files = [f for f in job_dir.glob("raw_media.*") if not f.name.endswith(".part")]
    if not files or files[0].stat().st_size == 0:
        raise RuntimeError("Downloaded file was empty")

    downloaded_raw = files[0]

    # If user requested a specific clip (e.g. 1s or 1-2m), cut the exact segment with FFmpeg!
    if has_trim:
        trimmed_out = job_dir / f"trimmed_output.{format_type}"
        return cut_remote_or_local_media_exact(
            str(downloaded_raw),
            trimmed_out,
            start_sec,
            end_sec,
            mode,
            format_type,
            quality,
            is_muted,
        )

    return downloaded_raw


# =====================================================================
# 4. USER REVIEWS & PROBLEM REPORTER STORAGE
# =====================================================================
DEFAULT_FEEDBACK = [
    {
        "id": "seed-1",
        "name": "Aarav Edits",
        "type": "review",
        "platform": "YOUTUBE",
        "rating": 5,
        "message": "720p video and clip trimmer work super smoothly for my shorts editing!",
        "status": "Verified User",
        "time": "Today",
    },
    {
        "id": "seed-2",
        "name": "Riya Designs",
        "type": "review",
        "platform": "PINTEREST",
        "rating": 5,
        "message": "Finally able to download Pinterest video pins and Instagram reels in one place without watermarks.",
        "status": "Verified User",
        "time": "Today",
    },
]


def load_feedback_list() -> List[dict]:
    with FEEDBACK_LOCK:
        if not FEEDBACK_FILE.exists():
            FEEDBACK_FILE.write_text(json.dumps(DEFAULT_FEEDBACK), encoding="utf-8")
            return list(DEFAULT_FEEDBACK)
        try:
            data = json.loads(FEEDBACK_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else list(DEFAULT_FEEDBACK)
        except Exception:
            return list(DEFAULT_FEEDBACK)


def save_feedback_item(item: dict) -> List[dict]:
    with FEEDBACK_LOCK:
        try:
            current = (
                json.loads(FEEDBACK_FILE.read_text(encoding="utf-8"))
                if FEEDBACK_FILE.exists()
                else list(DEFAULT_FEEDBACK)
            )
            if not isinstance(current, list):
                current = list(DEFAULT_FEEDBACK)
        except Exception:
            current = list(DEFAULT_FEEDBACK)

        current.insert(0, item)
        current = current[:60]
        FEEDBACK_FILE.write_text(json.dumps(current), encoding="utf-8")
        return current


# =====================================================================
# 5. API ROUTES
# =====================================================================
@app.api_route("/health", methods=["GET", "HEAD"])
def health_check():
    """Dedicated lightweight keep-alive endpoint for UptimeRobot (Zero Ad Traffic)"""
    return {"status": "ok", "service": "GlobiFree.Pro"}


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

    # Pre-warm YouTube defaults in background without affecting other platforms
    if info.get("platform") == "YOUTUBE":
        prewarm_youtube_defaults(target_url)

    return info


@app.get("/api/thumbnail")
def download_hd_thumbnail(url: str = Query(...), title: str = Query("GlobiFree_Thumbnail")):
    """1-Click HD Thumbnail Downloader for YouTube, Instagram, Pinterest, etc."""
    info = fast_fetch_info(url)
    thumb_url = info.get("hd_thumbnail") or info.get("thumbnail")
    if not thumb_url:
        raise HTTPException(status_code=404, detail="No thumbnail found for this link.")

    safe_title = ascii_safe_filename(title)
    thumb_dir = TEMP_DIR / f"thumb_{uuid.uuid4()}"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumb_dir / f"{safe_title}.jpg"

    try:
        resp = HTTP_SESSION.get(thumb_url, timeout=8)
        if resp.status_code != 200 and info.get("thumbnail"):
            resp = HTTP_SESSION.get(info["thumbnail"], timeout=8)
        thumb_path.write_bytes(resp.content)
        return FileResponse(
            path=str(thumb_path),
            filename=f"{safe_title}_HD_Thumbnail.jpg",
            media_type="image/jpeg",
            headers={"Content-Disposition": f'attachment; filename="{safe_title}_HD_Thumbnail.jpg"'},
        )
    except Exception as e:
        cleanup_dir(thumb_dir)
        raise HTTPException(status_code=500, detail=f"Thumbnail download failed: {str(e)}")


@app.post("/api/prewarm")
def api_prewarm(
    url: str = Form(...),
    mode: str = Form("video"),
    format_type: str = Form("mp4"),
    quality: str = Form("720"),
):
    if detect_platform_name(url) == "YOUTUBE":
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
    platform = detect_platform_name(url)
    is_yt = platform == "YOUTUBE"

    start_sec = time_str_to_seconds(start_time)
    end_sec = time_str_to_seconds(end_time)
    has_trim = start_sec is not None and end_sec is not None and end_sec > start_sec

    if not custom_title:
        try:
            meta = fast_fetch_info(url)
            custom_title = meta.get("title")
        except Exception:
            custom_title = "GlobiFree_Media"

    safe_header_title = ascii_safe_filename(custom_title or "GlobiFree_Media")
    if has_trim:
        safe_header_title = f"{safe_header_title}_Clip_{int(start_sec)}s_{int(end_sec)}s"
    final_filename = f"{safe_header_title}.{format_type}"

    # 1. YOUTUBE TURBO CLOUD ENGINE (Supports both Full Video & Exact FFmpeg Clip Trimming)
    if is_yt and (not is_muted or has_trim):
        entry = start_or_get_cloud_job(
            url, mode, format_type, quality, start_time, end_time, is_muted, final_filename
        )
        if entry:
            for _ in range(9):
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
                time.sleep(0.2)

            if entry["status"] == "processing":
                return JSONResponse(
                    {
                        "status": "processing",
                        "job_id": entry["job_id"],
                        "progress": entry.get("progress", 40),
                        "filename": final_filename,
                    }
                )

    # 2. UNIVERSAL MULTI-PLATFORM ENGINE (Instagram, Pinterest, X, FB, Reddit, TikTok, etc.)
    job_id = str(uuid.uuid4())
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        downloaded_file = run_universal_platform_download(
            url, job_dir, mode, format_type, quality, is_muted, start_time, end_time
        )
    except Exception as e:
        cleanup_dir(job_dir)
        raise HTTPException(
            status_code=500,
            detail=f"{platform.title()} download failed: {str(e)}. You can report this link below!",
        )

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

    files = [f for f in job_dir.glob("*") if f.is_file() and not f.name.startswith("raw_media")]
    if not files:
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


@app.get("/api/feedback")
def get_feedback():
    return {"items": load_feedback_list()}


@app.post("/api/feedback")
def post_feedback(
    name: str = Form("Anonymous User"),
    feedback_type: str = Form("review"),
    platform: str = Form("YOUTUBE"),
    rating: int = Form(5),
    media_url: Optional[str] = Form(None),
    message: str = Form(...),
):
    clean_msg = message.strip()
    if not clean_msg:
        raise HTTPException(status_code=400, detail="Please write a message or issue description.")

    item = {
        "id": str(uuid.uuid4())[:8],
        "name": (name.strip() or "Anonymous User")[:40],
        "type": "issue" if feedback_type == "issue" else "review",
        "platform": platform.strip().upper()[:20],
        "rating": max(1, min(5, int(rating))),
        "media_url": (media_url or "").strip()[:180],
        "message": clean_msg[:400],
        "status": "Logged for Admin Check" if feedback_type == "issue" else "Verified Feedback",
        "time": time.strftime("%d %b, %I:%M %p"),
    }
    updated = save_feedback_item(item)
    return {"status": "ok", "items": updated}
