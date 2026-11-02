import os
import re
import uuid
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


def get_base_ydl_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    
    # 🔥 THE FIX: Agar cookies hain, toh PC (web) ki tarah behave karo.
    # Mobile spoofing aur PC cookies mix hone se YouTube format error deta hai.
    if os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
        opts["extractor_args"] = {"youtube": {"player_client": ["web"]}}
    else:
        opts["extractor_args"] = {"youtube": {"player_client": ["tv", "web_creator"]}}
        
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


def cleanup_file(filepath: str):
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
    except Exception:
        pass


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    return "<h2>Globifree API is running. Please place index.html in the root directory.</h2>"


@app.post("/api/info")
async def get_media_info(url: str = Form(...)):
    info = None
    error_msg = ""
    
    # 🛡️ THE 3-LAYER UNBREAKABLE FETCH SHIELD 🛡️
    # Layer 1: Normal PC Web Fetch
    try:
        ydl_opts = get_base_ydl_opts()
        ydl_opts["skip_download"] = True
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e1:
        error_msg = str(e1)
        # Layer 2: TV Client Fetch (Fallback)
        try:
            ydl_opts2 = get_base_ydl_opts()
            ydl_opts2["skip_download"] = True
            ydl_opts2["extractor_args"] = {"youtube": {"player_client": ["tv"]}}
            with yt_dlp.YoutubeDL(ydl_opts2) as ydl2:
                info = ydl2.extract_info(url, download=False)
        except Exception as e2:
            # Layer 3: Brute Force Details (Ignores format check completely)
            try:
                ydl_opts3 = get_base_ydl_opts()
                ydl_opts3["extract_flat"] = True # Only grabs title/thumbnail
                with yt_dlp.YoutubeDL(ydl_opts3) as ydl3:
                    info = ydl3.extract_info(url, download=False)
            except Exception as e3:
                raise HTTPException(status_code=400, detail=f"Could not fetch details. Error: {error_msg}")

    if not info:
        raise HTTPException(status_code=400, detail="Failed to extract info.")
        
    formats = info.get("formats", [])
    heights = sorted(list({f.get("height") for f in formats if f.get("height") and isinstance(f.get("height"), int)}), reverse=True)
    
    # Agar format hide kiya gaya hai (Music videos me), toh default qualities daal do
    if not heights:
        heights = [1080, 720, 480, 360]
        
    max_height = heights[0] if heights else None
    platform = "Instagram" if "instagram.com" in url.lower() else "YouTube"

    return {
        "title": info.get("title", "Media_Video"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration") or 240,
        "uploader": info.get("uploader", "Creator"),
        "platform": platform,
        "max_resolution": f"{max_height}p HD" if max_height else "Original HD",
        "available_heights": heights,
    }


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
    out_template = os.path.join(DOWNLOAD_DIR, f"{unique_id}_%(title).40s.%(ext)s")
    is_muted = mute_video.lower() == "true"

    ydl_opts = get_base_ydl_opts()
    ydl_opts["outtmpl"] = out_template
    ydl_opts["nopart"] = True

    if mode == "audio":
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": format_type,
            "preferredquality": quality if quality.isdigit() else "320",
        }]
    else:
        if quality == "best":
            ydl_opts["format"] = "bestvideo+bestaudio/best"
        else:
            ydl_opts["format"] = f"bestvideo[height<={quality}]+bestaudio/bestvideo+bestaudio/best"

        if is_muted:
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegVideoConvertor",
                "preferedformat": format_type,
            }]
            ydl_opts["postprocessor_args"] = ["-an"]
        else:
            ydl_opts["merge_output_format"] = format_type

    start_sec = parse_time_to_seconds(start_time)
    end_sec = parse_time_to_seconds(end_time)

    if start_sec is not None and end_sec is not None:
        if end_sec <= start_sec:
            raise HTTPException(status_code=400, detail="End time must be greater than start time.")
        clip_len = end_sec - start_sec
        if clip_len < 1:
            raise HTTPException(status_code=400, detail="Minimum clip duration is 1 second.")
        if clip_len > 180:
            raise HTTPException(status_code=400, detail="Maximum clip duration is 3 minutes (180 seconds).")

        ydl_opts["download_ranges"] = download_range_func(None, [(start_sec, end_sec)])
        ydl_opts["force_keyframes_at_cuts"] = True

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=True)
            except Exception as de:
                # 🛡️ THE DOWNLOAD SHIELD
                ydl_opts["format"] = "b/best" 
                if "extractor_args" in ydl_opts:
                    del ydl_opts["extractor_args"] 
                    
                with yt_dlp.YoutubeDL(ydl_opts) as ydl_fallback:
                    info = ydl_fallback.extract_info(url, download=True)
                    
            prepared_path = ydl.prepare_filename(info)
            base_path = os.path.splitext(prepared_path)[0]
            expected_ext = f".{format_type}"
            final_path = base_path + expected_ext

            if not os.path.exists(final_path):
                for file in os.listdir(DOWNLOAD_DIR):
                    if file.startswith(unique_id):
                        final_path = os.path.join(DOWNLOAD_DIR, file)
                        break

            if not os.path.exists(final_path):
                raise FileNotFoundError("Processed file could not be located on server.")

            chosen_name = custom_title.strip() if (custom_title and custom_title.strip()) else info.get("title", "video")
            clean_title = sanitize_filename(chosen_name)
            suffix = ""
            if start_sec is not None and end_sec is not None:
                suffix += "_trimmed"
            if mode == "video" and is_muted:
                suffix += "_muted"

            download_filename = f"{clean_title}{suffix}{expected_ext}"
            background_tasks.add_task(cleanup_file, final_path)

            return FileResponse(
                path=final_path,
                filename=download_filename,
                media_type="application/octet-stream",
            )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {str(e)}")
