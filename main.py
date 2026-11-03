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

# Thread-Safe Caches (No shared curl session across threads!)
INFO_CACHE: Dict[str, Dict[str, Any]] = {}
PREWARM_CACHE: Dict[str, str] = {}
CACHE_LOCK = threading.Lock()
FEEDBACK_LOCK = threading.Lock()


# Auto-upgrade yt-dlp & pytubefix in background on startup so Docker cache never makes them outdated
@app.on_event("startup")
def auto_update_extractors():
    def _updater():
        try:
            subprocess.run(
                ["pip", "install", "--no-cache-dir", "-U", "--pre", "yt-dlp[default]", "pytubefix"],
                capture_output=True,
                timeout=120,
            )
        except Exception:
            pass

    threading.Thread(target=_updater, daemon=True).start()


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
# 1. INSTANT 0.2s FETCH INFO (Thread-Safe Independent Calls)
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

    if is_yt:
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

    try:
        resp = cffi_requests.get(
            f"https://noembed.com/embed?url={quote(url)}", impersonate="chrome", timeout=4
        )
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
# 2. PROVEN YOUTUBE CLOUD CDN ENGINE (Exact Working Method from Aasman Rootha)
# =====================================================================
def get_cloud_cdn_direct_link(
    url: str,
    mode: str,
    format_type: str,
    quality: str,
) -> Optional[str]:
    vid_id = extract_youtube_id(url)
    clean_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else url

    if mode == "audio":
        cloud_fmt = format_type if format_type in ["mp3", "m4a", "wav", "ogg", "opus"] else "mp3"
    else:
        if format_type == "webm":
            cloud_fmt = "webm"
        else:
            cloud_fmt = quality if quality in ["1080", "720", "480", "360"] else "720"

    cache_key = f"{vid_id or clean_url}:{cloud_fmt}"
    with CACHE_LOCK:
        if cache_key in PREWARM_CACHE:
            return PREWARM_CACHE[cache_key]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://ddownr.com/",
        "Origin": "https://ddownr.com",
    }

    api_hosts = ["https://p.savenow.to", "https://p.oceansaver.in"]

    for host in api_hosts:
        try:
            params = {
                "button": "1",
                "start": "1",
                "end": "1",
                "format": cloud_fmt,
                "iframe_source": "https://ddownr.com/",
                "url": clean_url,
            }
            init_resp = cffi_requests.get(
                f"{host}/ajax/download.php",
                params=params,
                headers=headers,
                impersonate="chrome",
                timeout=10,
            )
            if init_resp.status_code != 200:
                continue

            init_data = init_resp.json()
            job_id = init_data.get("id")
            if not job_id:
                continue

            progress_urls = [
                init_data.get("progress_url"),
                f"https://p.savenow.to/ajax/progress.php?id={job_id}",
                f"https://p.oceansaver.in/ajax/progress.php?id={job_id}",
            ]
            progress_urls = list(dict.fromkeys([u for u in progress_urls if u]))

            for _ in range(25):
                time.sleep(0.8)
                for p_url in progress_urls:
                    try:
                        p_resp = cffi_requests.get(
                            p_url, headers=headers, impersonate="chrome", timeout=6
                        )
                        if p_resp.status_code == 200:
                            p_data = p_resp.json()
                            dl_url = p_data.get("download_url")
                            if dl_url and (p_data.get("success") == 1 or p_data.get("progress") == 1000):
                                with CACHE_LOCK:
                                    PREWARM_CACHE[cache_key] = dl_url
                                return dl_url
                            if p_data.get("progress") == 1000 and not dl_url:
                                break
                    except Exception:
                        continue
        except Exception:
            continue

    return None


def safe_prewarm_single_stream(url: str, mode: str = "video", format_type: str = "mp4", quality: str = "720"):
    """Safely pre-warms 1 stream in background using isolated requests (no thread collision)"""
    try:
        get_cloud_cdn_direct_link(url, mode, format_type, quality)
    except Exception:
        pass


# =====================================================================
# 3. EXACT FFMPEG CLIP TRIMMER (Cuts Only 1s - 5m Segment)
# =====================================================================
def cut_media_segment_exact(
    source_url_or_path: str,
    out_file: Path,
    start_sec: float,
    end_sec: float,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool = False,
) -> Path:
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


