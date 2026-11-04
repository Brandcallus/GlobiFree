import os
import re
import sys
import json
import time
import uuid
import shutil
import html
import threading
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List
from urllib.parse import quote, urlparse

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
JOBS: Dict[str, Dict[str, Any]] = {}

CACHE_LOCK = threading.Lock()
JOBS_LOCK = threading.Lock()
FEEDBACK_LOCK = threading.Lock()

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

BLOCKED_AD_DOMAINS = (
    "linkbrisora",
    "pathzenora",
    "onclick",
    "adsterra",
    "popads",
    "/landers/",
    "main.php?lp_key",
)


@app.on_event("startup")
def auto_upgrade_extractors():
    def _upgrade():
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
                    "gallery-dl",
                ],
                capture_output=True,
                timeout=120,
            )
        except Exception:
            pass

    threading.Thread(target=_upgrade, daemon=True).start()


# =====================================================================
# HELPER FUNCTIONS
# =====================================================================
def is_safe_media_url(dl_url: Optional[str]) -> bool:
    if not dl_url or not isinstance(dl_url, str) or not dl_url.startswith("http"):
        return False
    low = dl_url.lower()
    for bad in BLOCKED_AD_DOMAINS:
        if bad in low:
            return False
    return True


def is_blocked_platform(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in ("youtube.com", "youtu.be", "youtube-nocookie.com"))


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
    if "instagram.com" in u or "instagr.am" in u:
        return "INSTAGRAM"
    if "snapchat.com" in u or "snap.com" in u:
        return "SNAPCHAT"
    if "facebook.com" in u or "fb.watch" in u or "fb.com" in u:
        return "FACEBOOK"
    if "twitter.com" in u or "x.com" in u or "t.co" in u:
        return "X / TWITTER"
    if "tiktok.com" in u:
        return "TIKTOK"
    if "pinterest." in u or "pin.it" in u:
        return "PINTEREST"
    if "reddit.com" in u or "redd.it" in u:
        return "REDDIT"
    if "vimeo.com" in u:
        return "VIMEO"
    if "twitch.tv" in u:
        return "TWITCH"
    if "artstation.com" in u:
        return "ARTSTATION"
    if "behance.net" in u or "be.net" in u:
        return "BEHANCE"
    if "flickr.com" in u or "flic.kr" in u:
        return "FLICKR"
    if "500px.com" in u:
        return "500PX"
    if "threads.net" in u or "threads.com" in u:
        return "THREADS"
    if "soundcloud.com" in u:
        return "SOUNDCLOUD"
    return "UNIVERSAL"


def update_job(job_id: Optional[str], **kwargs):
    if not job_id:
        return
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)
            JOBS[job_id]["ts"] = time.time()


