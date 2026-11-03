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

INFO_CACHE: Dict[str, Dict[str, Any]] = {}
PREWARM_CACHE: Dict[str, Dict[str, Any]] = {}
JOBS: Dict[str, Dict[str, Any]] = {}

CACHE_LOCK = threading.Lock()
JOBS_LOCK = threading.Lock()
FEEDBACK_LOCK = threading.Lock()


# =====================================================================
# HELPER UTILITIES & AUTO GARBAGE COLLECTOR
# =====================================================================
def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", str(name or "")).strip()
    return cleaned[:100] if cleaned else "GlobiFree_Media"


def ascii_safe_filename(name: str) -> str:
    cleaned = sanitize_filename(name)
    ascii_name = cleaned.encode("ascii", "ignore").decode("ascii").strip()
    ascii_name = re.sub(r"\s+", " ", ascii_name)
    return ascii_name if len(ascii_name) > 2 else "GlobiFree_Media"


def cleanup_dir(path: Path):
    try:
        if path and path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def cleanup_old_temp_files(max_age_sec: int = 900):
    now = time.time()
    try:
        for item in TEMP_DIR.iterdir():
            if item.is_dir():
                try:
                    if now - item.stat().st_mtime > max_age_sec:
                        shutil.rmtree(item, ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass

    with JOBS_LOCK:
        expired_jobs = [jid for jid, jdata in JOBS.items() if now - jdata.get("ts", 0) > max_age_sec]
        for jid in expired_jobs:
            JOBS.pop(jid, None)


def format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "HD Ready"
    try:
        seconds = int(float(seconds))
        hrs, rem = divmod(seconds, 3600)
        mins, secs = divmod(rem, 60)
        if hrs > 0:
            return f"{hrs:02d}:{mins:02d}:{secs:02d}"
        return f"{mins:02d}:{secs:02d}"
    except Exception:
        return "HD Ready"


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
        r"(?:v=|\/videos\/|embed\/|youtu\.be\/|\/v\/|\/e\/|watch\?v=|&v=|\/shorts\/|\/live\/)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def update_job(job_id: str, **kwargs):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)
            JOBS[job_id]["ts"] = time.time()


# =====================================================================
# 1. FAST & CRASH-PROOF INFO FETCH
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

        try:
            from pytubefix import YouTube
            yt = YouTube(clean_yt_url, use_po_token=True)
            duration = yt.length or 0
            thumb = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg" if vid_id else yt.thumbnail_url
            result = {
                "title": yt.title or "YouTube Video",
                "uploader": yt.author or "YouTube Creator",
                "duration": duration,
                "duration_string": format_duration(duration),
                "thumbnail": thumb,
                "hd_thumbnail": f"https://i.ytimg.com/vi/{vid_id}/maxresdefault.jpg" if vid_id else thumb,
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
    ]
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]
    cmd.append(url)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=14)
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

    fallback_thumb = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg" if vid_id else ""
    return {
        "title": f"{platform.title()} Media_{int(time.time()) % 10000}",
        "uploader": f"{platform.title()} Creator",
        "duration": 0,
        "duration_string": "HD Ready",
        "thumbnail": fallback_thumb,
        "hd_thumbnail": fallback_thumb,
        "platform": platform,
        "max_quality": "HD READY",
    }


