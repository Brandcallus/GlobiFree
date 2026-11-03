import os
import re
import sys
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
CDN_KEY_LOCKS: Dict[str, threading.Lock] = {}

CACHE_LOCK = threading.Lock()
JOBS_LOCK = threading.Lock()
FEEDBACK_LOCK = threading.Lock()


# =====================================================================
# 0. AUTO-SELF-UPDATING ENGINE (Bypasses Render Docker Layer Cache)
# =====================================================================
def force_upgrade_extractors():
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "-q",
                "-U",
                "--pre",
                "yt-dlp[default]",
                "pytubefix",
            ],
            capture_output=True,
            timeout=120,
        )
    except Exception:
        pass


# Start background upgrade immediately when container boots
threading.Thread(target=force_upgrade_extractors, daemon=True).start()


# =====================================================================
# HELPER UTILITIES & GARBAGE COLLECTION
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


def update_job(job_id: Optional[str], **kwargs):
    if not job_id:
        return
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)
            JOBS[job_id]["ts"] = time.time()


def get_cdn_lock(cache_key: str) -> threading.Lock:
    with CACHE_LOCK:
        if cache_key not in CDN_KEY_LOCKS:
            CDN_KEY_LOCKS[cache_key] = threading.Lock()
        return CDN_KEY_LOCKS[cache_key]


# =====================================================================
# 1. FAST INFO FETCH (Supports Videos & Photo Posts Across All Platforms)
# =====================================================================
def scrape_page_metadata(url: str, platform: str) -> Optional[dict]:
    try:
        resp = cffi_requests.get(url, impersonate="chrome", timeout=8, allow_redirects=True)
        if resp.status_code != 200 or not resp.text:
            return None
        html = resp.text

        title_match = (
            re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
            or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', html, re.I)
            or re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        )
        img_match = (
            re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
            or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.I)
            or re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        )

        title = title_match.group(1).strip() if title_match else f"{platform.title()} Media"
        thumb = img_match.group(1).strip() if img_match else ""

        if thumb or title_match:
            return {
                "title": title[:100],
                "uploader": f"{platform.title()} Creator",
                "duration": 0,
                "duration_string": "HD Ready",
                "thumbnail": thumb,
                "hd_thumbnail": thumb,
                "platform": platform,
                "max_quality": "HD READY",
            }
    except Exception:
        pass
    return None


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
                resp = cffi_requests.get(oembed_endpoint, impersonate="chrome", timeout=5)
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
        "--impersonate",
        "chrome",
    ]
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]
    cmd.append(url)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
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

    scraped = scrape_page_metadata(url, platform)
    if scraped:
        with CACHE_LOCK:
            INFO_CACHE[cache_key] = {**scraped, "_ts": time.time()}
        return scraped

    fallback_thumb = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg" if vid_id else ""
    return {
        "title": f"{platform.title()} Media",
        "uploader": f"{platform.title()} Creator",
        "duration": 0,
        "duration_string": "HD Ready",
        "thumbnail": fallback_thumb,
        "hd_thumbnail": fallback_thumb,
        "platform": platform,
        "max_quality": "HD READY",
    }