# =====================================================================
# 1. DEDICATED PLATFORM EXTRACTORS (Instagram, Snapchat, TikTok, X, ArtStation, etc.)
# =====================================================================
def extract_instagram_media(url: str) -> Optional[Dict[str, Any]]:
    """Multi-layer Instagram Reels/Posts/Stories extractor that works on cloud servers."""
    m = re.search(r"instagram\.com/(?:[^/]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url)
    shortcode = m.group(1) if m else None

    # Layer A: Try vxinstagram / ddinstagram JSON API
    if shortcode:
        for api_host in ("https://api.vxinstagram.com", "https://d.ddinstagram.com"):
            try:
                resp = cffi_requests.get(f"{api_host}/p/{shortcode}", impersonate="chrome", timeout=6)
                if resp.status_code == 200 and "application/json" in (resp.headers.get("content-type") or ""):
                    data = resp.json()
                    media_list = data.get("media_extended") or data.get("mediaURLs") or []
                    video_url = None
                    thumb_url = ""
                    if media_list and isinstance(media_list[0], dict):
                        video_url = media_list[0].get("url")
                        thumb_url = media_list[0].get("thumbnail_url") or video_url
                    elif media_list and isinstance(media_list[0], str):
                        video_url = media_list[0]
                    if is_safe_media_url(video_url):
                        return {
                            "title": (data.get("text") or f"Instagram_{shortcode}")[:80],
                            "uploader": data.get("user_name") or "Instagram Creator",
                            "duration": 0,
                            "thumbnail": thumb_url,
                            "video_url": video_url,
                            "image_url": thumb_url or video_url,
                        }
            except Exception:
                pass

    # Layer B: Public Instagram Embed Captioned endpoint (rarely blocked on cloud IPs)
    if shortcode:
        try:
            embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
            resp = cffi_requests.get(
                embed_url,
                headers={"User-Agent": CHROME_UA, "Accept-Language": "en-US,en;q=0.9"},
                impersonate="chrome",
                timeout=8,
            )
            if resp.status_code == 200:
                page = resp.text
                # Look for escaped video_url inside contextJSON or script tags
                vid_matches = re.findall(r'\\?"video_url\\?"\s*:\s*\\?"(https?:[^"\\]+(?:\\.[^"\\]*)*)\\?"', page)
                disp_matches = re.findall(r'\\?"display_url\\?"\s*:\s*\\?"(https?:[^"\\]+(?:\\.[^"\\]*)*)\\?"', page)
                video_url = None
                if vid_matches:
                    raw_v = vid_matches[0].encode("utf-8").decode("unicode_escape").replace("\\/", "/")
                    video_url = html.unescape(raw_v)
                thumb_url = ""
                if disp_matches:
                    raw_t = disp_matches[-1].encode("utf-8").decode("unicode_escape").replace("\\/", "/")
                    thumb_url = html.unescape(raw_t)
                if not thumb_url:
                    img_m = re.search(r'class="EmbeddedMediaImage"[^>]+src="([^"]+)"', page)
                    if img_m:
                        thumb_url = html.unescape(img_m.group(1))
                user_m = re.search(r'class="UsernameText"[^>]*>([^<]+)<', page)
                uploader = user_m.group(1).strip() if user_m else "Instagram Creator"
                if video_url or thumb_url:
                    return {
                        "title": f"Instagram_{shortcode}",
                        "uploader": uploader,
                        "duration": 0,
                        "thumbnail": thumb_url,
                        "video_url": video_url if is_safe_media_url(video_url) else None,
                        "image_url": thumb_url if is_safe_media_url(thumb_url) else None,
                    }
        except Exception:
            pass

    return None


def extract_snapchat_media(url: str) -> Optional[Dict[str, Any]]:
    """Extracts Snapchat /t/ shortlinks, Spotlight videos, and Public Stories."""
    try:
        resp = cffi_requests.get(
            url,
            headers={"User-Agent": CHROME_UA, "Accept-Language": "en-US,en;q=0.9"},
            impersonate="chrome",
            allow_redirects=True,
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        page = resp.text
        resolved_url = str(resp.url) or url

        title = "Snapchat Video"
        uploader = "Snapchat Creator"
        thumb_url = ""
        video_url = None
        duration = 0

        # 1. Parse __NEXT_DATA__ JSON (used by Snapchat Spotlight, Stories & /t/ redirects)
        next_m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
            page,
            re.S,
        )
        if next_m:
            try:
                next_json = json.loads(next_m.group(1))
                page_props = (next_json.get("props") or {}).get("pageProps") or {}

                # Check videoMetadata
                vid_meta = page_props.get("videoMetadata") or {}
                if vid_meta:
                    title = vid_meta.get("name") or vid_meta.get("description") or title
                    video_url = vid_meta.get("contentUrl") or video_url
                    thumb_url = vid_meta.get("thumbnailUrl") or thumb_url
                    uploader = (vid_meta.get("creator") or {}).get("name") or uploader

                # Deep search for mediaUrl / snapMediaUrl / contentUrl in pageProps
                raw_props_str = json.dumps(page_props)
                if not video_url:
                    sc_vids = re.findall(
                        r'"(?:contentUrl|mediaUrl|videoUrl|snapMediaUrl)"\s*:\s*"(https://[^"]+?)"',
                        raw_props_str,
                    )
                    for cand in sc_vids:
                        if ".mp4" in cand or "sc-cdn.net" in cand or "bolt" in cand:
                            video_url = cand
                            break
                    if not video_url and sc_vids:
                        video_url = sc_vids[0]

                if not thumb_url:
                    sc_thumbs = re.findall(
                        r'"(?:thumbnailUrl|previewImageUrl|snapPreviewUrl)"\s*:\s*"(https://[^"]+?)"',
                        raw_props_str,
                    )
                    if sc_thumbs:
                        thumb_url = sc_thumbs[0]
            except Exception:
                pass

        # 2. Check preload video link or og:video / og:image tags
        if not video_url:
            preload_v = re.search(r'<link[^>]+rel=["\']preload["\'][^>]+as=["\']video["\'][^>]+href=["\']([^"\']+)["\']', page, re.I)
            if preload_v:
                video_url = html.unescape(preload_v.group(1))
        if not video_url:
            og_v = re.search(r'<meta[^>]+property=["\']og:video(?::secure_url|:url)?["\'][^>]+content=["\']([^"\']+)["\']', page, re.I)
            if og_v:
                video_url = html.unescape(og_v.group(1))
        if not thumb_url:
            og_i = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', page, re.I)
            if og_i:
                thumb_url = html.unescape(og_i.group(1))
        og_t = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', page, re.I)
        if og_t and title == "Snapchat Video":
            title = html.unescape(og_t.group(1)).strip()

        if video_url or thumb_url:
            return {
                "title": title[:90],
                "uploader": uploader,
                "duration": duration,
                "thumbnail": thumb_url,
                "video_url": video_url if is_safe_media_url(video_url) else None,
                "image_url": thumb_url if is_safe_media_url(thumb_url) else None,
                "resolved_url": resolved_url,
            }
    except Exception:
        pass
    return None


def extract_tiktok_no_watermark(url: str) -> Optional[Dict[str, Any]]:
    """Fetches TikTok HD No-Watermark video & metadata via TikWM API."""
    try:
        resp = cffi_requests.post(
            "https://www.tikwm.com/api/",
            data={"url": url, "hd": 1},
            impersonate="chrome",
            timeout=8,
        )
        if resp.status_code == 200:
            payload = resp.json()
            data = payload.get("data") or {}
            video_url = data.get("hdplay") or data.get("play")
            if video_url and not str(video_url).startswith("http"):
                video_url = f"https://www.tikwm.com{video_url}"
            if is_safe_media_url(video_url):
                return {
                    "title": data.get("title") or "TikTok Video",
                    "uploader": (data.get("author") or {}).get("nickname") or "TikTok Creator",
                    "duration": data.get("duration") or 0,
                    "thumbnail": data.get("origin_cover") or data.get("cover") or "",
                    "video_url": video_url,
                    "audio_url": data.get("music") or video_url,
                }
    except Exception:
        pass
    return None


def extract_twitter_x_api(url: str) -> Optional[Dict[str, Any]]:
    """Extracts X/Twitter videos & GIFs via fxtwitter/vxtwitter API."""
    try:
        m = re.search(r"(?:twitter\.com|x\.com)/([^/?#]+)/status/(\d+)", url)
        if not m:
            return None
        user, status_id = m.group(1), m.group(2)
        api_url = f"https://api.fxtwitter.com/{user}/status/{status_id}"
        resp = cffi_requests.get(api_url, impersonate="chrome", timeout=7)
        if resp.status_code == 200:
            tweet = (resp.json() or {}).get("tweet") or {}
            media = tweet.get("media") or {}
            videos = media.get("videos") or []
            photos = media.get("photos") or []
            video_url = videos[0].get("url") if videos else None
            thumb = (
                videos[0].get("thumbnail_url")
                if videos
                else (photos[0].get("url") if photos else "")
            )
            if video_url or thumb:
                return {
                    "title": (tweet.get("text") or f"X Post {status_id}")[:90],
                    "uploader": (tweet.get("author") or {}).get("name") or user,
                    "duration": videos[0].get("duration", 0) if videos else 0,
                    "thumbnail": thumb,
                    "video_url": video_url,
                    "image_url": photos[0].get("url") if photos else thumb,
                }
    except Exception:
        pass
    return None


def extract_artstation_project(url: str) -> Optional[Dict[str, Any]]:
    """Extracts high-res 3D shorts, turntable videos & 4K artworks from ArtStation."""
    try:
        m = re.search(r"artstation\.com/(?:artwork|projects)/([a-zA-Z0-9_-]+)", url)
        if not m:
            return None
        project_hash = m.group(1)
        api_url = f"https://www.artstation.com/projects/{project_hash}.json"
        resp = cffi_requests.get(api_url, impersonate="chrome", timeout=8)
        if resp.status_code != 200:
            return None
        data = resp.json()
        assets = data.get("assets") or []
        video_url = None
        embed_url = None
        best_image = data.get("cover_url") or ""

        for asset in assets:
            img = asset.get("image_url") or ""
            if img and (not best_image or "/4k/" in img or "/large/" in img):
                best_image = img
            player_html = asset.get("player_embedded") or ""
            if player_html:
                mp4_match = re.search(r'src=["\'](https?://[^"\']+\.mp4[^"\']*)["\']', player_html)
                if mp4_match:
                    video_url = mp4_match.group(1)
                    break
                iframe_match = re.search(r'src=["\'](https?://[^"\']+)["\']', player_html)
                if iframe_match and not embed_url:
                    embed_url = iframe_match.group(1)
                    if "artstation.com/embed/" in embed_url:
                        try:
                            emb_resp = cffi_requests.get(embed_url, impersonate="chrome", timeout=6)
                            emb_mp4 = re.search(r'(https?://cdn[^\s"\'<>]+?\.mp4[^\s"\'<>]*)', emb_resp.text)
                            if emb_mp4:
                                video_url = html.unescape(emb_mp4.group(1))
                                break
                        except Exception:
                            pass

        return {
            "title": data.get("title") or f"ArtStation_{project_hash}",
            "uploader": (data.get("user") or {}).get("full_name") or "ArtStation Artist",
            "duration": 0,
            "thumbnail": best_image,
            "video_url": video_url,
            "embed_url": embed_url,
            "image_url": best_image,
        }
    except Exception:
        pass
    return None


def extract_design_or_photo_page(url: str, platform: str) -> Optional[Dict[str, Any]]:
    """Extracts high-res videos/photos from Behance, Flickr, 500px, Pinterest, or OpenGraph pages."""
    try:
        headers = {
            "User-Agent": CHROME_UA,
            "Referer": f"https://{urlparse(url).netloc}/",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = cffi_requests.get(url, headers=headers, impersonate="chrome", allow_redirects=True, timeout=10)
        if resp.status_code != 200:
            return None
        page = resp.text

        title_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', page, re.I)
        if not title_match:
            title_match = re.search(r"<title>([^<]+)</title>", page, re.I)
        title = html.unescape(title_match.group(1)).strip() if title_match else f"{platform.title()} Media"

        og_img_match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', page, re.I)
        best_img = html.unescape(og_img_match.group(1)) if og_img_match else ""

        og_vid_match = re.search(
            r'<meta[^>]+property=["\']og:video(?::secure_url|:url)?["\'][^>]+content=["\']([^"\']+)["\']',
            page,
            re.I,
        )
        video_url = html.unescape(og_vid_match.group(1)) if og_vid_match else None

        if platform == "PINTEREST" and not video_url:
            pin_mp4 = re.findall(r'https://v\d*\.pinimg\.com/videos/[^\s"\'<>\\]+\.mp4', page)
            if pin_mp4:
                hd_pins = [p for p in pin_mp4 if "720p" in p or "mc" in p]
                video_url = (hd_pins[0] if hd_pins else pin_mp4[0]).replace("\\/", "/")

        if platform == "BEHANCE":
            if not video_url:
                behance_mp4 = re.findall(r'https?://[^\s"\'<>\\]+?\.mp4[^\s"\'<>\\]*', page)
                if behance_mp4:
                    video_url = html.unescape(behance_mp4[0].replace("\\/", "/"))
            behance_imgs = re.findall(
                r'https://mir-s3-cdn-cf\.behance\.net/project_modules/(?:fs|max_3840|1400|disp)/[^\s"\'<>\\]+',
                page,
            )
            if behance_imgs:
                best_img = behance_imgs[0].replace("\\/", "/")

        if platform == "FLICKR":
            flickr_imgs = re.findall(r'(?:https?:)?//live\.staticflickr\.com/\d+/\d+_[\da-fA-F]+_[okhbclz]\.(?:jpg|png)', page)
            if flickr_imgs:
                def _flickr_rank(u_str: str) -> int:
                    for idx, tag in enumerate(["_o.", "_k.", "_h.", "_b.", "_c.", "_z."]):
                        if tag in u_str:
                            return idx
                    return 99

                flickr_imgs.sort(key=_flickr_rank)
                raw_u = flickr_imgs[0].replace("\\/", "/")
                best_img = f"https:{raw_u}" if raw_u.startswith("//") else raw_u

        if platform == "500PX":
            px_id_match = re.search(r"500px\.com/photo/(\d+)", str(resp.url) or url)
            if px_id_match:
                px_id = px_id_match.group(1)
                try:
                    api_px = cffi_requests.get(
                        f"https://api.500px.com/v1/photos?ids={px_id}&image_size[]=2048&image_size[]=1600&image_size[]=1080",
                        impersonate="chrome",
                        timeout=6,
                    )
                    if api_px.status_code == 200:
                        photos_dict = (api_px.json() or {}).get("photos") or {}
                        p_obj = photos_dict.get(px_id) or {}
                        if p_obj.get("name"):
                            title = p_obj["name"]
                        imgs_list = p_obj.get("images") or []
                        if imgs_list:
                            imgs_list.sort(key=lambda x: x.get("size", 0), reverse=True)
                            best_img = imgs_list[0].get("https_url") or imgs_list[0].get("url") or best_img
                except Exception:
                    pass
            if not best_img:
                drscdn = re.findall(r'https://drscdn\.500px\.org/photo/[^\s"\'<>\\]+', page)
                if drscdn:
                    best_img = html.unescape(drscdn[0].replace("\\/", "/"))

        if video_url or best_img:
            return {
                "title": title[:90],
                "uploader": f"{platform.title()} Creator",
                "duration": 0,
                "thumbnail": best_img,
                "video_url": video_url if is_safe_media_url(video_url) else None,
                "image_url": best_img if is_safe_media_url(best_img) else None,
            }
    except Exception:
        pass
    return None


def fetch_from_cobalt_instances(url: str, mode: str, quality: str) -> Optional[str]:
    """Queries Cobalt v10 instances for fast direct streams."""
    q_val = quality if quality in ["1080", "720", "480", "360"] else "1080"
    cobalt_hosts = [
        "https://cobalt-api.kwiatekmiki.com",
        "https://cobalt-backend.canine.tools",
        "https://capi.3kh0.net",
        "https://api.cobalt.blackcat.sweeux.org",
    ]
    for host in cobalt_hosts:
        try:
            payload = {
                "url": url,
                "videoQuality": q_val,
                "downloadMode": "audio" if mode == "audio" else "auto",
                "audioFormat": "mp3",
                "filenameStyle": "basic",
                "tiktokFullAudio": True,
            }
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": CHROME_UA,
            }
            resp = cffi_requests.post(f"{host}/", json=payload, headers=headers, impersonate="chrome", timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                dl_url = data.get("url")
                if is_safe_media_url(dl_url):
                    return dl_url
                picker = data.get("picker") or []
                if picker and is_safe_media_url(picker[0].get("url")):
                    return picker[0]["url"]
        except Exception:
            continue
    return None


# =====================================================================
# 2. FAST INFO FETCH
# =====================================================================
def fast_fetch_info(url: str) -> dict:
    if is_blocked_platform(url):
        raise HTTPException(status_code=400, detail="This platform is not supported.")

    platform = detect_platform_name(url)
    cache_key = url.strip()

    with CACHE_LOCK:
        cached_info = INFO_CACHE.get(cache_key)
        if cached_info and (time.time() - cached_info.get("_ts", 0) < 1800):
            return {k: v for k, v in cached_info.items() if k != "_ts"}

    # 1. Instagram Fast Extractor
    if platform == "INSTAGRAM":
        ig = extract_instagram_media(url)
        if ig:
            result = {
                "title": ig["title"],
                "uploader": ig["uploader"],
                "duration": 0,
                "duration_string": "Reel / Post HD",
                "thumbnail": ig["thumbnail"],
                "hd_thumbnail": ig["image_url"] or ig["thumbnail"],
                "platform": platform,
                "max_quality": "1080P HD",
            }
            with CACHE_LOCK:
                INFO_CACHE[cache_key] = {**result, "_ts": time.time()}
            return result

    # 2. Snapchat Fast Extractor
    if platform == "SNAPCHAT":
        sc = extract_snapchat_media(url)
        if sc:
            result = {
                "title": sc["title"],
                "uploader": sc["uploader"],
                "duration": sc["duration"],
                "duration_string": "Snap HD Ready",
                "thumbnail": sc["thumbnail"],
                "hd_thumbnail": sc["image_url"] or sc["thumbnail"],
                "platform": platform,
                "max_quality": "HD READY",
            }
            with CACHE_LOCK:
                INFO_CACHE[cache_key] = {**result, "_ts": time.time()}
            return result

    # 3. TikTok No-Watermark Fast Extractor
    if platform == "TIKTOK":
        tk = extract_tiktok_no_watermark(url)
        if tk:
            result = {
                "title": tk["title"],
                "uploader": tk["uploader"],
                "duration": tk["duration"],
                "duration_string": format_duration(tk["duration"]),
                "thumbnail": tk["thumbnail"],
                "hd_thumbnail": tk["thumbnail"],
                "platform": platform,
                "max_quality": "NO WATERMARK HD",
            }
            with CACHE_LOCK:
                INFO_CACHE[cache_key] = {**result, "_ts": time.time()}
            return result

    # 4. X / Twitter Fast Extractor
    if platform == "X / TWITTER":
        tw = extract_twitter_x_api(url)
        if tw:
            result = {
                "title": tw["title"],
                "uploader": tw["uploader"],
                "duration": tw["duration"],
                "duration_string": format_duration(tw["duration"]),
                "thumbnail": tw["thumbnail"],
                "hd_thumbnail": tw.get("image_url") or tw["thumbnail"],
                "platform": platform,
                "max_quality": "HD READY",
            }
            with CACHE_LOCK:
                INFO_CACHE[cache_key] = {**result, "_ts": time.time()}
            return result

    # 5. ArtStation Fast Extractor
    if platform == "ARTSTATION":
        art = extract_artstation_project(url)
        if art:
            result = {
                "title": art["title"],
                "uploader": art["uploader"],
                "duration": 0,
                "duration_string": "3D / 4K Art",
                "thumbnail": art["thumbnail"],
                "hd_thumbnail": art["image_url"] or art["thumbnail"],
                "platform": platform,
                "max_quality": "4K READY",
            }
            with CACHE_LOCK:
                INFO_CACHE[cache_key] = {**result, "_ts": time.time()}
            return result

    # 6. Behance, Flickr, 500px, Pinterest Scraper
    if platform in ("BEHANCE", "FLICKR", "500PX", "PINTEREST"):
        scraped = extract_design_or_photo_page(url, platform)
        if scraped and (scraped.get("thumbnail") or scraped.get("video_url")):
            result = {
                "title": scraped["title"],
                "uploader": scraped["uploader"],
                "duration": 0,
                "duration_string": "High-Res Ready",
                "thumbnail": scraped.get("thumbnail") or "",
                "hd_thumbnail": scraped.get("image_url") or scraped.get("thumbnail") or "",
                "platform": platform,
                "max_quality": "HIGH-RES",
            }
            with CACHE_LOCK:
                INFO_CACHE[cache_key] = {**result, "_ts": time.time()}
            return result

    # 7. Universal yt-dlp metadata fetch (without --impersonate error risk)
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-playlist",
        "--no-warnings",
        "--no-check-certificates",
        "--geo-bypass",
        "--user-agent",
        CHROME_UA,
    ]
    if platform == "VIMEO":
        cmd += ["--referer", "https://vimeo.com/"]
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]
    cmd.append(url)

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

    # 8. Fallback OpenGraph scrape
    scraped = extract_design_or_photo_page(url, platform)
    if scraped:
        return {
            "title": scraped["title"],
            "uploader": scraped["uploader"],
            "duration": 0,
            "duration_string": "HD Ready",
            "thumbnail": scraped.get("thumbnail") or "",
            "hd_thumbnail": scraped.get("image_url") or scraped.get("thumbnail") or "",
            "platform": platform,
            "max_quality": "HD READY",
        }

    return {
        "title": f"{platform.title()} Media",
        "uploader": f"{platform.title()} Creator",
        "duration": 0,
        "duration_string": "HD Ready",
        "thumbnail": "",
        "hd_thumbnail": "",
        "platform": platform,
        "max_quality": "HD READY",
    }