# =====================================================================
# 2. FFMPEG TRIMMING & AUDIO/VIDEO PROCESSING
# =====================================================================
def cut_media_segment_exact(
    source_url: str,
    out_file: Path,
    start_sec: float,
    end_sec: float,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool = False,
) -> Path:
    clip_duration = max(0.5, round(end_sec - start_sec, 2))
    cmd = ["ffmpeg", "-y"]

    if str(source_url).startswith("http"):
        cmd += [
            "-user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        ]

    cmd += [
        "-ss", str(start_sec),
        "-i", str(source_url),
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
        cmd += [
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-movflags", "+faststart",
        ]
        if not is_muted:
            cmd += ["-c:a", "aac", "-b:a", "128k"]

    cmd.append(str(out_file))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if proc.returncode == 0 and out_file.exists() and out_file.stat().st_size > 0:
        return out_file

    fallback_cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(start_sec),
        "-i", str(source_url),
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
# 3. YOUTUBE 3-LAYER TRIPLE ENGINE (Cloud CDN -> Pytubefix -> yt-dlp)
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
        cached = PREWARM_CACHE.get(cache_key)
        if cached and (time.time() - cached.get("ts", 0) < 900):
            return cached.get("url")

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
                timeout=6,
            )
            if init_resp.status_code != 200:
                continue

            init_data = init_resp.json()
            job_id = init_data.get("id")
            if not job_id:
                continue

            progress_url = init_data.get("progress_url") or f"{host}/ajax/progress.php?id={job_id}"

            for _ in range(16):
                time.sleep(0.75)
                try:
                    p_resp = cffi_requests.get(
                        progress_url, headers=headers, impersonate="chrome", timeout=4
                    )
                    if p_resp.status_code == 200:
                        p_data = p_resp.json()
                        dl_url = p_data.get("download_url")
                        if dl_url and str(dl_url).startswith("http"):
                            with CACHE_LOCK:
                                PREWARM_CACHE[cache_key] = {"url": dl_url, "ts": time.time()}
                            return dl_url
                        if p_data.get("text") and "error" in str(p_data.get("text")).lower():
                            break
                except Exception:
                    continue
        except Exception:
            continue

    return None


def download_youtube_with_pytubefix(
    url: str,
    job_dir: Path,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool,
) -> Path:
    from pytubefix import YouTube

    vid_id = extract_youtube_id(url)
    clean_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else url
    yt = YouTube(clean_url, use_po_token=True)

    if mode == "audio":
        audio_stream = yt.streams.filter(only_audio=True).order_by("abr").desc().first()
        if not audio_stream:
            raise RuntimeError("No audio stream found via pytubefix")
        raw_audio = Path(audio_stream.download(output_path=str(job_dir), filename="raw_audio.m4a"))
        out_audio = job_dir / f"final_media.{format_type}"
        if format_type == "m4a":
            raw_audio.rename(out_audio)
            return out_audio
        bitrate = f"{quality}k" if str(quality).isdigit() else "192k"
        cmd = ["ffmpeg", "-y", "-i", str(raw_audio), "-vn"]
        if format_type == "mp3":
            cmd += ["-c:a", "libmp3lame", "-b:a", bitrate]
        elif format_type == "wav":
            cmd += ["-c:a", "pcm_s16le"]
        cmd.append(str(out_audio))
        subprocess.run(cmd, check=True, timeout=90)
        return out_audio

    target_res = f"{quality}p"
    prog_stream = (
        yt.streams.filter(progressive=True, file_extension="mp4", res=target_res).first()
        or yt.streams.filter(progressive=True, file_extension="mp4").order_by("resolution").desc().first()
    )

    if (quality == "1080" or is_muted) or not prog_stream:
        vid_stream = (
            yt.streams.filter(adaptive=True, only_video=True, file_extension="mp4", res=target_res).first()
            or yt.streams.filter(adaptive=True, only_video=True, file_extension="mp4").order_by("resolution").desc().first()
            or prog_stream
        )
        if not vid_stream:
            raise RuntimeError("No video stream found via pytubefix")

        raw_vid = Path(vid_stream.download(output_path=str(job_dir), filename="raw_video.mp4"))
        out_vid = job_dir / f"final_media.{format_type}"

        if is_muted or vid_stream.is_progressive:
            if is_muted and vid_stream.is_progressive:
                subprocess.run(["ffmpeg", "-y", "-i", str(raw_vid), "-an", "-c:v", "copy", str(out_vid)], check=True, timeout=60)
                return out_vid
            raw_vid.rename(out_vid)
            return out_vid

        aud_stream = yt.streams.filter(only_audio=True).order_by("abr").desc().first()
        if aud_stream:
            raw_aud = Path(aud_stream.download(output_path=str(job_dir), filename="raw_audio.m4a"))
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(raw_vid), "-i", str(raw_aud), "-c:v", "copy", "-c:a", "aac", str(out_vid)],
                check=True,
                timeout=90,
            )
            return out_vid

    if not prog_stream:
        raise RuntimeError("No playable YouTube stream found")

    raw_file = Path(prog_stream.download(output_path=str(job_dir), filename="raw_prog.mp4"))
    out_file = job_dir / f"final_media.{format_type}"
    if is_muted:
        subprocess.run(["ffmpeg", "-y", "-i", str(raw_file), "-an", "-c:v", "copy", str(out_file)], check=True, timeout=60)
        return out_file

    raw_file.rename(out_file)
    return out_file


