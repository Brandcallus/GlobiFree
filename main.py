import os
import re
import json
import urllib.request
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from curl_cffi import requests as cffi_requests
import yt_dlp

app = FastAPI(title="GlobiFree Media Downloader Pro")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def ascii_safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    ascii_name = cleaned.encode("ascii", "ignore").decode("ascii").strip()
    return ascii_name if len(ascii_name) > 2 else "GlobiFree_Media"

def extract_youtube_id(url: str) -> Optional[str]:
    patterns = [r"(?:v=|\/videos\/|embed\/|youtu\.be\/|\/v\/|\/e\/|watch\?v=|&v=|\/shorts\/)([a-zA-Z0-9_-]{11})"]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match: return match.group(1)
    return None

def fast_fetch_info(url: str) -> dict:
    is_yt = "youtube.com" in url.lower() or "youtu.be" in url.lower()
    if is_yt:
        vid_id = extract_youtube_id(url)
        clean_yt_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else url
        oembed_urls = [f"https://www.youtube.com/oembed?url={quote(clean_yt_url)}&format=json"]
        for oembed_endpoint in oembed_urls:
            try:
                resp = cffi_requests.get(oembed_endpoint, impersonate="chrome", timeout=3)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("title"):
                        return {
                            "title": data.get("title"),
                            "uploader": data.get("author_name") or "YouTube",
                            "duration_string": "HD Ready",
                            "thumbnail": data.get("thumbnail_url") or f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
                            "platform": "YOUTUBE",
                        }
            except: pass

    cmd = ["yt-dlp", "--dump-json", "--no-playlist", "--impersonate", "chrome", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        if proc.returncode == 0 and proc.stdout.strip():
            info = json.loads(proc.stdout.splitlines()[0])
            return {
                "title": info.get("title") or "Media",
                "uploader": info.get("uploader") or "Creator",
                "duration_string": "HD Ready",
                "thumbnail": info.get("thumbnail") or "",
                "platform": "INSTAGRAM" if "instagram" in url.lower() else "YOUTUBE",
            }
    except: pass
    raise HTTPException(status_code=400, detail="Unable to fetch link details.")

def get_direct_stream_url(url: str, mode: str, quality: str) -> Optional[str]:
    # Use pytubefix for fastest YouTube direct URL extraction
    if "youtube" in url.lower() or "youtu.be" in url.lower():
        try:
            from pytubefix import YouTube
            yt = YouTube(url, client='ANDROID_VR')
            if mode == 'audio':
                stream = yt.streams.get_audio_only() or yt.streams.filter(only_audio=True).first()
            else:
                stream = yt.streams.filter(res=f"{quality}p", progressive=True, file_extension='mp4').first()
                if not stream:
                    stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
            if stream: return stream.url
        except: pass

    # Fallback to yt-dlp
    opts = {
        'quiet': True, 'no_warnings': True, 'format': 'bestaudio/best' if mode == 'audio' else f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/best[height<={quality}]/best',
        'geturl': True, 'impersonate': 'chrome'
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if 'url' in info: return info['url']
            if 'requested_downloads' in info: return info['requested_downloads'][0]['url']
    except: pass
    return None

@app.get("/", response_class=HTMLResponse)
def serve_home():
    html_path = Path("index.html")
    return html_path.read_text(encoding="utf-8") if html_path.exists() else "<h1>index.html not found</h1>"

@app.api_route("/api/info", methods=["GET", "POST"])
def api_info(url_form: Optional[str] = Form(None, alias="url"), url_query: Optional[str] = Query(None, alias="url")):
    target_url = (url_form or url_query or "").strip()
    if not target_url: raise HTTPException(status_code=400, detail="URL is required.")
    return fast_fetch_info(target_url)

@app.post("/api/prepare")
def api_prepare(url: str = Form(...), mode: str = Form("video"), format_type: str = Form("mp4"), quality: str = Form("720"), custom_title: Optional[str] = Form(None)):
    safe_title = ascii_safe_filename(custom_title or "GlobiFree_Media")
    
    # Return our internal Proxy Streaming URL instead of handing the raw URL to the browser
    dl_url = f"/api/stream_proxy?target={quote(url)}&mode={mode}&quality={quality}&format_type={format_type}&title={quote(safe_title)}"
    
    return JSONResponse({
        "download_url": dl_url,
        "filename": f"{safe_title}.{format_type}",
    })

@app.get("/api/stream_proxy")
def stream_proxy(target: str, mode: str, quality: str, format_type: str, title: str):
    raw_url = get_direct_stream_url(target, mode, quality)
    if not raw_url:
        raise HTTPException(status_code=500, detail="Direct stream blocked by platform.")

    def stream_generator():
        req = urllib.request.Request(raw_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'})
        try:
            with urllib.request.urlopen(req) as response:
                while True:
                    chunk = response.read(1024 * 1024) # Stream in 1MB chunks instantly
                    if not chunk: break
                    yield chunk
        except Exception:
            pass # Stop streaming cleanly if connection drops

    headers = {
        "Content-Disposition": f'attachment; filename="{title}.{format_type}"',
        "Cache-Control": "no-cache",
    }
    return StreamingResponse(stream_generator(), media_type="application/octet-stream", headers=headers)