# =====================================================================
# 3. UNIVERSAL MULTI-ENGINE DOWNLOADER
# =====================================================================
def download_direct_url_to_file(
    direct_url: str,
    job_dir: Path,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool,
    referer: Optional[str] = None,
) -> Optional[Path]:
    """Downloads a direct MP4/Audio/Image URL and processes audio/mute if requested."""
    try:
        headers = {"User-Agent": CHROME_UA}
        if referer:
            headers["Referer"] = referer
        resp = cffi_requests.get(direct_url, headers=headers, impersonate="chrome", allow_redirects=True, timeout=45)
        if resp.status_code != 200 or not resp.content:
            return None

        ctype = (resp.headers.get("content-type") or "").lower()
        is_image = "image/" in ctype or any(
            direct_url.lower().split("?")[0].endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")
        )

        if is_image and mode == "video":
            ext = "png" if "png" in ctype else "jpg"
            img_path = job_dir / f"artwork.{ext}"
            img_path.write_bytes(resp.content)
            return img_path

        raw_path = job_dir / f"raw_direct.{format_type}"
        raw_path.write_bytes(resp.content)

        if mode == "audio":
            out_audio = job_dir / f"output.{format_type}"
            cmd = ["ffmpeg", "-y", "-i", str(raw_path), "-vn"]
            if format_type == "mp3":
                cmd += ["-c:a", "libmp3lame", "-b:a", f"{quality}k" if str(quality).isdigit() else "320k"]
            elif format_type == "m4a":
                cmd += ["-c:a", "aac", "-b:a", "192k"]
            elif format_type == "wav":
                cmd += ["-c:a", "pcm_s16le"]
            cmd.append(str(out_audio))
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
            return out_audio

        if is_muted:
            muted_out = job_dir / f"muted.{format_type}"
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(raw_path), "-an", "-c:v", "copy", str(muted_out)],
                check=True,
                capture_output=True,
                timeout=60,
            )
            return muted_out

        return raw_path
    except Exception:
        return None