# =====================================================================
# 2. LAYER 1: MULTI-MIRROR CLOUD CDN CLUSTER (With copyright=0 & API Key)
# =====================================================================
def _fetch_cdn_for_format(
    clean_url: str,
    vid_id: Optional[str],
    cloud_fmt: str,
    job_id: Optional[str] = None,
) -> Optional[str]:
    cache_key = f"{vid_id or clean_url}:{cloud_fmt}"

    with CACHE_LOCK:
        cached = PREWARM_CACHE.get(cache_key)
        if cached and (time.time() - cached.get("ts", 0) < 1200):
            return cached.get("url")

    key_lock = get_cdn_lock(cache_key)
    with key_lock:
        with CACHE_LOCK:
            cached = PREWARM_CACHE.get(cache_key)
            if cached and (time.time() - cached.get("ts", 0) < 1200):
                return cached.get("url")

        # Official multi-mirror cluster used by top downloader sites (with copyright=0 & api key)
        cluster_nodes = [
            {
                "host": "https://ab.cococococ.com",
                "referer": "https://y2down.cc/",
                "origin": "https://y2down.cc",
            },
            {
                "host": "https://p.oceansaver.in",
                "referer": "https://ddownr.com/",
                "origin": "https://ddownr.com",
            },
            {
                "host": "https://p.savenow.to",
                "referer": "https://loader.to/",
                "origin": "https://loader.to",
            },
        ]

        for node in cluster_nodes:
            host = node["host"]
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Referer": node["referer"],
                "Origin": node["origin"],
                "Accept": "*/*",
            }
            try:
                params = {
                    "button": "1",
                    "start": "1",
                    "end": "1",
                    "format": cloud_fmt,
                    "copyright": "0",
                    "url": clean_url,
                    "api": "dfcb6d76f2f6a9894gjkege8a4ab232222",
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
                cdn_job_id = init_data.get("id")
                if not cdn_job_id:
                    continue

                progress_urls = [
                    init_data.get("progress_url"),
                    f"{host}/ajax/progress.php?id={cdn_job_id}",
                    f"https://p.oceansaver.in/ajax/progress.php?id={cdn_job_id}",
                    f"https://p.savenow.to/ajax/progress.php?id={cdn_job_id}",
                    f"https://ab.cococococ.com/ajax/progress.php?id={cdn_job_id}",
                ]
                progress_urls = list(dict.fromkeys([u for u in progress_urls if u]))

                for _step in range(38):
                    time.sleep(0.9)
                    abort_node = False
                    for p_url in progress_urls:
                        try:
                            p_resp = cffi_requests.get(
                                p_url, headers=headers, impersonate="chrome", timeout=6
                            )
                            if p_resp.status_code != 200:
                                continue
                            p_data = p_resp.json()

                            dl_url = p_data.get("download_url")
                            if not dl_url:
                                alt_urls = p_data.get("alternative_download_urls")
                                if isinstance(alt_urls, list) and alt_urls:
                                    first_alt = alt_urls[0]
                                    if isinstance(first_alt, dict):
                                        dl_url = first_alt.get("url")
                                    elif isinstance(first_alt, str):
                                        dl_url = first_alt

                            if dl_url and str(dl_url).startswith("http"):
                                with CACHE_LOCK:
                                    PREWARM_CACHE[cache_key] = {"url": dl_url, "ts": time.time()}
                                return dl_url

                            raw_prog = p_data.get("progress")
                            if job_id and isinstance(raw_prog, (int, float)) and raw_prog > 0:
                                mapped_pct = min(88, max(40, 40 + int((raw_prog / 1000.0) * 48)))
                                update_job(job_id, progress=mapped_pct)

                            if p_data.get("success") == 0 and isinstance(raw_prog, (int, float)) and raw_prog >= 1000:
                                abort_node = True
                                break
                        except Exception:
                            continue
                    if abort_node:
                        break
            except Exception:
                continue

    return None


def get_cloud_cdn_direct_link(
    url: str,
    mode: str,
    format_type: str,
    quality: str,
    job_id: Optional[str] = None,
) -> Optional[str]:
    vid_id = extract_youtube_id(url)
    clean_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else url

    if mode == "audio":
        primary_fmt = format_type if format_type in ["mp3", "m4a", "wav", "ogg", "opus"] else "mp3"
        formats_to_try = [primary_fmt, "mp3", "m4a"]
    else:
        if format_type == "webm":
            primary_fmt = "webm"
        else:
            primary_fmt = quality if quality in ["1080", "720", "480", "360"] else "720"
        formats_to_try = [primary_fmt, "720", "480", "360"]

    formats_to_try = list(dict.fromkeys(formats_to_try))

    for fmt in formats_to_try:
        dl_url = _fetch_cdn_for_format(clean_url, vid_id, fmt, job_id=job_id)
        if dl_url:
            return dl_url

    return None


# =====================================================================
# 3. LAYER 2 & 3: COBALT + PIPED + INVIDIOUS PUBLIC API CLUSTERS
# =====================================================================
def get_cobalt_cluster_url(url: str, mode: str, quality: str) -> Optional[str]:
    vid_id = extract_youtube_id(url)
    clean_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else url
    q_val = quality if quality in ["1080", "720", "480", "360"] else "720"

    v10_instances = [
        "https://cobalt-api.kwiatekmiki.com",
        "https://cobalt-backend.canine.tools",
        "https://capi.3kh0.net",
    ]
    for host in v10_instances:
        try:
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0",
            }
            payload = {
                "url": clean_url,
                "videoQuality": q_val,
                "downloadMode": "audio" if mode == "audio" else "auto",
                "audioFormat": "mp3",
            }
            resp = cffi_requests.post(
                f"{host}/", json=payload, headers=headers, impersonate="chrome", timeout=6
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("url") and str(data["url"]).startswith("http"):
                    return data["url"]
        except Exception:
            continue
    return None


def get_piped_or_invidious_url(url: str, mode: str, quality: str) -> Optional[str]:
    vid_id = extract_youtube_id(url)
    if not vid_id:
        return None

    piped_hosts = [
        "https://pipedapi.kavin.rocks",
        "https://api.piped.private.coffee",
        "https://pipedapi.adminforge.de",
        "https://pipedapi.drgns.space",
    ]
    for host in piped_hosts:
        try:
            resp = cffi_requests.get(f"{host}/streams/{vid_id}", impersonate="chrome", timeout=6)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if mode == "audio":
                audios = data.get("audioStreams") or []
                if audios:
                    audios.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
                    if audios[0].get("url"):
                        return audios[0]["url"]
            else:
                vids = data.get("videoStreams") or []
                progs = [s for s in vids if not s.get("videoOnly", True) and s.get("url")]
                matched = [s for s in progs if s.get("quality") == f"{quality}p"]
                if matched:
                    return matched[0]["url"]
                if progs:
                    return progs[0]["url"]
        except Exception:
            continue

    invidious_hosts = [
        "https://inv.tux.pizza",
        "https://invidious.nerdvpn.de",
        "https://inv.nadeko.net",
    ]
    for host in invidious_hosts:
        try:
            resp = cffi_requests.get(f"{host}/api/v1/videos/{vid_id}", impersonate="chrome", timeout=6)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if mode == "audio":
                adaptive = data.get("adaptiveFormats") or []
                audios = [f for f in adaptive if str(f.get("type", "")).startswith("audio/") and f.get("url")]
                if audios:
                    return audios[0]["url"]
            else:
                fmt_streams = data.get("formatStreams") or []
                if fmt_streams:
                    for s in fmt_streams:
                        if s.get("qualityLabel") == f"{quality}p" and s.get("url"):
                            return s["url"]
                    if fmt_streams[0].get("url"):
                        return fmt_streams[0]["url"]
        except Exception:
            continue

    return None


# =====================================================================
# 4. LAYER 4: PYTUBEFIX MULTI-CLIENT ENGINE
# =====================================================================
def download_youtube_with_pytubefix(
    url: str,
    job_dir: Path,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool,
) -> Optional[Path]:
    try:
        from pytubefix import YouTube
    except Exception:
        return None

    clients = ["ANDROID_VR", "IOS", "WEB_CREATOR", "MWEB", "WEB_EMBED", "ANDROID_MUSIC", "TV"]
    for client in clients:
        try:
            yt = YouTube(url, client=client)
            out_file = job_dir / f"pytube_out.{format_type}"

            if mode == "audio":
                stream = yt.streams.get_audio_only() or yt.streams.filter(only_audio=True).first()
                if not stream:
                    continue
                raw_audio = Path(stream.download(output_path=str(job_dir), filename=f"raw_aud_{client}"))
                cmd = ["ffmpeg", "-y", "-i", str(raw_audio), "-vn"]
                if format_type == "mp3":
                    bitrate = f"{quality}k" if str(quality).isdigit() else "320k"
                    cmd += ["-c:a", "libmp3lame", "-b:a", bitrate]
                elif format_type == "m4a":
                    cmd += ["-c:a", "aac", "-b:a", "192k"]
                cmd.append(str(out_file))
                subprocess.run(cmd, check=True, timeout=60)
                if out_file.exists() and out_file.stat().st_size > 0:
                    return out_file
            else:
                target_res = f"{quality}p"
                prog_stream = None
                if not is_muted:
                    prog_stream = (
                        yt.streams.filter(res=target_res, file_extension="mp4", progressive=True).first()
                        or yt.streams.filter(progressive=True, file_extension="mp4")
                        .order_by("resolution")
                        .desc()
                        .first()
                    )

                if prog_stream:
                    raw_vid = Path(prog_stream.download(output_path=str(job_dir), filename=f"raw_vid_{client}.mp4"))
                    if not is_muted and format_type == "mp4":
                        return raw_vid
                    cmd = ["ffmpeg", "-y", "-i", str(raw_vid)]
                    if is_muted:
                        cmd += ["-an", "-c:v", "copy"]
                    else:
                        cmd += ["-c", "copy"]
                    cmd.append(str(out_file))
                    subprocess.run(cmd, check=True, timeout=60)
                    if out_file.exists() and out_file.stat().st_size > 0:
                        return out_file

                vid_stream = (
                    yt.streams.filter(res=target_res, file_extension="mp4").first()
                    or yt.streams.filter(file_extension="mp4").order_by("resolution").desc().first()
                )
                if not vid_stream:
                    continue
                raw_vid = Path(vid_stream.download(output_path=str(job_dir), filename=f"adapt_v_{client}.mp4"))
                cmd = ["ffmpeg", "-y", "-i", str(raw_vid)]
                if not is_muted:
                    aud_stream = yt.streams.get_audio_only()
                    if aud_stream:
                        raw_aud = Path(aud_stream.download(output_path=str(job_dir), filename=f"adapt_a_{client}"))
                        cmd += ["-i", str(raw_aud), "-c:v", "copy", "-c:a", "aac", "-shortest"]
                    else:
                        cmd += ["-c", "copy"]
                else:
                    cmd += ["-an", "-c:v", "copy"]
                cmd.append(str(out_file))
                subprocess.run(cmd, check=True, timeout=75)
                if out_file.exists() and out_file.stat().st_size > 0:
                    return out_file
        except Exception:
            continue

    return None


# =====================================================================
# 5. FFMPEG EXACT CLIP TRIMMER (1s to 5m Precision Cut)
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
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        ]

    cmd += [
        "-ss",
        str(start_sec),
        "-i",
        str(source_url),
        "-t",
        str(clip_duration),
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
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-movflags",
            "+faststart",
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
        "-ss",
        str(start_sec),
        "-i",
        str(source_url),
        "-t",
        str(clip_duration),
    ]
    if is_muted and mode == "video":
        fallback_cmd += ["-an", "-c:v", "copy"]
    else:
        fallback_cmd += ["-c", "copy"]
    fallback_cmd.append(str(out_file))
    subprocess.run(fallback_cmd, check=True, timeout=60)
    return out_file


