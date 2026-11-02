import os
import re
import uuid
import shutil
import json
import urllib.request
import urllib.parse
from typing import Optional
from fastapi import FastAPI, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
from yt_dlp.utils import download_range_func

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
COOKIE_FILE = os.path.join(BASE_DIR, "cookies.txt")


def has_valid_cookie_file() -> bool:
    """Check karta hai ki cookies.txt exist karti hai aur Netscape format mein hai."""
    if not os.path.exists(COOKIE_FILE) or os.path.getsize(COOKIE_FILE) < 20:
        return False
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(512)
            return "Netscape" in content or ".youtube.com" in content or ".instagram.com" in content
    except Exception:
        return False


def create_temp_cookie_file(unique_id: str) -> Optional[str]:
    """Master cookies.txt ko corrupt hone se bachane ke liye har request par temp copy banata hai."""
    if has_valid_cookie_file():
        temp_cookie = os.path.join(DOWNLOAD_DIR, f"cookie_{unique_id}.txt")
        shutil.copyfile(COOKIE_FILE, temp_cookie)
        return temp_cookie
    return None


def cleanup_files(*filepaths: str):
    for filepath in filepaths:
        try:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass


def get_strategy_configs(temp_cookie: Optional[str]):
    """
    Alag-alag strategies try karega taaki YouTube kabhi block na kar paye:
    1. Bina cookies ke iOS / Android VR / TV client (Server IPs par sabse best kaam karta hai)
    2. Cookies ke sath Web Creator / TV / MWeb client
    3. Default fallback
    """
    strategies = []

    # Strategy 1: Mobile & Embedded clients (Bina cookies ke kyunki mobile clients cookies ke sath skip ho jaate hain)
    strategies.append({
        "use_cookie": False,
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android_vr", "tv", "web_embedded"],
            }
        },
    })

    # Strategy 2: Cookies ke sath Web/TV clients (Age-restricted ya login-required videos aur Instagram ke liye)
    if temp_cookie:
        strategies.append({
            "use_cookie": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["tv", "web_creator", "mweb", "web"],
                }
            },
        })

    # Strategy 3: Android + MWeb fallback
    strategies.append({
        "use_cookie": bool(temp_cookie),
        "extractor_args": {
            "youtube": {
                "player_client": ["mweb", "web"],
            }
        },
    })

    return strategies


def build_ydl_opts(base_opts: dict, strategy: dict, temp_cookie: Optional[str]) -> dict:
    opts = dict(base_opts)
    opts.update({
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "extractor_args": strategy.get("extractor_args", {}),
    })
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
    """Agar YouTube metadata block kare, toh official oEmbed API se instant details lata hai."""
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
            "ignore_no_formats_error": True,
        }

        info = None
        last_error = None

        for strategy in get_strategy_configs(temp_cookie):
            try:
                ydl_opts = build_ydl_opts(base_opts, strategy, temp_cookie)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    extracted = ydl.extract_info(url, download=False)
                    if extracted and (extracted.get("title") or extracted.get("formats")):
                        info = extracted
                        # Agar real video formats mil gaye toh loop break karo
                        if any(f.get("vcodec") != "none" for f in extracted.get("formats", [])):
                            break
            except Exception as e:
                last_error = e
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
                if f.get("height") and isinstance(f.get("height"), int) and f.get("vcodec") != "none"
            }),
            reverse=True,
        )

        if not heights:
            heights = [1080, 720, 480, 360]

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
    # Disk par simple unique_id filename rakhenge taaki emojis/special chars se FFmpeg fail na ho
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
        base_opts["format"] = "bestaudio/bestvideo*+bestaudio/best"
        base_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": format_type,
            "preferredquality": quality if quality.isdigit() else "320",
        }]
    else:
        if quality == "best" or not quality.isdigit():
            base_opts["format"] = "bestvideo*+bestaudio/best"
        else:
            # bestvideo* (asterisk ke sath) pre-merged aur separate dono streams ko support karta hai
            base_opts["format"] = (
                f"bestvideo*[height<={quality}]+bestaudio/"
                f"best[height<={quality}]/"
                f"bestvideo*+bestaudio/best"
            )

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
        # Har strategy ko ek-ek karke try karega jab tak file download na ho jaye
        for strategy in get_strategy_configs(temp_cookie):
            try:
                ydl_opts = build_ydl_opts(base_opts, strategy, temp_cookie)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                if info:
                    break
            except Exception as e:
                last_error = e
                # Agar partial file bani ho toh saaf karke agli strategy try karo
                for f in os.listdir(DOWNLOAD_DIR):
                    if f.startswith(unique_id) and not f.startswith(f"cookie_{unique_id}"):
                        cleanup_files(os.path.join(DOWNLOAD_DIR, f))
                continue

        # Ultimate Fallback: Agar specific resolution na mile toh jo bhi best format available hai wo uthao
        if not info:
            fallback_opts = dict(base_opts)
            fallback_opts["format"] = "bestvideo*+bestaudio/best"
            for strategy in get_strategy_configs(temp_cookie):
                try:
                    ydl_opts = build_ydl_opts(fallback_opts, strategy, temp_cookie)
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl_fallback:
                        info = ydl_fallback.extract_info(url, download=True)
                    if info:
                        break
                except Exception as e:
                    last_error = e
                    continue

        if not info:
            raise HTTPException(
                status_code=400,
                detail=f"Download failed: {str(last_error)}",
            )

        # Downloaded file ko dhoondo
        final_path = None
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

        if not final_path or not os.path.exists(final_path):
            raise HTTPException(
                status_code=400,
                detail="File could not be generated on server.",
            )

        actual_ext = os.path.splitext(final_path)[1] or f".{format_type}"
        chosen_name = (
            custom_title.strip()
            if (custom_title and custom_title.strip())
            else info.get("title", "Globifree_Media")
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
