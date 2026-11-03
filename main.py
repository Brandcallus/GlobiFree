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

def format_duration(seconds: Optional[int]) -> str:
    if not seconds: return "HD Ready"
    hrs, rem = divmod(int(seconds), 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0: return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"

# =====================================================================
# FAST FETCH INFO
# =====================================================================
def fast_fetch_info(url: str) -> dict:
    is_yt = "youtube.com" in url.lower() or "youtu.be" in url.lower()
    if is_yt:
        vid_id = extract_youtube_id(url)
        clean_yt_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else url
        oembed_urls = [f"https://www.youtube.com/oembed?url={quote(clean_yt_url)}&format=json"]
        for endpoint in oembed_urls:
            try:
                resp = cffi_requests.get(endpoint, impersonate="chrome", timeout=3)
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
                "duration_string": format_duration(info.get("duration")),
                "thumbnail": info.get("thumbnail") or "",
                "platform": "INSTAGRAM" if "instagram" in url.lower() else "YOUTUBE",
            }
    except: pass
    raise HTTPException(status_code=400, detail="Unable to fetch link details.")

# =====================================================================
# EXTERNAL FAST APIs (To Bypass YouTube Blocks Completely)
# =====================================================================
def get_cobalt_url(url: str, mode: str, quality: str) -> Optional[str]:
    """Returns direct high-speed CDN URL from Cobalt instances"""
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    payload = {
        "url": url,
        "vQuality": quality if quality in ["1080", "720", "480", "360"] else "720",
        "isAudioOnly": mode == "audio",
        "filenamePattern": "basic"
    }
    instances = ["https://api.cobalt.tools", "https://co.wuk.sh", "https://cobalt.q0.wtf"]
    
    for inst in instances:
        try:
            resp = cffi_requests.post(f"{inst}/api/json", json=payload, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") in ["redirect", "stream", "success"] and "url" in data:
                    return data["url"]
        except: pass
    return None

def get_piped_url(url: str, mode: str, quality: str) -> Optional[str]:
    """Returns raw Googlevideo streaming URL from Piped instances"""
    vid_id = extract_youtube_id(url)
    if not vid_id: return None
    
    instances = ["https://pipedapi.kavin.rocks", "https://pipedapi.syncpundit.io", "https://api.piped.projectsegfau.lt"]
    for inst in instances:
        try:
            resp = cffi_requests.get(f"{inst}/streams/{vid_id}", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if mode == "audio":
                    audios = data.get("audioStreams", [])
                    if audios:
                        audios.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
                        return audios[0].get("url")
                else:
                    vids = data.get("videoStreams", [])
                    progs = [s for s in vids if not s.get("videoOnly", True)]
                    target_progs = [s for s in progs if s.get("quality") == f"{quality}p"]
                    if target_progs: return target_progs[0].get("url")
                    if progs: return progs[0].get("url")
                    if vids: return vids[0].get("url")
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
    
    # 1. Try Cobalt API for Instant Direct Link (Fastest, zero proxy needed)
    cobalt_url = get_cobalt_url(url, mode, quality)
    if cobalt_url:
        return JSONResponse({
            "download_url": cobalt_url,
            "filename": f"{safe_title}.{format_type}"
        })
    
    # 2. If Cobalt is busy, fallback to our Live Proxy Engine using Piped/Pytubefix
    dl_url = f"/api/stream_proxy?target={quote(url)}&mode={mode}&quality={quality}&format_type={format_type}&title={quote(safe_title)}"
    return JSONResponse({
        "download_url": dl_url,
        "filename": f"{safe_title}.{format_type}",
    })


@app.get("/api/stream_proxy")
def stream_proxy(target: str, mode: str, quality: str, format_type: str, title: str):
    raw_url = None
    is_yt = "youtube.com" in target.lower() or "youtu.be" in target.lower()
    
    # A. Use Piped API for YouTube Bypass
    if is_yt:
        raw_url = get_piped_url(target, mode, quality)
        
        # B. Fallback to Pytubefix if Piped is down
        if not raw_url:
            try:
                from pytubefix import YouTube
                yt = YouTube(target, client='ANDROID_VR')
                if mode == 'audio':
                    stream = yt.streams.get_audio_only() or yt.streams.filter(only_audio=True).first()
                else:
                    stream = yt.streams.filter(res=f"{quality}p", progressive=True, file_extension='mp4').first()
                    if not stream:
                        stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
                if stream: raw_url = stream.url
            except: pass
            
    # C. Fallback for Instagram/Others using yt-dlp
    if not raw_url:
        opts = {'quiet': True, 'no_warnings': True, 'geturl': True, 'impersonate': 'chrome', 'format': 'bestaudio/best' if mode == 'audio' else 'best'}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target, download=False)
                raw_url = info.get('url') or (info.get('requested_downloads', [{}])[0].get('url'))
        except: pass

    # If all 3 bypass methods fail
    if not raw_url:
        raise HTTPException(status_code=500, detail="Direct stream completely blocked by platform.")

    def stream_generator():
        # Using a proper stealth User-Agent so we don't get 403 on the streaming link
        req = urllib.request.Request(raw_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'})
        try:
            with urllib.request.urlopen(req) as response:
                while True:
                    # Stream in 1 MB chunks instantly
                    chunk = response.read(1024 * 1024)
                    if not chunk: break
                    yield chunk
        except Exception:
            pass 

    headers = {
        "Content-Disposition": f'attachment; filename="{title}.{format_type}"',
        "Cache-Control": "no-cache",
    }
    return StreamingResponse(stream_generator(), media_type="application/octet-stream", headers=headers)
