import os
import re
import json
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from curl_cffi import requests as cffi_requests

app = FastAPI(title="GlobiFree Media Downloader Pro")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def format_duration(seconds: Optional[int]) -> str:
    if not seconds: return "HD Ready"
    hrs, rem = divmod(int(seconds), 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0: return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"

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

@app.get("/", response_class=HTMLResponse)
def serve_home():
    html_path = Path("index.html")
    return html_path.read_text(encoding="utf-8") if html_path.exists() else "<h1>index.html not found</h1>"

@app.api_route("/api/info", methods=["GET", "POST"])
def api_info(url_form: Optional[str] = Form(None, alias="url"), url_query: Optional[str] = Query(None, alias="url")):
    target_url = (url_form or url_query or "").strip()
    if not target_url: raise HTTPException(status_code=400, detail="URL is required.")
    return fast_fetch_info(target_url)
