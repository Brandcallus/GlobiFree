import os
import re
import uuid
import json
import subprocess
import urllib.request
import urllib.parse
from typing import Optional, Tuple
from fastapi import FastAPI, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
from yt_dlp.utils import download_range_func

try:
    from pytubefix import YouTube as PyTubeYouTube
    HAS_PYTUBEFIX = True
except Exception:
    HAS_PYTUBEFIX = False

app = FastAPI(title="Globifree HD Downloader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def find_master_cookie_file() -> Optional[str]:
    """BASE_DIR mein cookies.txt ya COOKIES.TXT (case-insensitive) dhoondta hai."""
    for fname in os.listdir(BASE_DIR):
        if fname.lower() == "cookies.txt":
            fpath = os.path.join(BASE_DIR, fname)
            if os.path.isfile(fpath) and os.path.getsize(fpath) > 20:
                return fpath
    return None


def create_temp_cookie_file(unique_id: str) -> Optional[str]:
    """Master cookies file ko normalize karke safe temp copy banata hai."""
    master_cookie = find_master_cookie_file()
    if not master_cookie:
        return None
    try:
        with open(master_cookie, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read().replace("\r\n", "\n")
        if ".youtube.com" not in raw and ".instagram.com" not in raw and "Netscape" not in raw:
            return None
        if not raw.lstrip().startswith("# Netscape HTTP Cookie File"):
            raw = "# Netscape HTTP Cookie File\n" + raw
        temp_cookie = os.path.join(DOWNLOAD_DIR, f"cookie_{unique_id}.txt")
        with open(temp_cookie, "w", encoding="utf-8") as out:
            out.write(raw)
        return temp_cookie
    except Exception:
        return None


def cleanup_files(*filepaths: str):
    for filepath in filepaths:
        try:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass


def get_ejs_runtime_opts() -> dict:
    """yt-dlp ke liye NodeJS runtime aur EJS solver options generate karta hai."""
    try:
        parsed = yt_dlp.parse_options([
            "--js-runtimes", "node",
            "--remote-components", "ejs:github",
        ])
        return {
            k: v for k, v in parsed.ydl_opts.items()
            if k in ("js_runtimes", "remote_components")
        }
    except Exception:
        return {"js_runtimes": {"node": {}}}


EJS_OPTS = get_ejs_runtime_opts()


def has_playable_formats(info: Optional[dict]) -> bool:
    """Verify karta hai ki sirf storyboard images nahi balki asli video/audio stream mili hai."""
    if not info:
        return False
    formats = info.get("formats") or []
    for f in formats:
        ext = f.get("ext")
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        if ext == "mhtml" or vcodec == "images":
            continue
        if (vcodec and vcodec != "none") or (acodec and acodec != "none"):
            if f.get("url") or f.get("manifest_url"):
                return True
    return bool(info.get("url"))


def get_strategy_configs(temp_cookie: Optional[str]):
    strategies = []

    # Strategy 1: Cookies + Default/Web/TV clients (with NodeJS EJS solver)
    if temp_cookie:
        strategies.append({
            "use_cookie": True,
            "extractor_args": {},
        })
        strategies.append({
            "use_cookie": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["tv", "mweb", "web_creator", "web_safari"],
                }
            },
        })

    # Strategy 2: Bina cookies ke mobile/safari clients
    strategies.append({
        "use_cookie": False,
        "extractor_args": {
            "youtube": {
                "player_client": ["android_vr", "ios", "web_safari", "tv"],
            }
        },
    })

    # Strategy 3: Bina cookies ke pure default
    strategies.append({
        "use_cookie": False,
        "extractor_args": {},
    })

    return strategies


def build_ydl_opts(base_opts: dict, strategy: dict, temp_cookie: Optional[str]) -> dict:
    opts = dict(base_opts)
    opts.update(EJS_OPTS)
    opts.update({
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
    })
    if strategy.get("extractor_args"):
        opts["extractor_args"] = strategy["extractor_args"]
    else:
        opts.pop("extractor_args", None)

    if strategy.get("use_cookie") and temp_cookie and os.path.exists(temp_cookie):
        opts["cookiefile"] = temp_cookie
    else:
        opts.pop("cookiefile", None)
    return opts