def download_with_ytdlp(
    target_url: str,
    job_dir: Path,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool,
    referer: Optional[str] = None,
) -> Optional[Path]:
    raw_template = str(job_dir / "ytdlp_media.%(ext)s")
    base_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-check-certificates",
        "--geo-bypass",
        "--concurrent-fragments",
        "8",
        "--user-agent",
        CHROME_UA,
        "-o",
        raw_template,
    ]
    if referer:
        base_cmd += ["--referer", referer, "--add-header", f"Origin:{referer.rstrip('/')}"]
    if COOKIES_FILE.exists():
        base_cmd += ["--cookies", str(COOKIES_FILE)]

    if mode == "audio":
        cmd = base_cmd + [
            "-f",
            "bestaudio/best",
            "-x",
            "--audio-format",
            format_type,
            "--audio-quality",
            f"{quality}K" if str(quality).isdigit() else "0",
            target_url,
        ]
    else:
        q_filter = f"[height<={quality}]" if str(quality).isdigit() else ""
        fmt = (
            f"bestvideo{q_filter}/best{q_filter}/best"
            if is_muted
            else f"bestvideo{q_filter}+bestaudio/best{q_filter}/bestvideo+bestaudio/best"
        )
        cmd = base_cmd + ["-f", fmt, "--merge-output-format", format_type, target_url]

    subprocess.run(cmd, capture_output=True, text=True, timeout=110)
    files = [
        f
        for f in job_dir.glob("ytdlp_media.*")
        if f.is_file() and not f.name.endswith(".part") and not f.name.endswith(".ytdl") and f.stat().st_size > 0
    ]
    if files:
        downloaded_raw = files[0]
        if is_muted and mode == "video":
            muted_out = job_dir / f"muted_output.{downloaded_raw.suffix.lstrip('.') or format_type}"
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(downloaded_raw), "-an", "-c:v", "copy", str(muted_out)],
                check=True,
                capture_output=True,
                timeout=60,
            )
            return muted_out
        return downloaded_raw
    return None