# =====================================================================
# 6. UNIVERSAL VIDEO & PHOTO/IMAGE EXTRACTOR (Pinterest, Insta, X, Reddit, FB)
# =====================================================================
def extract_direct_media_or_photo_fallback(url: str, job_dir: Path, mode: str) -> Optional[Path]:
    try:
        resp = cffi_requests.get(url, impersonate="chrome", timeout=10, allow_redirects=True)
        if resp.status_code != 200 or not resp.text:
            return None
        html = resp.text.replace("\\/", "/")

        video_patterns = [
            r'<meta[^>]+property=["\']og:video(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video(?::secure_url)?["\']',
            r"(https://v1\.pinimg\.com/videos/[^\s\"'<>]+\.mp4)",
            r"(https://video\.twimg\.com/[^\s\"'<>]+\.mp4[^\s\"'<>]*)",
            r"(https://[^\s\"'<>]+\.cdninstagram\.com/[^\s\"'<>]+\.mp4[^\s\"'<>]*)",
            r"(https://v\.redd\.it/[^\s\"'<>]+/DASH_\d+\.mp4)",
        ]
        for pat in video_patterns:
            m = re.search(pat, html, re.I)
            if m:
                vid_url = m.group(1).replace("&amp;", "&")
                v_resp = cffi_requests.get(vid_url, impersonate="chrome", timeout=20)
                if v_resp.status_code == 200 and len(v_resp.content) > 1000:
                    vid_path = job_dir / "scraped_video.mp4"
                    vid_path.write_bytes(v_resp.content)
                    return vid_path

        if mode == "video":
            img_patterns = [
                r"(https://i\.pinimg\.com/originals/[^\s\"'<>]+\.(?:jpg|jpeg|png|webp|gif))",
                r"(https://i\.pinimg\.com/736x/[^\s\"'<>]+\.(?:jpg|jpeg|png|webp))",
                r"(https://pbs\.twimg\.com/media/[^\s\"'<>]+)",
                r"(https://i\.redd\.it/[^\s\"'<>]+\.(?:jpg|jpeg|png|webp|gif))",
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            ]
            for pat in img_patterns:
                m = re.search(pat, html, re.I)
                if m:
                    img_url = m.group(1).replace("&amp;", "&")
                    if "i.pinimg.com/736x/" in img_url:
                        orig_try = img_url.replace("/736x/", "/originals/")
                        check = cffi_requests.get(orig_try, impersonate="chrome", timeout=8)
                        if check.status_code == 200 and len(check.content) > 1000:
                            out_img = job_dir / "downloaded_photo.jpg"
                            out_img.write_bytes(check.content)
                            return out_img

                    i_resp = cffi_requests.get(img_url, impersonate="chrome", timeout=12)
                    if i_resp.status_code == 200 and len(i_resp.content) > 500:
                        ext = (
                            "png"
                            if ".png" in img_url.lower()
                            else ("gif" if ".gif" in img_url.lower() else "jpg")
                        )
                        out_img = job_dir / f"downloaded_photo.{ext}"
                        out_img.write_bytes(i_resp.content)
                        return out_img
    except Exception:
        pass
    return None