def parse_time_to_seconds(time_str: Optional[str]) -> Optional[float]:
    if not time_str or not time_str.strip():
        return None
    parts = [float(p) for p in time_str.strip().split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    elif len(parts) == 2:
        return parts[0] * 60 + parts[1]
    elif len(parts) == 1:
        return parts[0]
    raise ValueError("Invalid time format. Please use MM:SS or HH:MM:SS.")


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name)
    return cleaned.strip()[:100] or "Globifree_Media"


def fetch_oembed_fallback(url: str) -> dict:
    oembed_url = f"https://www.youtube.com/oembed?url={urllib.parse.quote(url)}&format=json"
    req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as response:
        data = json.loads(response.read().decode("utf-8"))
        return {
            "title": data.get("title", "YouTube_Video"),
            "thumbnail": data.get("thumbnail_url", ""),
            "duration": 240,
            "uploader": data.get("author_name", "YouTube Creator"),
            "platform": "YouTube",
            "max_resolution": "1080p HD",
            "available_heights": [1080, 720, 480, 360],
        }


def download_with_pytubefix(
    url: str,
    unique_id: str,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool,
    start_sec: Optional[float],
    end_sec: Optional[float],
) -> Tuple[str, str]:
    """Backup Engine: Agar yt-dlp fail ho jaye toh pytubefix + ffmpeg se download karega."""
    if not HAS_PYTUBEFIX or ("youtube.com" not in url.lower() and "youtu.be" not in url.lower()):
        raise RuntimeError("Secondary engine unavailable for this URL.")

    clients = ["ANDROID_VR", "MWEB", "IOS", "TV", "WEB"]
    last_err = None

    for client_name in clients:
        raw_video = None
        raw_audio = None
        try:
            yt = PyTubeYouTube(url, client=client_name)
            title = yt.title or "Globifree_Media"
            final_path = os.path.join(DOWNLOAD_DIR, f"{unique_id}.{format_type}")

            trim_args = []
            if start_sec is not None and end_sec is not None:
                trim_args = ["-ss", str(start_sec), "-to", str(end_sec)]

            if mode == "audio":
                astream = (
                    yt.streams.filter(only_audio=True).order_by("abr").desc().first()
                    or yt.streams.get_audio_only()
                    or yt.streams.get_highest_resolution()
                )
                if not astream:
                    continue
                raw_audio = astream.download(output_path=DOWNLOAD_DIR, filename=f"{unique_id}_raw_audio")
                bitrate = f"{quality}k" if quality.isdigit() else "320k"
                cmd = ["ffmpeg", "-y", *trim_args, "-i", raw_audio]
                if format_type == "mp3":
                    cmd += ["-vn", "-acodec", "libmp3lame", "-b:a", bitrate, final_path]
                elif format_type == "m4a":
                    cmd += ["-vn", "-acodec", "aac", "-b:a", bitrate, final_path]
                elif format_type == "wav":
                    cmd += ["-vn", final_path]
                elif format_type == "flac":
                    cmd += ["-vn", "-acodec", "flac", final_path]
                else:
                    cmd += ["-vn", "-acodec", "libopus", "-b:a", bitrate, final_path]
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                cleanup_files(raw_audio)
                if os.path.exists(final_path) and os.path.getsize(final_path) > 0:
                    return final_path, title

            else:
                target_h = int(quality) if quality.isdigit() else 4320
                video_candidates = [
                    s for s in yt.streams.filter(type="video")
                    if getattr(s, "height", None)
                ]
                video_candidates.sort(key=lambda s: s.height, reverse=True)
                under_or_eq = [s for s in video_candidates if s.height <= target_h]
                vstream = (
                    under_or_eq[0]
                    if under_or_eq
                    else (video_candidates[-1] if video_candidates else yt.streams.get_highest_resolution())
                )
                if not vstream:
                    continue

                raw_video = vstream.download(output_path=DOWNLOAD_DIR, filename=f"{unique_id}_raw_video")

                if is_muted:
                    cmd = [
                        "ffmpeg", "-y", *trim_args,
                        "-i", raw_video,
                        "-an", "-c:v", "copy" if format_type == "mp4" and not trim_args else "libx264",
                        final_path,
                    ]
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif vstream.is_progressive:
                    cmd = [
                        "ffmpeg", "-y", *trim_args,
                        "-i", raw_video,
                        "-c", "copy" if format_type == "mp4" and not trim_args else "libx264",
                        final_path,
                    ]
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    astream = (
                        yt.streams.filter(only_audio=True).order_by("abr").desc().first()
                        or yt.streams.get_audio_only()
                    )
                    if astream:
                        raw_audio = astream.download(output_path=DOWNLOAD_DIR, filename=f"{unique_id}_raw_audio")
                        v_codec = "copy" if (format_type in ("mp4", "mkv") and not trim_args) else "libx264"
                        a_codec = "aac" if format_type in ("mp4", "mov", "mkv") else "libopus"
                        cmd = [
                            "ffmpeg", "-y", *trim_args,
                            "-i", raw_video, "-i", raw_audio,
                            "-c:v", v_codec, "-c:a", a_codec,
                            "-shortest", final_path,
                        ]
                    else:
                        cmd = ["ffmpeg", "-y", *trim_args, "-i", raw_video, "-c", "copy", final_path]
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                cleanup_files(raw_video, raw_audio)
                if os.path.exists(final_path) and os.path.getsize(final_path) > 0:
                    return final_path, title
        except Exception as e:
            last_err = e
            cleanup_files(raw_video, raw_audio)
            continue

    raise RuntimeError(f"Backup engine failed: {last_err}")


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    return "<h2>Globifree API is running. Please place index.html in the root directory.</h2>"