def download_with_gallery_dl(url: str, job_dir: Path) -> Optional[Path]:
    """Fallback using gallery-dl for Instagram, Pinterest boards, ArtStation, Behance, Flickr, 500px, Reddit, X."""
    gdl_dir = job_dir / "gdl"
    gdl_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "gallery-dl",
        "--no-mtime",
        "--range",
        "1",
        "-D",
        str(gdl_dir),
    ]
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]
    cmd.append(url)
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        files = [f for f in gdl_dir.rglob("*") if f.is_file() and f.stat().st_size > 0]
        if files:
            vids = [f for f in files if f.suffix.lower() in (".mp4", ".webm", ".mov", ".gif")]
            if vids:
                return vids[0]
            files.sort(key=lambda x: x.stat().st_size, reverse=True)
            return files[0]
    except Exception:
        pass
    return None


def download_universal_media(
    url: str,
    job_dir: Path,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool,
) -> Path:
    platform = detect_platform_name(url)

    # 1. Instagram Multi-Layer Engine
    if platform == "INSTAGRAM":
        ig = extract_instagram_media(url)
        if ig and (ig.get("video_url") or ig.get("image_url")):
            target_dl = ig.get("video_url") or ig.get("image_url")
            res = download_direct_url_to_file(
                target_dl, job_dir, mode, format_type, quality, is_muted, referer="https://www.instagram.com/"
            )
            if res:
                return res

    # 2. Snapchat (/t/ share links, Spotlight & Stories) Engine
    if platform == "SNAPCHAT":
        sc = extract_snapchat_media(url)
        if sc:
            if sc.get("video_url"):
                res = download_direct_url_to_file(
                    sc["video_url"], job_dir, mode, format_type, quality, is_muted, referer="https://www.snapchat.com/"
                )
                if res:
                    return res
            if sc.get("resolved_url") and sc["resolved_url"] != url:
                res = download_with_ytdlp(
                    sc["resolved_url"], job_dir, mode, format_type, quality, is_muted, referer="https://www.snapchat.com/"
                )
                if res:
                    return res

    # 3. TikTok No-Watermark Fast Engine
    if platform == "TIKTOK":
        tk = extract_tiktok_no_watermark(url)
        if tk:
            target_dl = tk["audio_url"] if mode == "audio" else tk["video_url"]
            res = download_direct_url_to_file(target_dl, job_dir, mode, format_type, quality, is_muted)
            if res:
                return res

    # 4. X (Twitter) Video & GIF Engine
    if platform == "X / TWITTER":
        tw = extract_twitter_x_api(url)
        if tw and (tw.get("video_url") or tw.get("image_url")):
            target_dl = tw.get("video_url") or tw.get("image_url")
            res = download_direct_url_to_file(target_dl, job_dir, mode, format_type, quality, is_muted)
            if res:
                return res

    # 5. ArtStation 3D Shorts & 4K Artworks Engine
    if platform == "ARTSTATION":
        art = extract_artstation_project(url)
        if art:
            if art.get("video_url"):
                res = download_direct_url_to_file(
                    art["video_url"],
                    job_dir,
                    mode,
                    format_type,
                    quality,
                    is_muted,
                    referer="https://www.artstation.com/",
                )
                if res:
                    return res
            if art.get("embed_url"):
                res = download_with_ytdlp(
                    art["embed_url"],
                    job_dir,
                    mode,
                    format_type,
                    quality,
                    is_muted,
                    referer="https://www.artstation.com/",
                )
                if res:
                    return res
            if art.get("image_url"):
                res = download_direct_url_to_file(
                    art["image_url"],
                    job_dir,
                    mode,
                    format_type,
                    quality,
                    is_muted,
                    referer="https://www.artstation.com/",
                )
                if res:
                    return res

    # 6. Vimeo Creator-Restricted & Embedded Videos Engine
    if platform == "VIMEO":
        res = download_with_ytdlp(
            url, job_dir, mode, format_type, quality, is_muted, referer="https://vimeo.com/"
        )
        if res:
            return res
        vm_match = re.search(r"vimeo\.com/(?:channels/[^/]+/|groups/[^/]+/videos/|video/)?(\d+)", url)
        if vm_match:
            player_url = f"https://player.vimeo.com/video/{vm_match.group(1)}"
            res = download_with_ytdlp(
                player_url,
                job_dir,
                mode,
                format_type,
                quality,
                is_muted,
                referer="https://player.vimeo.com/",
            )
            if res:
                return res

    # 7. Primary yt-dlp Engine (Facebook, Reddit + Audio, Pinterest, Twitch VODs/Clips, Instagram, Snapchat, etc.)
    res = download_with_ytdlp(url, job_dir, mode, format_type, quality, is_muted)
    if res:
        return res

    # 8. Cobalt Mirror Tunnel Fallback
    cobalt_stream = fetch_from_cobalt_instances(url, mode, quality)
    if cobalt_stream:
        res = download_direct_url_to_file(cobalt_stream, job_dir, mode, format_type, quality, is_muted)
        if res:
            return res

    # 9. Design & Photography Direct Scraper (Behance, Flickr, 500px, Pinterest, etc.)
    scraped = extract_design_or_photo_page(url, platform)
    if scraped:
        target_dl = scraped.get("video_url") or scraped.get("image_url")
        if target_dl:
            res = download_direct_url_to_file(
                target_dl,
                job_dir,
                mode,
                format_type,
                quality,
                is_muted,
                referer=f"https://{urlparse(url).netloc}/",
            )
            if res:
                return res

    # 10. gallery-dl Universal Fallback
    gdl_file = download_with_gallery_dl(url, job_dir)
    if gdl_file:
        return gdl_file

    raise RuntimeError(f"Could not extract media from {platform.title()}. Please verify the link is public.")