def run_universal_platform_download(
    url: str,
    job_dir: Path,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool,
) -> Path:
    raw_template = str(job_dir / "raw_media.%(ext)s")
    is_yt = detect_platform_name(url) == "YOUTUBE"

    base_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-check-certificates",
        "--geo-bypass",
        "--impersonate",
        "chrome",
        "--concurrent-fragments",
        "8",
        "-o",
        raw_template,
    ]
    if COOKIES_FILE.exists():
        base_cmd += ["--cookies", str(COOKIES_FILE)]

    # Modern 2026 yt-dlp client strategies (NO broken mweb,tv,web_safari!)
    yt_strategies = (
        [
            ["--extractor-args", "youtube:player_client=android_vr,ios,web_embedded;formats=missing_pot"],
            ["--extractor-args", "youtube:player_client=default,web_creator"],
            [],
        ]
        if is_yt
        else [[]]
    )

    last_err = "Media extraction failed"

    for attempt_idx, extra_args in enumerate(yt_strategies):
        if mode == "audio":
            cmd = base_cmd + extra_args + [
                "-f",
                "bestaudio/best",
                "-x",
                "--audio-format",
                format_type,
                "--audio-quality",
                f"{quality}K" if str(quality).isdigit() else "0",
                url,
            ]
        else:
            q_filter = f"[height<={quality}]" if str(quality).isdigit() else ""
            fmt = (
                f"bestvideo{q_filter}/best{q_filter}/best"
                if is_muted
                else f"bestvideo{q_filter}+bestaudio/best{q_filter}/best"
            )
            cmd = base_cmd + extra_args + ["-f", fmt, "--merge-output-format", format_type, url]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if proc.returncode == 0:
            files = [
                f
                for f in job_dir.glob("raw_media.*")
                if not f.name.endswith(".part") and not f.name.endswith(".ytdl")
            ]
            if files and files[0].stat().st_size > 0:
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

        stderr_text = proc.stderr or ""
        # Self-heal if yt-dlp in Docker layer was outdated
        if attempt_idx == 0 and "Failed to extract any player response" in stderr_text:
            force_upgrade_extractors()

        err_lines = [line for line in stderr_text.splitlines() if line.strip()]
        if err_lines:
            last_err = err_lines[-1]

    if not is_yt:
        scraped_file = extract_direct_media_or_photo_fallback(url, job_dir, mode)
        if scraped_file and scraped_file.exists():
            if mode == "audio" and scraped_file.suffix.lower() == ".mp4":
                aud_out = job_dir / f"extracted_audio.{format_type}"
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(scraped_file), "-vn", str(aud_out)],
                    check=True,
                    timeout=60,
                )
                return aud_out
            return scraped_file

    raise RuntimeError(last_err)