@app.post("/api/info")
async def get_media_info(url: str = Form(...)):
    unique_id = uuid.uuid4().hex[:8]
    temp_cookie = create_temp_cookie_file(unique_id)
    platform = "Instagram" if "instagram.com" in url.lower() else "YouTube"

    try:
        base_opts = {
            "skip_download": True,
        }

        info = None
        last_error = None

        for strategy in get_strategy_configs(temp_cookie):
            try:
                ydl_opts = build_ydl_opts(base_opts, strategy, temp_cookie)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    extracted = ydl.extract_info(url, download=False)
                    if has_playable_formats(extracted):
                        info = extracted
                        break
            except Exception as e:
                last_error = e
                continue

        if not info and HAS_PYTUBEFIX and platform == "YouTube":
            for client_name in ["ANDROID_VR", "MWEB", "IOS", "WEB"]:
                try:
                    yt = PyTubeYouTube(url, client=client_name)
                    if yt.title:
                        return {
                            "title": yt.title,
                            "thumbnail": yt.thumbnail_url,
                            "duration": yt.length or 240,
                            "uploader": yt.author or "YouTube Creator",
                            "platform": "YouTube",
                            "max_resolution": "1080p HD",
                            "available_heights": [1080, 720, 480, 360, 240],
                        }
                except Exception:
                    continue

        if not info:
            if platform == "YouTube":
                return fetch_oembed_fallback(url)
            raise HTTPException(status_code=400, detail=f"Could not fetch details: {str(last_error)}")

        formats = info.get("formats", [])
        heights = sorted(
            list({
                f.get("height")
                for f in formats
                if f.get("height")
                and isinstance(f.get("height"), int)
                and f.get("vcodec") not in (None, "none", "images")
                and f.get("ext") != "mhtml"
            }),
            reverse=True,
        )

        if not heights:
            heights = [1080, 720, 480, 360, 240]

        max_height = heights[0] if heights else 1080
        thumb = info.get("thumbnail")
        if not thumb and info.get("thumbnails"):
            thumb = info.get("thumbnails")[-1].get("url")

        return {
            "title": info.get("title") or "Media_Video",
            "thumbnail": thumb,
            "duration": info.get("duration") or 240,
            "uploader": info.get("uploader") or "Creator",
            "platform": platform,
            "max_resolution": f"{max_height}p HD",
            "available_heights": heights,
        }
    except HTTPException:
        raise
    except Exception as e:
        if platform == "YouTube":
            try:
                return fetch_oembed_fallback(url)
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=f"Metadata error: {str(e)}")
    finally:
        cleanup_files(temp_cookie)