# =====================================================================
# 4. FFMPEG EXACT CLIP TRIMMER (1s to 5m Precision Cut)
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
        cmd += ["-user_agent", CHROME_UA]
    cmd += ["-ss", str(start_sec), "-i", str(source_url), "-t", str(clip_duration)]

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
    return out_file


# =====================================================================
# 5. BACKGROUND JOB WORKER
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
    has_trim = start_sec is not None and end_sec is not None and end_sec > start_sec

    try:
        update_job(job_id, progress=55)
        downloaded_file = download_universal_media(
            url, job_dir, mode, format_type, quality, is_muted and not has_trim
        )

        is_static_image = downloaded_file.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")

        if has_trim and not is_static_image:
            update_job(job_id, progress=88)
            trimmed_out = job_dir / f"trimmed.{format_type}"
            downloaded_file = cut_media_segment_exact(
                str(downloaded_file),
                trimmed_out,
                start_sec,
                end_sec,
                mode,
                format_type,
                quality,
                is_muted,
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
        "platform": "INSTAGRAM",
        "rating": 5,
        "message": "Instagram Reels, Snapchat Spotlight, TikTok no-watermark aur ArtStation 3D shorts super fast download ho rahe hain!",
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
    title: str = Query("GlobiFree_Artwork"),
):
    if is_blocked_platform(url):
        raise HTTPException(status_code=400, detail="This platform is not supported.")

    info = fast_fetch_info(url)
    thumb_url = info.get("hd_thumbnail") or info.get("thumbnail")
    if not thumb_url:
        raise HTTPException(status_code=404, detail="High-res image or thumbnail not found.")

    safe_title = ascii_safe_filename(title)
    thumb_dir = TEMP_DIR / f"thumb_{uuid.uuid4()}"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumb_dir / f"{safe_title}.jpg"

    try:
        headers = {"User-Agent": CHROME_UA, "Referer": f"https://{urlparse(url).netloc}/"}
        resp = cffi_requests.get(thumb_url, headers=headers, impersonate="chrome", timeout=12)
        if resp.status_code != 200 and info.get("thumbnail"):
            resp = cffi_requests.get(info["thumbnail"], headers=headers, impersonate="chrome", timeout=12)
        thumb_path.write_bytes(resp.content)
        background_tasks.add_task(cleanup_dir, thumb_dir)
        return FileResponse(
            path=str(thumb_path),
            filename=f"{safe_title}_HD.jpg",
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
    quality: str = Form("1080"),
):
    return {"status": "ready"}


@app.post("/api/prepare")
def api_prepare(
    url: str = Form(...),
    mode: str = Form("video"),
    format_type: str = Form("mp4"),
    quality: str = Form("1080"),
    mute_video: str = Form("false"),
    custom_title: Optional[str] = Form(None),
    start_time: Optional[str] = Form(None),
    end_time: Optional[str] = Form(None),
):
    if is_blocked_platform(url):
        raise HTTPException(status_code=400, detail="This platform is not supported.")

    cleanup_old_temp_files()
    is_muted = mute_video.lower() == "true"
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
        args=(
            job_id,
            url,
            mode,
            format_type,
            quality,
            is_muted,
            safe_header_title,
            start_sec,
            end_sec,
        ),
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
        raise HTTPException(status_code=404, detail="Session expired. Please click download again.")
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
    files = [
        f
        for f in job_dir.rglob("*")
        if f.is_file() and not f.name.endswith(".part") and not f.name.endswith(".ytdl")
    ]
    if not files:
        cleanup_dir(job_dir)
        raise HTTPException(status_code=404, detail="File not found.")
    target_file = sorted(files, key=lambda x: x.stat().st_mtime, reverse=True)[0]
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
    platform: str = Form("INSTAGRAM"),
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