# =====================================================================
# 7. BACKGROUND JOB WORKER (5-Layer YouTube + Universal Video/Photo)
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

        if is_yt:
            # LAYER 1: Multi-Mirror Cloud CDN Cluster (with copyright=0 & api key)
            direct_cdn_url = get_cloud_cdn_direct_link(url, mode, format_type, quality, job_id=job_id)

            # LAYER 2: Cobalt Cluster
            if not direct_cdn_url:
                update_job(job_id, progress=62)
                direct_cdn_url = get_cobalt_cluster_url(url, mode, quality)

            # LAYER 3: Piped & Invidious Stream Cluster
            if not direct_cdn_url:
                update_job(job_id, progress=70)
                direct_cdn_url = get_piped_or_invidious_url(url, mode, quality)

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

                update_job(job_id, progress=88)
                out_path = job_dir / f"final_output.{format_type}"
                if has_trim:
                    cut_media_segment_exact(
                        direct_cdn_url, out_path, start_sec, end_sec, mode, format_type, quality, is_muted
                    )
                else:
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-y",
                            "-user_agent",
                            "Mozilla/5.0",
                            "-i",
                            direct_cdn_url,
                            "-an",
                            "-c:v",
                            "copy",
                            str(out_path),
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

            # LAYER 4: pytubefix Multi-Client Engine
            update_job(job_id, progress=78)
            downloaded_file = download_youtube_with_pytubefix(
                url, job_dir, mode, format_type, quality, is_muted and not has_trim
            )

            # LAYER 5: Self-Upgraded yt-dlp Nightly Engine
            if not downloaded_file or not downloaded_file.exists():
                update_job(job_id, progress=85)
                downloaded_file = run_universal_platform_download(
                    url, job_dir, mode, format_type, quality, is_muted and not has_trim
                )

            if has_trim:
                update_job(job_id, progress=92)
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

        # NON-YOUTUBE PLATFORMS (Instagram, Pinterest, X/Twitter, Facebook, Reddit, TikTok, etc.)
        update_job(job_id, progress=55)
        downloaded_file = run_universal_platform_download(
            url, job_dir, mode, format_type, quality, is_muted and not has_trim
        )

        is_photo = downloaded_file.suffix.lower() in [".jpg", ".jpeg", ".png", ".webp"]
        if has_trim and not is_photo:
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
# 8. REVIEWS & FEEDBACK STORAGE
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
# 9. API ROUTES
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
        if resp.status_code != 200 and info.get("thumbnail"):
            resp = cffi_requests.get(info["thumbnail"], impersonate="chrome", timeout=8)
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
            args=(url, mode, format_type, quality, None),
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
            else (
                "webm"
                if format_type == "webm"
                else (quality if quality in ["1080", "720", "480", "360"] else "720")
            )
        )
        cache_key = f"{vid_id or clean_url}:{cloud_fmt}"
        with CACHE_LOCK:
            cached = PREWARM_CACHE.get(cache_key)
            if cached and (time.time() - cached.get("ts", 0) < 1200):
                return JSONResponse(
                    {
                        "status": "ready",
                        "download_url": cached["url"],
                        "filename": final_filename,
                    }
                )

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "processing",
            "progress": 35,
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

    return JSONResponse(
        {
            "status": "processing",
            "job_id": job_id,
            "progress": 35,
            "filename": final_filename,
        }
    )


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
        "progress": job.get("progress", 50),
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
        f
        for f in job_dir.glob("*")
        if f.is_file()
        and (
            f.name.startswith("trimmed_")
            or f.name.startswith("muted_")
            or f.name.startswith("final_")
            or f.name.startswith("downloaded_photo")
            or f.name.startswith("scraped_video")
            or f.name.startswith("pytube_out")
        )
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