@app.post("/api/download")
async def download_media(
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
    unique_id = uuid.uuid4().hex[:8]
    out_template = os.path.join(DOWNLOAD_DIR, f"{unique_id}.%(ext)s")
    is_muted = mute_video.lower() == "true"
    temp_cookie = create_temp_cookie_file(unique_id)

    base_opts = {
        "outtmpl": out_template,
        "nopart": True,
        "retries": 3,
        "fragment_retries": 3,
    }

    if mode == "audio":
        base_opts["format"] = "ba/b"
        base_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": format_type,
            "preferredquality": quality if quality.isdigit() else "320",
        }]
    else:
        # Strict filter ke bajaye format_sort use karenge taaki 240p/360p/720p kabhi fail na ho
        base_opts["format"] = "bv*+ba/b"
        if quality != "best" and quality.isdigit():
            base_opts["format_sort"] = [f"res:{quality}"]

        if is_muted:
            base_opts["postprocessors"] = [{
                "key": "FFmpegVideoConvertor",
                "preferedformat": format_type,
            }]
            base_opts["postprocessor_args"] = ["-an"]
        else:
            base_opts["merge_output_format"] = format_type

    start_sec = parse_time_to_seconds(start_time)
    end_sec = parse_time_to_seconds(end_time)

    if start_sec is not None and end_sec is not None:
        base_opts["download_ranges"] = download_range_func(None, [(start_sec, end_sec)])
        base_opts["force_keyframes_at_cuts"] = True

    info = None
    last_error = None

    try:
        # Engine 1: yt-dlp with EJS + NodeJS + Multi-Client Rotation
        for strategy in get_strategy_configs(temp_cookie):
            try:
                ydl_opts = build_ydl_opts(base_opts, strategy, temp_cookie)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                if info:
                    break
            except Exception as e:
                last_error = e
                for f in os.listdir(DOWNLOAD_DIR):
                    if f.startswith(unique_id) and not f.startswith(f"cookie_{unique_id}"):
                        cleanup_files(os.path.join(DOWNLOAD_DIR, f))
                continue

        final_path = None
        fallback_title = "Globifree_Media"

        if info:
            fallback_title = info.get("title") or fallback_title
            expected_path = os.path.join(DOWNLOAD_DIR, f"{unique_id}.{format_type}")
            if os.path.exists(expected_path) and os.path.getsize(expected_path) > 0:
                final_path = expected_path
            else:
                for file in os.listdir(DOWNLOAD_DIR):
                    if file.startswith(unique_id) and not file.startswith(f"cookie_{unique_id}"):
                        candidate = os.path.join(DOWNLOAD_DIR, file)
                        if os.path.getsize(candidate) > 0:
                            final_path = candidate
                            break

        # Engine 2: Agar yt-dlp fail hua, toh pytubefix + ffmpeg backup engine chalao
        if not final_path or not os.path.exists(final_path):
            try:
                final_path, fallback_title = download_with_pytubefix(
                    url=url,
                    unique_id=unique_id,
                    mode=mode,
                    format_type=format_type,
                    quality=quality,
                    is_muted=is_muted,
                    start_sec=start_sec,
                    end_sec=end_sec,
                )
            except Exception as backup_err:
                raise HTTPException(
                    status_code=400,
                    detail=f"Download failed: {str(last_error or backup_err)}",
                )

        actual_ext = os.path.splitext(final_path)[1] or f".{format_type}"
        chosen_name = (
            custom_title.strip()
            if (custom_title and custom_title.strip())
            else fallback_title
        )
        clean_title = sanitize_filename(chosen_name)
        download_filename = f"{clean_title}{actual_ext}"

        background_tasks.add_task(cleanup_files, final_path, temp_cookie)

        return FileResponse(
            path=final_path,
            filename=download_filename,
            media_type="application/octet-stream",
        )
    except HTTPException:
        cleanup_files(temp_cookie)
        raise
    except Exception as e:
        cleanup_files(temp_cookie)
        raise HTTPException(status_code=400, detail=f"Download failed: {str(e)}")