# =====================================================================
# 4. UNIVERSAL PLATFORM DOWNLOAD (Instagram, Pinterest, X, FB, Reddit, YT Fallback)
# =====================================================================
def run_universal_platform_download(
    url: str,
    job_dir: Path,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool,
    is_yt: bool = False,
) -> Path:
    raw_template = str(job_dir / "raw_media.%(ext)s")

    base_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-check-certificates",
        "--geo-bypass",
        "--concurrent-fragments", "8",
        "-o", raw_template,
    ]
    if is_yt:
         base_cmd += ["--extractor-args", "youtube:player_client=default,ios,android"]
    if COOKIES_FILE.exists():
        base_cmd += ["--cookies", str(COOKIES_FILE)]

    if mode == "audio":
        cmd = base_cmd + [
            "-f", "bestaudio/best",
            "-x",
            "--audio-format", format_type,
            "--audio-quality", f"{quality}K" if str(quality).isdigit() else "0",
            url,
        ]
    else:
        q_filter = f"[height<={quality}]" if str(quality).isdigit() else ""
        fmt = (
            f"bestvideo{q_filter}/best{q_filter}/best"
            if is_muted
            else f"bestvideo{q_filter}+bestaudio/best{q_filter}/best"
        )
        cmd = base_cmd + ["-f", fmt, "--merge-output-format", format_type, url]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=95)

    if proc.returncode != 0 and mode != "audio":
        fallback_cmd = base_cmd + ["-f", "best", "--merge-output-format", format_type, url]
        proc = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=90)

    if proc.returncode != 0:
        err_lines = [line for line in (proc.stderr or "").splitlines() if line.strip()]
        raise RuntimeError(err_lines[-1] if err_lines else "Platform media extraction failed")

    files = [f for f in job_dir.glob("raw_media.*") if not f.name.endswith(".part") and not f.name.endswith(".ytdl")]
    if not files or files[0].stat().st_size == 0:
        raise RuntimeError("Downloaded file was empty")

    downloaded_raw = files[0]

    if is_muted and mode == "video":
        muted_out = job_dir / f"muted_output.{downloaded_raw.suffix.lstrip('.') or format_type}"
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(downloaded_raw), "-an", "-c:v", "copy", str(muted_out)],
            check=True,
            timeout=60,
        )
        return muted_out

    return downloaded_raw


