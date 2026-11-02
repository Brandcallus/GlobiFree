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
    expose_headers=["Content-Disposition", "Content-Length"],
)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def normalize_media_url(url: str) -> str:
    """youtu.be ya tracking params (?si=...) hata kar clean URL banata hai taaki redirect time bache."""
    url = url.strip()
    yt_match = re.search(r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/|embed/))([A-Za-z0-9_-]{11})", url)
    if yt_match:
        return f"https://www.youtube.com/watch?v={yt_match.group(1)}"
    return url


def find_master_cookie_file() -> Optional[str]:
    for fname in os.listdir(BASE_DIR):
        if fname.lower() == "cookies.txt":
            fpath = os.path.join(BASE_DIR, fname)
            if os.path.isfile(fpath) and os.path.getsize(fpath) > 20:
                return fpath
    return None


def create_temp_cookie_file(unique_id: str) -> Optional[str]:
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


def fast_youtube_info(url: str) -> Optional[dict]:
    """YouTube videos ke liye 0.5 second mein title, thumbnail aur real duration nikalta hai."""
    try:
        clean_url = normalize_media_url(url)
        oembed_url = f"https://www.youtube.com/oembed?url={urllib.parse.quote(clean_url)}&format=json"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as response:
            data = json.loads(response.read().decode("utf-8"))

        duration = 240
        # Real duration nikalne ke liye fast watch page regex check
        try:
            watch_req = urllib.request.Request(
                clean_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            with urllib.request.urlopen(watch_req, timeout=4) as w_resp:
                html_chunk = w_resp.read(350000).decode("utf-8", errors="ignore")
                dur_match = re.search(r'"lengthSeconds"\s*:\s*"(\d+)"', html_chunk)
                if dur_match:
                    duration = int(dur_match.group(1))
        except Exception:
            pass

        return {
            "title": data.get("title", "YouTube_Video"),
            "thumbnail": data.get("thumbnail_url", ""),
            "duration": duration,
            "uploader": data.get("author_name", "YouTube Creator"),
            "platform": "YouTube",
            "max_resolution": "1080p HD",
            "available_heights": [1080, 720, 480, 360, 240, 144],
        }
    except Exception:
        return None


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
    """Fast Backup Engine: pytubefix + ffmpeg."""
    if not HAS_PYTUBEFIX or ("youtube.com" not in url.lower() and "youtu.be" not in url.lower()):
        raise RuntimeError("Secondary engine unavailable for this URL.")

    clients = ["IOS", "ANDROID_VR", "MWEB", "WEB"]
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
                )
                if not astream:
                    continue
                raw_audio = astream.download(output_path=DOWNLOAD_DIR, filename=f"{unique_id}_raw_audio")
                bitrate = f"{quality}k" if quality.isdigit() else "320k"
                cmd = ["ffmpeg", "-y", "-threads", "0", *trim_args, "-i", raw_audio]
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

                # Helper: parse "720p" -> 720
                def get_res_int(s) -> int:
                    res = getattr(s, "resolution", None)
                    if res and isinstance(res, str) and res.endswith("p") and res[:-1].isdigit():
                        return int(res[:-1])
                    return 0

                # Agar <= 720p MP4 chahiye aur progressive stream hai toh direct 1-file download karo (Super Fast!)
                vstream = None
                if target_h <= 720 and not is_muted:
                    prog_streams = [
                        s for s in yt.streams.filter(progressive=True, file_extension="mp4")
                        if get_res_int(s) > 0
                    ]
                    prog_streams.sort(key=get_res_int, reverse=True)
                    under_prog = [s for s in prog_streams if get_res_int(s) <= target_h]
                    if under_prog:
                        vstream = under_prog[0]
                    elif prog_streams:
                        vstream = prog_streams[0]

                if not vstream:
                    video_candidates = [
                        s for s in yt.streams.filter(type="video")
                        if get_res_int(s) > 0
                    ]
                    video_candidates.sort(key=get_res_int, reverse=True)
                    under_or_eq = [s for s in video_candidates if get_res_int(s) <= target_h]
                    vstream = (
                        under_or_eq[0]
                        if under_or_eq
                        else (video_candidates[-1] if video_candidates else yt.streams.get_highest_resolution())
                    )

                if not vstream:
                    continue

                raw_video = vstream.download(output_path=DOWNLOAD_DIR, filename=f"{unique_id}_raw_video")

                # Agar progressive MP4 hai aur trim/mute nahi hai, toh bina FFmpeg ke seedha rename karke bhej do!
                if vstream.is_progressive and not is_muted and not trim_args and format_type == "mp4":
                    os.replace(raw_video, final_path)
                    return final_path, title

                if is_muted:
                    cmd = [
                        "ffmpeg", "-y", "-threads", "0", *trim_args,
                        "-i", raw_video,
                        "-an", "-c:v", "copy" if (format_type == "mp4" and not trim_args) else "libx264",
                        "-preset", "ultrafast",
                        final_path,
                    ]
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif vstream.is_progressive:
                    cmd = [
                        "ffmpeg", "-y", "-threads", "0", *trim_args,
                        "-i", raw_video,
                        "-c", "copy" if (format_type == "mp4" and not trim_args) else "libx264",
                        "-preset", "ultrafast",
                        final_path,
                    ]
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    astream = (
                        yt.streams.filter(only_audio=True, subtype="mp4").order_by("abr").desc().first()
                        or yt.streams.filter(only_audio=True).order_by("abr").desc().first()
                    )
                    if astream:
                        raw_audio = astream.download(output_path=DOWNLOAD_DIR, filename=f"{unique_id}_raw_audio")
                        v_codec = "copy" if (format_type in ("mp4", "mkv") and not trim_args) else "libx264"
                        a_codec = "copy" if (format_type in ("mp4", "mkv") and astream.subtype == "mp4" and not trim_args) else "aac"
                        cmd = [
                            "ffmpeg", "-y", "-threads", "0", *trim_args,
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
    url = normalize_media_url(url)
    platform = "Instagram" if "instagram.com" in url.lower() else "YouTube"

    # YouTube ke liye Instant 0.5s Metadata Fetcher (Bina 15 second wait kiye!)
    if platform == "YouTube":
        fast_info = fast_youtube_info(url)
        if fast_info:
            return fast_info

    # Instagram ya fallback ke liye yt-dlp
    unique_id = uuid.uuid4().hex[:8]
    temp_cookie = create_temp_cookie_file(unique_id)
    try:
        ydl_opts = {
            **EJS_OPTS,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": 10,
        }
        if temp_cookie:
            ydl_opts["cookiefile"] = temp_cookie

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            thumb = info.get("thumbnail")
            if not thumb and info.get("thumbnails"):
                thumb = info.get("thumbnails")[-1].get("url")
            return {
                "title": info.get("title") or "Media_Video",
                "thumbnail": thumb,
                "duration": info.get("duration") or 60,
                "uploader": info.get("uploader") or "Creator",
                "platform": platform,
                "max_resolution": "1080p HD",
                "available_heights": [1080, 720, 480, 360],
            }
    except Exception as e:
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
    url = normalize_media_url(url)
    unique_id = uuid.uuid4().hex[:8]
    out_template = os.path.join(DOWNLOAD_DIR, f"{unique_id}.%(ext)s")
    is_muted = mute_video.lower() == "true"
    temp_cookie = create_temp_cookie_file(unique_id)

    base_opts = {
        **EJS_OPTS,
        "outtmpl": out_template,
        "nopart": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "socket_timeout": 12,
        "retries": 2,
        "fragment_retries": 2,
        # 8x Parallel Fragment Download + 10MB chunks for high speed
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 10485760,
    }

    if mode == "audio":
        base_opts["format"] = "ba[ext=m4a]/ba/b"
        base_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": format_type,
            "preferredquality": quality if quality.isdigit() else "320",
        }]
    else:
        # Fast MP4 preference taaki FFmpeg ko re-encode na karna pade
        if quality != "best" and quality.isdigit() and int(quality) <= 720 and format_type == "mp4" and not is_muted:
            # 720p ya kam ke liye pehle pre-merged single-file MP4 check karega (Instant Download!)
            base_opts["format"] = (
                f"b[height<={quality}][ext=mp4]/"
                f"bv*[height<={quality}][ext=mp4]+ba[ext=m4a]/"
                f"bv*[height<={quality}]+ba/b"
            )
        else:
            base_opts["format"] = "bv*+ba/b"
            if quality != "best" and quality.isdigit():
                base_opts["format_sort"] = [f"res:{quality}", "ext:mp4:m4a"]
            else:
                base_opts["format_sort"] = ["ext:mp4:m4a"]

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

    # Sirf 2 fast attempts (No long waiting loops!):
    # 1. Cookie-free fast mobile/safari clients
    # 2. Cookies + Web/TV clients
    attempts = [
        {
            "use_cookie": False,
            "extractor_args": {
                "youtube": {
                    "player_client": ["ios", "web_safari", "android_vr", "tv_embedded"],
                }
            },
        },
    ]
    if temp_cookie:
        attempts.append({
            "use_cookie": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["tv_embedded", "web_safari", "mweb", "web"],
                }
            },
        })

    info = None
    ydl_error = None

    try:
        for attempt in attempts:
            try:
                opts = dict(base_opts)
                opts["extractor_args"] = attempt["extractor_args"]
                if attempt["use_cookie"] and temp_cookie:
                    opts["cookiefile"] = temp_cookie
                else:
                    opts.pop("cookiefile", None)

                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                if info:
                    break
            except Exception as e:
                ydl_error = e
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

        # Backup Engine: pytubefix (Fixed .resolution support + instant progressive MP4)
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
                    detail=f"Download failed: {str(ydl_error)} | Backup: {str(backup_err)}",
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