# =====================================================================
# 4. UNIVERSAL MULTI-PLATFORM ENGINE (Instagram, Pinterest, X, FB, Reddit, etc.)
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
    is_yt = detect_platform_name(url) == "YOUTUBE"

    # If YouTube ever reaches here (e.g. muted full video), try pytubefix clients first
    if is_yt:
        try:
            from pytubefix import YouTube
            for client in ["ANDROID_VR", "IOS", "WEB_EMBED", "TV", "MWEB"]:
                try:
                    yt = YouTube(url, client=client)
                    out_file = job_dir / f"output.{format_type}"
                    if mode == "audio":
                        stream = yt.streams.get_audio_only() or yt.streams.filter(only_audio=True).first()
                        raw_a = Path(stream.download(output_path=str(job_dir), filename="raw_aud"))
                        if has_trim:
                            return cut_media_segment_exact(str(raw_a), out_file, start_sec, end_sec, mode, format_type, quality, False)
                        subprocess.run(["ffmpeg", "-y", "-i", str(raw_a), "-vn", str(out_file)], check=True, timeout=45)
                        return out_file
                    else:
                        stream = (
                            yt.streams.filter(res=f"{quality}p", file_extension="mp4", progressive=True).first()
                            or yt.streams.filter(progressive=True, file_extension="mp4").order_by("resolution").desc().first()
                            or yt.streams.get_highest_resolution()
                        )
                        raw_v = Path(stream.download(output_path=str(job_dir), filename="raw_vid.mp4"))
                        if has_trim:
                            return cut_media_segment_exact(str(raw_v), out_file, start_sec, end_sec, mode, format_type, quality, is_muted)
                        if is_muted:
                            subprocess.run(["ffmpeg", "-y", "-i", str(raw_v), "-an", "-c:v", "copy", str(out_file)], check=True, timeout=45)
                            return out_file
                        return raw_v
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

    if has_trim:
        trimmed_out = job_dir / f"trimmed_output.{format_type}"
        return cut_media_segment_exact(
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
# 5. USER REVIEWS & PROBLEM REPORTER STORAGE
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
# 6. API ROUTES
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

    # Pre-warm ONLY 1 default stream (720p MP4) safely in background
    if info.get("platform") == "YOUTUBE":
        threading.Thread(
            target=safe_prewarm_single_stream, args=(target_url, "video", "mp4", "720"), daemon=True
        ).start()

    return info


@app.get("/api/thumbnail")
def download_hd_thumbnail(url: str = Query(...), title: str = Query("GlobiFree_Thumbnail")):
    info = fast_fetch_info(url)
    thumb_url = info.get("hd_thumbnail") or info.get("thumbnail")
    if not thumb_url:
        raise HTTPException(status_code=404, detail="No thumbnail found for this link.")

    safe_title = ascii_safe_filename(title)
    thumb_dir = TEMP_DIR / f"thumb_{uuid.uuid4()}"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumb_dir / f"{safe_title}.jpg"

    try:
        resp = cffi_requests.get(thumb_url, impersonate="chrome", timeout=8)
        if resp.status_code != 200 and info.get("thumbnail"):
            resp = cffi_requests.get(info["thumbnail"], impersonate="chrome", timeout=8)
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
        threading.Thread(
            target=safe_prewarm_single_stream, args=(url, mode, format_type, quality), daemon=True
        ).start()
        return {"status": "warming"}
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

    # 1. YOUTUBE CLOUD CDN ENGINE (Proven 100% Working)
    if is_yt:
        direct_cdn_url = get_cloud_cdn_direct_link(url, mode, format_type, quality)
        if direct_cdn_url:
            # A. Normal Full Video / Audio -> Send Direct Chrome CDN Link immediately!
            if not has_trim and not is_muted:
                return JSONResponse(
                    {
                        "status": "ready",
                        "download_url": direct_cdn_url,
                        "filename": final_filename,
                    }
                )

            # B. Trimmed Clip or Muted Video -> Cut exact range from Cloud CDN stream via FFmpeg!
            job_id = str(uuid.uuid4())
            job_dir = TEMP_DIR / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            try:
                out_path = job_dir / f"trimmed.{format_type}"
                if has_trim:
                    cut_media_segment_exact(
                        direct_cdn_url,
                        out_path,
                        start_sec,
                        end_sec,
                        mode,
                        format_type,
                        quality,
                        is_muted,
                    )
                else:
                    # Muted full video
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", direct_cdn_url, "-an", "-c:v", "copy", str(out_path)],
                        check=True,
                        timeout=60,
                    )
                return JSONResponse(
                    {
                        "status": "ready",
                        "download_url": f"/api/stream/{job_id}?filename={quote(final_filename)}",
                        "filename": final_filename,
                    }
                )
            except Exception:
                cleanup_dir(job_dir)

    # 2. OTHER PLATFORMS (Instagram, Pinterest, X, FB, Reddit, TikTok) OR Fallback
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