# =====================================================================
# 5. BACKGROUND JOB WORKER (Prevents HTTP Timeout & Worker Crashes)
# =====================================================================
def process_download_job(
    job_id: str,
    url: str,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool,
    safe_header_title: str,
    start_sec: Optional[float],
    end_sec: Optional[float],
):
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    platform = detect_platform_name(url)
    is_yt = platform == "YOUTUBE"
    has_trim = start_sec is not None and end_sec is not None and end_sec > start_sec
    final_filename = f"{safe_header_title}.{format_type}"

    try:
        update_job(job_id, progress=40)

        # ------------------- YOUTUBE FLOW -------------------
        if is_yt:
            direct_cdn_url = get_cloud_cdn_direct_link(url, mode, format_type, quality)
            if direct_cdn_url:
                if not has_trim and not is_muted:
                    cleanup_dir(job_dir)
                    update_job(
                        job_id,
                        status="ready",
                        progress=100,
                        download_url=direct_cdn_url,
                        filename=final_filename,
                    )
                    return

                try:
                    update_job(job_id, progress=85)
                    out_path = job_dir / f"final_output.{format_type}"
                    if has_trim:
                        cut_media_segment_exact(
                            direct_cdn_url, out_path, start_sec, end_sec, mode, format_type, quality, is_muted
                        )
                    else:
                        subprocess.run(
                            [
                                "ffmpeg", "-y",
                                "-user_agent", "Mozilla/5.0",
                                "-i", direct_cdn_url,
                                "-an", "-c:v", "copy", str(out_path),
                            ],
                            check=True,
                            timeout=60,
                        )
                    update_job(
                        job_id,
                        status="ready",
                        progress=100,
                        download_url=f"/api/stream/{job_id}?filename={quote(final_filename)}",
                        filename=final_filename,
                    )
                    return
                except Exception:
                    pass

            downloaded_file = None
            try:
                update_job(job_id, progress=60)
                downloaded_file = download_youtube_with_pytubefix(
                    url, job_dir, mode, format_type, quality, is_muted and not has_trim
                )
            except Exception:
                update_job(job_id, progress=75)
                downloaded_file = run_universal_platform_download(
                    url, job_dir, mode, format_type, quality, is_muted and not has_trim, is_yt=True
                )

            if has_trim:
                update_job(job_id, progress=90)
                trimmed_out = job_dir / f"trimmed_output.{format_type}"
                downloaded_file = cut_media_segment_exact(
                    str(downloaded_file), trimmed_out, start_sec, end_sec, mode, format_type, quality, is_muted
                )

            final_ext = downloaded_file.suffix.lstrip(".") or format_type
            local_filename = f"{safe_header_title}.{final_ext}"
            update_job(
                job_id,
                status="ready",
                progress=100,
                download_url=f"/api/stream/{job_id}?filename={quote(local_filename)}",
                filename=local_filename,
            )
            return

        # ------------------- OTHER PLATFORMS FLOW -------------------
        update_job(job_id, progress=55)
        downloaded_file = run_universal_platform_download(
            url, job_dir, mode, format_type, quality, is_muted and not has_trim, is_yt=False
        )

        if has_trim:
            update_job(job_id, progress=88)
            trimmed_out = job_dir / f"trimmed_output.{format_type}"
            downloaded_file = cut_media_segment_exact(
                str(downloaded_file), trimmed_out, start_sec, end_sec, mode, format_type, quality, is_muted
            )

        final_ext = downloaded_file.suffix.lstrip(".") or format_type
        local_filename = f"{safe_header_title}.{final_ext}"

        update_job(
            job_id,
            status="ready",
            progress=100,
            download_url=f"/api/stream/{job_id}?filename={quote(local_filename)}",
            filename=local_filename,
        )

    except Exception as e:
        cleanup_dir(job_dir)
        update_job(job_id, status="error", error=f"{platform.title()} download failed: {str(e)}")


# =====================================================================
# 6. REVIEWS & FEEDBACK STORAGE
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
        FEEDBACK_FILE.write_text(json.dumps(current[:60]), encoding="utf-8")
        return current


# =====================================================================
# 7. API ROUTES
# =====================================================================
@app.api_route("/health", methods=["GET", "HEAD"])
def health_check():
    return {"status": "ok", "service": "GlobiFree.Pro"}


@app.get("/", response_class=HTMLResponse)
def serve_home():
    html_path = Path("index.html")
    return html_path.read_text(encoding="utf-8") if html_path.exists() else "<h1>index.html not found</h1>"


@app.api_route("/api/info", methods=["GET", "POST"])
def api_info(
    url_form: Optional[str] = Form(None, alias="url"),
    url_query: Optional[str] = Query(None, alias="url"),
):
    target_url = (url_form or url_query or "").strip()
    if not target_url:
        raise HTTPException(status_code=400, detail="URL is required.")
    return fast_fetch_info(target_url)


@app.get("/api/thumbnail")
def download_hd_thumbnail(
    background_tasks: BackgroundTasks,
    url: str = Query(...),
    title: str = Query("GlobiFree_Thumbnail"),
):
    info = fast_fetch_info(url)
    thumb_url = info.get("hd_thumbnail") or info.get("thumbnail")
    if not thumb_url:
        raise HTTPException(status_code=404, detail="Thumbnail not found.")

    safe_title = ascii_safe_filename(title)
    thumb_dir = TEMP_DIR / f"thumb_{uuid.uuid4()}"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumb_dir / f"{safe_title}.jpg"

    try:
        resp = cffi_requests.get(thumb_url, impersonate="chrome", timeout=8)
        thumb_path.write_bytes(resp.content)
        background_tasks.add_task(cleanup_dir, thumb_dir)
        return FileResponse(
            path=str(thumb_path),
            filename=f"{safe_title}_HD_Thumbnail.jpg",
            media_type="image/jpeg",
        )
    except Exception as e:
        cleanup_dir(thumb_dir)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/prewarm")
def api_prewarm(
    url: str = Form(...),
    mode: str = Form("video"),
    format_type: str = Form("mp4"),
    quality: str = Form("720"),
):
    if detect_platform_name(url) == "YOUTUBE":
        threading.Thread(
            target=get_cloud_cdn_direct_link,
            args=(url, mode, format_type, quality),
            daemon=True,
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
    cleanup_old_temp_files()

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

    if is_yt and not has_trim and not is_muted:
        vid_id = extract_youtube_id(url)
        clean_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else url
        cloud_fmt = (
            (format_type if format_type in ["mp3", "m4a", "wav", "ogg", "opus"] else "mp3")
            if mode == "audio"
            else ("webm" if format_type == "webm" else (quality if quality in ["1080", "720", "480", "360"] else "720"))
        )
        cache_key = f"{vid_id or clean_url}:{cloud_fmt}"
        with CACHE_LOCK:
            cached = PREWARM_CACHE.get(cache_key)
            if cached and (time.time() - cached.get("ts", 0) < 900):
                return JSONResponse({
                    "status": "ready",
                    "download_url": cached["url"],
                    "filename": final_filename,
                })

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "processing",
            "progress": 30,
            "filename": final_filename,
            "download_url": None,
            "error": None,
            "ts": time.time(),
        }

    threading.Thread(
        target=process_download_job,
        args=(job_id, url, mode, format_type, quality, is_muted, safe_header_title, start_sec, end_sec),
        daemon=True,
    ).start()

    return JSONResponse({
        "status": "processing",
        "job_id": job_id,
        "progress": 30,
        "filename": final_filename,
    })


@app.get("/api/job_status")
def api_job_status(job_id: str = Query(...), filename: Optional[str] = Query(None)):
    with JOBS_LOCK:
        job = JOBS.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Download session expired. Please click download again.")

    if job["status"] == "error":
        raise HTTPException(status_code=500, detail=job.get("error") or "Download failed.")

    if job["status"] == "ready":
        return {
            "status": "ready",
            "download_url": job["download_url"],
            "filename": job.get("filename") or filename or "GlobiFree_Media.mp4",
            "progress": 100,
        }

    return {
        "status": "processing",
        "job_id": job_id,
        "progress": job.get("progress", 55),
        "filename": job.get("filename") or filename or "GlobiFree_Media.mp4",
    }


@app.get("/api/stream/{job_id}")
def stream_prepared_file(
    job_id: str,
    background_tasks: BackgroundTasks,
    filename: str = Query("GlobiFree_Media.mp4"),
):
    job_dir = TEMP_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="File expired.")

    preferred = [
        f for f in job_dir.glob("*")
        if f.is_file() and (f.name.startswith("trimmed_") or f.name.startswith("muted_") or f.name.startswith("final_"))
    ]
    files = preferred or [f for f in job_dir.glob("*") if f.is_file() and not f.name.endswith(".part")]

    if not files:
        cleanup_dir(job_dir)
        raise HTTPException(status_code=404, detail="File not found.")

    target_file = files[0]
    safe_name = ascii_safe_filename(Path(filename).stem) + target_file.suffix
    background_tasks.add_task(cleanup_dir, job_dir)

    return FileResponse(path=str(target_file), filename=safe_name, media_type="application/octet-stream")


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
    item = {
        "id": str(uuid.uuid4())[:8],
        "name": (name.strip() or "Anonymous User")[:40],
        "type": "issue" if feedback_type == "issue" else "review",
        "platform": platform.strip().upper()[:20],
        "rating": max(1, min(5, int(rating))),
        "media_url": (media_url or "").strip()[:180],
        "message": message.strip()[:400],
        "status": "Logged for Admin Check" if feedback_type == "issue" else "Verified Feedback",
        "time": time.strftime("%d %b, %I:%M %p"),
    }
    return {"status": "ok", "items": save_feedback_item(item)}
