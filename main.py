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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Dict, Any, List
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, HTTPException, Query, BackgroundTasks, Response
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

INFO_CACHE: Dict[str, Dict[str, Any]] = {}
JOBS: Dict[str, Dict[str, Any]] = {}

CACHE_LOCK = threading.Lock()
JOBS_LOCK = threading.Lock()

YOUTUBE_RESTRICTION_MESSAGE = (
    "For YouTube, no download formats other than the thumbnail are currently available."
)

BOT_STATE: Dict[str, Any] = {
    "active": True,
    "interval_seconds": 300,
    "pings": 0,
    "last_ping": "Starting...",
    "target_url": os.environ.get("RENDER_EXTERNAL_URL", "https://globifree.onrender.com"),
}

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


# =====================================================================
# STARTUP: AUTO-UPGRADE EXTRACTORS + SILENT 5-MIN KEEP-ALIVE BOT
# =====================================================================
def keep_alive_bot_loop():
    """Silently pings the Render website every 5 minutes (300s) so it never spins down."""
    time.sleep(5)
    while True:
        target_base = (
            os.environ.get("RENDER_EXTERNAL_URL")
            or BOT_STATE.get("target_url")
            or "https://globifree.onrender.com"
        ).rstrip("/")
        port = os.environ.get("PORT", "7860")

        for endpoint in (f"{target_base}/health", f"{target_base}/"):
            try:
                cffi_requests.get(
                    endpoint,
                    headers={"User-Agent": "GlobiFree-KeepAlive-Bot/4.0"},
                    impersonate="chrome",
                    timeout=8,
                )
            except Exception:
                pass

        try:
            cffi_requests.get(f"http://127.0.0.1:{port}/health", timeout=4)
        except Exception:
            pass

        BOT_STATE["pings"] = int(BOT_STATE.get("pings", 0)) + 1
        BOT_STATE["last_ping"] = time.strftime("%d %b, %I:%M:%S %p")
        time.sleep(300)


@app.on_event("startup")
def on_startup_tasks():
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
    threading.Thread(target=keep_alive_bot_loop, daemon=True).start()


# =====================================================================
# HELPER FUNCTIONS
# =====================================================================
def extract_clean_url(raw_input: Optional[str]) -> str:
    """Extracts the first valid http/https URL from any messy share text."""
    text = str(raw_input or "").strip()
    if not text:
        return ""
    m = re.search(r"(https?://[^\s\"'<>]+)", text, re.I)
    if m:
        return m.group(1).rstrip(".,;!?)]")
    return text


def is_youtube_url(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in ("youtube.com", "youtu.be", "youtube-nocookie.com"))


def extract_youtube_id(url: str) -> Optional[str]:
    patterns = [
        r"(?:v=|\/videos\/|embed\/|youtu\.be\/|\/v\/|\/e\/|watch\?v=|&v=|\/shorts\/|\/live\/)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def is_safe_media_url(dl_url: Optional[str]) -> bool:
    if not dl_url or not isinstance(dl_url, str) or not dl_url.startswith("http"):
        return False
    low = dl_url.lower()
    for bad in BLOCKED_AD_DOMAINS:
        if bad in low:
            return False
    return True


def is_url_actual_video(candidate_url: str, referer: str = "https://www.snapchat.com/") -> bool:
    """Verifies via fast HTTP Range check that a URL is an actual video stream (MP4/WebM) and NOT an image."""
    if not is_safe_media_url(candidate_url):
        return False
    low = candidate_url.lower().split("?")[0]
    if low.endswith((".jpg", ".jpeg", ".png", ".webp", ".svg")):
        return False
    if low.endswith((".mp4", ".webm", ".mov", ".m4v")):
        return True
    try:
        headers = {
            "User-Agent": CHROME_UA,
            "Referer": referer,
            "Range": "bytes=0-64",
        }
        resp = cffi_requests.get(
            candidate_url, headers=headers, impersonate="chrome", allow_redirects=True, timeout=4
        )
        ctype = (resp.headers.get("content-type") or "").lower()
        if "video/" in ctype:
            return True
        if "image/" in ctype or "text/html" in ctype:
            return False
        head_bytes = resp.content[:32] if resp.content else b""
        if b"ftyp" in head_bytes or b"moov" in head_bytes or head_bytes.startswith(b"\x1a\x45\xdf\xa3"):
            return True
    except Exception:
        pass
    return False


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
    if is_youtube_url(u):
        return "YOUTUBE"
    if "instagram.com" in u or "instagr.am" in u:
        return "INSTAGRAM"
    if "snapchat.com" in u or "snap.com" in u:
        return "SNAPCHAT"
    if "twitch.tv" in u:
        return "TWITCH"
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
# 1. DEDICATED FAST PLATFORM EXTRACTORS (< 0.5s)
# =====================================================================
def extract_youtube_thumb_fast(url: str) -> Dict[str, Any]:
    """Instant (~0.2s) YouTube Title & 4K Thumbnail extractor (Video/Audio download restricted)."""
    vid_id = extract_youtube_id(url)
    clean_watch_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else url

    title = "YouTube Video"
    uploader = "YouTube Creator"

    try:
        oemb = cffi_requests.get(
            f"https://www.youtube.com/oembed?url={quote(clean_watch_url)}&format=json",
            impersonate="chrome",
            timeout=2.5,
        )
        if oemb.status_code == 200:
            od = oemb.json()
            title = od.get("title") or title
            uploader = od.get("author_name") or uploader
    except Exception:
        pass

    hq_thumb = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg" if vid_id else ""
    max_thumb = f"https://i.ytimg.com/vi/{vid_id}/maxresdefault.jpg" if vid_id else hq_thumb

    return {
        "title": title,
        "uploader": uploader,
        "duration": 0,
        "duration_string": "Thumbnail Only",
        "thumbnail": hq_thumb,
        "hd_thumbnail": max_thumb,
        "platform": "YOUTUBE",
        "max_quality": "THUMBNAIL ONLY",
        "youtube_restricted": True,
    }


def extract_twitch_clip(url: str) -> Optional[Dict[str, Any]]:
    """Extracts Twitch Clips directly via Twitch GQL API with signed MP4 URLs in ~0.4s."""
    m = re.search(r"(?:twitch\.tv/[^/]+/clip/|clips\.twitch\.tv/)([A-Za-z0-9_-]+)", url)
    if not m:
        return None
    slug = m.group(1)
    try:
        headers = {
            "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
            "Content-Type": "application/json",
            "User-Agent": CHROME_UA,
        }
        gql_payload = [
            {
                "operationName": "VideoAccessToken_Clip",
                "variables": {"slug": slug},
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": "36b89d2507fce29e5ca551df756d27c1cfe079e2609642b4390aa4c35796eb11",
                    }
                },
            },
            {
                "query": (
                    "query($slug: ID!) { clip(slug: $slug) { title durationSeconds thumbnailURL "
                    "broadcaster { displayName login } } }"
                ),
                "variables": {"slug": slug},
            },
        ]
        resp = cffi_requests.post(
            "https://gql.twitch.tv/gql", json=gql_payload, headers=headers, impersonate="chrome", timeout=4.5
        )
        if resp.status_code == 200:
            results = resp.json()
            if isinstance(results, list) and len(results) >= 1:
                clip_token_data = ((results[0].get("data") or {}).get("clip")) or {}
                clip_meta = ((results[1].get("data") or {}).get("clip")) if len(results) > 1 else {}
                clip_meta = clip_meta or {}

                token_obj = clip_token_data.get("playbackAccessToken") or {}
                sig = token_obj.get("signature")
                token_val = token_obj.get("value")
                qualities = clip_token_data.get("videoQualities") or []

                video_url = None
                if qualities and sig and token_val:
                    qualities.sort(key=lambda q: int(q.get("quality") or 0), reverse=True)
                    base_src = qualities[0].get("sourceURL")
                    if base_src:
                        video_url = f"{base_src}?sig={sig}&token={quote(token_val)}"

                title = clip_meta.get("title") or f"Twitch Clip {slug}"
                broadcaster = (clip_meta.get("broadcaster") or {}).get("displayName") or "Twitch Streamer"
                duration = clip_meta.get("durationSeconds") or 0
                thumb = clip_meta.get("thumbnailURL") or ""

                if video_url or thumb:
                    return {
                        "title": title,
                        "uploader": broadcaster,
                        "duration": duration,
                        "thumbnail": thumb,
                        "video_url": video_url,
                        "image_url": thumb,
                    }
    except Exception:
        pass
    return None


def extract_instagram_media(url: str) -> Optional[Dict[str, Any]]:
    """Parallel fast Instagram Reels, Stories, Highlights & Posts extractor."""
    story_m = re.search(r"instagram\.com/stories/([^/?#]+)(?:/(\d+))?", url)
    if story_m:
        username = story_m.group(1)
        story_id = story_m.group(2) or ""
        story_path = f"stories/{username}/{story_id}".rstrip("/")

        for api_host in ("https://api.vxinstagram.com", "https://d.ddinstagram.com"):
            try:
                resp = cffi_requests.get(f"{api_host}/{story_path}", impersonate="chrome", timeout=4.0)
                if resp.status_code == 200 and "application/json" in (resp.headers.get("content-type") or ""):
                    data = resp.json()
                    media_list = data.get("media_extended") or data.get("mediaURLs") or []
                    video_url = None
                    thumb_url = ""
                    for item in media_list:
                        if isinstance(item, dict):
                            u_cand = item.get("url")
                            if item.get("type") == "video" or (u_cand and ".mp4" in u_cand):
                                video_url = u_cand
                            if item.get("thumbnail_url") and not thumb_url:
                                thumb_url = item.get("thumbnail_url")
                            elif u_cand and not thumb_url:
                                thumb_url = u_cand
                        elif isinstance(item, str):
                            if ".mp4" in item:
                                video_url = item
                            elif not thumb_url:
                                thumb_url = item
                    caption = data.get("text") or ""
                    if video_url or thumb_url:
                        return {
                            "title": (caption or f"Instagram_Story_{username}")[:80],
                            "uploader": data.get("user_name") or username,
                            "duration": 0,
                            "thumbnail": thumb_url,
                            "video_url": video_url if is_safe_media_url(video_url) else None,
                            "image_url": thumb_url if is_safe_media_url(thumb_url) else None,
                        }
            except Exception:
                pass

        cobalt_dl = fetch_from_cobalt_instances(url, "video", "1080")
        if cobalt_dl:
            return {
                "title": f"Instagram_Story_{username}",
                "uploader": f"@{username}",
                "duration": 0,
                "thumbnail": "",
                "video_url": cobalt_dl,
                "image_url": None,
            }

    m = re.search(r"instagram\.com/(?:[^/]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url)
    shortcode = m.group(1) if m else None
    if not shortcode:
        return None

    results: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def _try_vx():
        try:
            resp = cffi_requests.get(f"https://api.vxinstagram.com/p/{shortcode}", impersonate="chrome", timeout=3.5)
            if resp.status_code == 200 and "application/json" in (resp.headers.get("content-type") or ""):
                data = resp.json()
                media_list = data.get("media_extended") or data.get("mediaURLs") or []
                video_url = None
                thumb_url = ""
                for item in media_list:
                    if isinstance(item, dict):
                        u_cand = item.get("url")
                        if item.get("type") == "video" or (u_cand and ".mp4" in u_cand):
                            video_url = u_cand
                        if item.get("thumbnail_url") and not thumb_url:
                            thumb_url = item.get("thumbnail_url")
                        elif u_cand and not thumb_url:
                            thumb_url = u_cand
                    elif isinstance(item, str) and ".mp4" in item:
                        video_url = item
                caption = data.get("text") or ""
                if video_url or thumb_url:
                    with lock:
                        results.append(
                            {
                                "title": (caption or f"Instagram_{shortcode}")[:80],
                                "uploader": data.get("user_name") or "Instagram Creator",
                                "duration": 0,
                                "thumbnail": thumb_url,
                                "video_url": video_url if is_safe_media_url(video_url) else None,
                                "image_url": thumb_url if is_safe_media_url(thumb_url) else None,
                            }
                        )
        except Exception:
            pass

    def _try_embed():
        try:
            embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
            resp = cffi_requests.get(
                embed_url,
                headers={"User-Agent": CHROME_UA, "Accept-Language": "en-US,en;q=0.9"},
                impersonate="chrome",
                timeout=4.0,
            )
            if resp.status_code == 200:
                page = resp.text
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
                    with lock:
                        results.append(
                            {
                                "title": f"Instagram_{shortcode}",
                                "uploader": uploader,
                                "duration": 0,
                                "thumbnail": thumb_url,
                                "video_url": video_url if is_safe_media_url(video_url) else None,
                                "image_url": thumb_url if is_safe_media_url(thumb_url) else None,
                            }
                        )
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_try_vx)
        f2 = ex.submit(_try_embed)
        for f in as_completed([f1, f2], timeout=4.2):
            try:
                f.result()
            except Exception:
                pass

    if results:
        best = results[0]
        for r in results[1:]:
            if not best.get("video_url") and r.get("video_url"):
                best["video_url"] = r["video_url"]
            if not best.get("thumbnail") and r.get("thumbnail"):
                best["thumbnail"] = r["thumbnail"]
        return best
    return None


def extract_snapchat_media(url: str) -> Optional[Dict[str, Any]]:
    """Extracts verified MP4 video and HD thumbnail from Snapchat Spotlight and Stories."""
    try:
        resp = cffi_requests.get(
            url,
            headers={"User-Agent": CHROME_UA, "Accept-Language": "en-US,en;q=0.9"},
            impersonate="chrome",
            allow_redirects=True,
            timeout=5.0,
        )
        resolved_url = str(resp.url) or url
        page = resp.text if resp.status_code == 200 else ""

        spot_m = re.search(r"/spotlight/([A-Za-z0-9_-]+)", resolved_url) or re.search(
            r"/spotlight/([A-Za-z0-9_-]+)", url
        )
        canonical_url = f"https://www.snapchat.com/spotlight/{spot_m.group(1)}" if spot_m else resolved_url
        if spot_m and "/@" in resolved_url:
            try:
                c_resp = cffi_requests.get(
                    canonical_url,
                    headers={"User-Agent": CHROME_UA, "Accept-Language": "en-US,en;q=0.9"},
                    impersonate="chrome",
                    allow_redirects=True,
                    timeout=4.5,
                )
                if c_resp.status_code == 200 and "__NEXT_DATA__" in c_resp.text:
                    page = c_resp.text
            except Exception:
                pass

        title = "Snapchat Spotlight Video"
        uploader = "Snapchat Creator"
        thumb_url = ""
        video_url = None
        duration = 0
        candidate_videos: List[str] = []

        ld_blocks = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page, re.S | re.I
        )
        for block in ld_blocks:
            try:
                ld_data = json.loads(block.strip())
                items = ld_data if isinstance(ld_data, list) else [ld_data]
                for item in items:
                    if isinstance(item, dict) and item.get("@type") == "VideoObject":
                        if item.get("contentUrl"):
                            candidate_videos.append(item["contentUrl"])
                        if item.get("thumbnailUrl"):
                            t_val = item["thumbnailUrl"]
                            thumb_url = t_val[0] if isinstance(t_val, list) and t_val else str(t_val)
                        if item.get("name") or item.get("description"):
                            title = item.get("name") or item.get("description")
                        creator = item.get("creator") or item.get("author") or {}
                        if isinstance(creator, dict) and creator.get("name"):
                            uploader = creator["name"]
            except Exception:
                pass

        next_m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
            page,
            re.S,
        )
        if next_m:
            try:
                next_json = json.loads(next_m.group(1))
                page_props = (next_json.get("props") or {}).get("pageProps") or {}

                vid_meta = page_props.get("videoMetadata") or {}
                if isinstance(vid_meta, dict):
                    if vid_meta.get("contentUrl"):
                        candidate_videos.insert(0, vid_meta["contentUrl"])
                    if vid_meta.get("thumbnailUrl") and not thumb_url:
                        thumb_url = vid_meta["thumbnailUrl"]
                    if vid_meta.get("name") or vid_meta.get("description"):
                        title = vid_meta.get("name") or vid_meta.get("description") or title
                    creator_obj = vid_meta.get("creator") or {}
                    if isinstance(creator_obj, dict) and creator_obj.get("name"):
                        uploader = creator_obj["name"]

                def _walk_snap(obj: Any):
                    if isinstance(obj, dict):
                        snap_urls = obj.get("snapUrls")
                        if isinstance(snap_urls, dict) and snap_urls.get("mediaUrl"):
                            if obj.get("snapMediaType") == 1:
                                candidate_videos.insert(0, snap_urls["mediaUrl"])
                            else:
                                candidate_videos.append(snap_urls["mediaUrl"])
                            prev_u = snap_urls.get("mediaPreviewUrl")
                            if isinstance(prev_u, dict) and prev_u.get("value"):
                                nonlocal thumb_url
                                if not thumb_url:
                                    thumb_url = prev_u["value"]
                        for k, v in obj.items():
                            if k in ("contentUrl", "videoUrl", "mediaUrl") and isinstance(v, str) and v.startswith("http"):
                                candidate_videos.append(v)
                            else:
                                _walk_snap(v)
                    elif isinstance(obj, list):
                        for elem in obj:
                            _walk_snap(elem)

                _walk_snap(page_props)
            except Exception:
                pass

        for pat in (
            r'<link[^>]+rel=["\']preload["\'][^>]+as=["\']video["\'][^>]+href=["\']([^"\']+)["\']',
            r'<meta[^>]+property=["\']og:video(?::secure_url|:url)?["\'][^>]+content=["\']([^"\']+)["\']',
            r'<video[^>]+src=["\'](https?://[^"\']+)["\']',
        ):
            m_vid = re.search(pat, page, re.I)
            if m_vid:
                candidate_videos.append(html.unescape(m_vid.group(1)))

        if not thumb_url:
            og_i = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', page, re.I)
            if og_i:
                thumb_url = html.unescape(og_i.group(1))

        og_t = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', page, re.I)
        if og_t and title == "Snapchat Spotlight Video":
            title = html.unescape(og_t.group(1)).strip()

        seen = set()
        for cand in candidate_videos:
            clean_c = cand.replace("\\u0026", "&").replace("\\/", "/")
            if clean_c in seen:
                continue
            seen.add(clean_c)
            if is_url_actual_video(clean_c, referer="https://www.snapchat.com/"):
                video_url = clean_c
                break

        return {
            "title": title[:90],
            "uploader": uploader,
            "duration": duration,
            "thumbnail": thumb_url,
            "video_url": video_url,
            "image_url": thumb_url if is_safe_media_url(thumb_url) else None,
            "canonical_url": canonical_url,
        }
    except Exception:
        return None


def extract_tiktok_no_watermark(url: str) -> Optional[Dict[str, Any]]:
    """Fetches TikTok HD No-Watermark video & metadata via TikWM API."""
    try:
        resp = cffi_requests.post(
            "https://www.tikwm.com/api/",
            data={"url": url, "hd": 1},
            impersonate="chrome",
            timeout=5.0,
        )
        if resp.status_code == 200:
            payload = resp.json()
            data = payload.get("data") or {}
            video_url = data.get("hdplay") or data.get("play")
            if video_url and not str(video_url).startswith("http"):
                video_url = f"https://www.tikwm.com{video_url}"
            raw_title = data.get("title") or "TikTok Video"
            author_name = (data.get("author") or {}).get("nickname") or "TikTok Creator"
            if is_safe_media_url(video_url):
                return {
                    "title": raw_title[:90],
                    "uploader": author_name,
                    "duration": data.get("duration") or 0,
                    "thumbnail": data.get("origin_cover") or data.get("cover") or "",
                    "video_url": video_url,
                    "audio_url": data.get("music") or video_url,
                }
    except Exception:
        pass
    return None


def extract_twitter_x_api(url: str) -> Optional[Dict[str, Any]]:
    """Extracts X/Twitter videos & GIFs via fxtwitter API."""
    try:
        m = re.search(r"(?:twitter\.com|x\.com)/([^/?#]+)/status/(\d+)", url)
        if not m:
            return None
        user, status_id = m.group(1), m.group(2)
        api_url = f"https://api.fxtwitter.com/{user}/status/{status_id}"
        resp = cffi_requests.get(api_url, impersonate="chrome", timeout=4.5)
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
            full_text = tweet.get("text") or f"X Post {status_id}"
            if video_url or thumb:
                return {
                    "title": full_text[:90],
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
        resp = cffi_requests.get(api_url, impersonate="chrome", timeout=5.0)
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
                            emb_resp = cffi_requests.get(embed_url, impersonate="chrome", timeout=4.0)
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
    """Fast OpenGraph + CDN scraper for Behance, Flickr, 500px, Pinterest, Facebook, Reddit, Vimeo."""
    try:
        headers = {
            "User-Agent": CHROME_UA,
            "Referer": f"https://{urlparse(url).netloc}/",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = cffi_requests.get(url, headers=headers, impersonate="chrome", allow_redirects=True, timeout=5.0)
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
            flickr_imgs = re.findall(
                r'(?:https?:)?//live\.staticflickr\.com/\d+/\d+_[\da-fA-F]+_[okhbclz]\.(?:jpg|png)', page
            )
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
                        timeout=4.0,
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

        if video_url or best_img or title:
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
    """Queries Cobalt v10 instances in parallel for fast direct streams."""
    q_val = quality if quality in ["1080", "720", "480", "360"] else "1080"
    cobalt_hosts = [
        "https://cobalt-api.kwiatekmiki.com",
        "https://cobalt-backend.canine.tools",
        "https://capi.3kh0.net",
        "https://api.cobalt.blackcat.sweeux.org",
    ]

    def _try_host(host: str) -> Optional[str]:
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
            resp = cffi_requests.post(
                f"{host}/", json=payload, headers=headers, impersonate="chrome", timeout=4.0
            )
            if resp.status_code == 200:
                data = resp.json()
                dl_url = data.get("url")
                if is_safe_media_url(dl_url):
                    return dl_url
                picker = data.get("picker") or []
                if picker and is_safe_media_url(picker[0].get("url")):
                    return picker[0]["url"]
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_try_host, h) for h in cobalt_hosts]
        for f in as_completed(futures, timeout=4.5):
            try:
                res = f.result()
                if res:
                    return res
            except Exception:
                pass
    return None


# =====================================================================
# 2. ULTRA-FAST INFO FETCH (< 0.6 SECOND)
# =====================================================================
def fast_fetch_info(raw_url: str) -> dict:
    url = extract_clean_url(raw_url)
    if not url:
        raise HTTPException(status_code=400, detail="Valid URL is required.")

    platform = detect_platform_name(url)
    cache_key = url.strip()

    with CACHE_LOCK:
        cached_info = INFO_CACHE.get(cache_key)
        if cached_info and (time.time() - cached_info.get("_ts", 0) < 300):
            return {k: v for k, v in cached_info.items() if not k.startswith("_")}

    if platform == "YOUTUBE":
        yt_result = extract_youtube_thumb_fast(url)
        with CACHE_LOCK:
            INFO_CACHE[cache_key] = {**yt_result, "_ts": time.time()}
        return yt_result

    if platform == "TWITCH":
        tw_clip = extract_twitch_clip(url)
        if tw_clip:
            result = {
                "title": tw_clip["title"],
                "uploader": tw_clip["uploader"],
                "duration": tw_clip["duration"],
                "duration_string": format_duration(tw_clip["duration"]),
                "thumbnail": tw_clip["thumbnail"],
                "hd_thumbnail": tw_clip["image_url"] or tw_clip["thumbnail"],
                "platform": platform,
                "max_quality": "1080P CLIP",
            }
            with CACHE_LOCK:
                INFO_CACHE[cache_key] = {
                    **result,
                    "_video_url": tw_clip.get("video_url"),
                    "_ts": time.time(),
                }
            return result

    if platform == "INSTAGRAM":
        ig = extract_instagram_media(url)
        if ig:
            result = {
                "title": ig["title"],
                "uploader": ig["uploader"],
                "duration": 0,
                "duration_string": "Story / Reel HD",
                "thumbnail": ig["thumbnail"],
                "hd_thumbnail": ig["image_url"] or ig["thumbnail"],
                "platform": platform,
                "max_quality": "1080P HD",
            }
            with CACHE_LOCK:
                INFO_CACHE[cache_key] = {
                    **result,
                    "_video_url": ig.get("video_url"),
                    "_ts": time.time(),
                }
            return result

    if platform == "SNAPCHAT":
        sc = extract_snapchat_media(url)
        if sc:
            result = {
                "title": sc["title"],
                "uploader": sc["uploader"],
                "duration": sc["duration"],
                "duration_string": "Snap Video Ready" if sc.get("video_url") else "Snap HD Ready",
                "thumbnail": sc["thumbnail"],
                "hd_thumbnail": sc["image_url"] or sc["thumbnail"],
                "platform": platform,
                "max_quality": "HD VIDEO",
            }
            with CACHE_LOCK:
                INFO_CACHE[cache_key] = {
                    **result,
                    "_video_url": sc.get("video_url"),
                    "_canonical_url": sc.get("canonical_url"),
                    "_ts": time.time(),
                }
            return result

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
                INFO_CACHE[cache_key] = {
                    **result,
                    "_video_url": tk.get("video_url"),
                    "_audio_url": tk.get("audio_url"),
                    "_ts": time.time(),
                }
            return result

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
                INFO_CACHE[cache_key] = {
                    **result,
                    "_video_url": tw.get("video_url"),
                    "_ts": time.time(),
                }
            return result

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
                INFO_CACHE[cache_key] = {
                    **result,
                    "_video_url": art.get("video_url"),
                    "_embed_url": art.get("embed_url"),
                    "_ts": time.time(),
                }
            return result

    scraped = extract_design_or_photo_page(url, platform)
    if scraped and (scraped.get("thumbnail") or scraped.get("video_url")):
        result = {
            "title": scraped["title"],
            "uploader": scraped["uploader"],
            "duration": 0,
            "duration_string": "HD Ready",
            "thumbnail": scraped.get("thumbnail") or "",
            "hd_thumbnail": scraped.get("image_url") or scraped.get("thumbnail") or "",
            "platform": platform,
            "max_quality": "HD READY",
        }
        with CACHE_LOCK:
            INFO_CACHE[cache_key] = {
                **result,
                "_video_url": scraped.get("video_url"),
                "_ts": time.time(),
            }
        return result

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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
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

    return {
        "title": (scraped or {}).get("title") or f"{platform.title()} Media",
        "uploader": f"{platform.title()} Creator",
        "duration": 0,
        "duration_string": "HD Ready",
        "thumbnail": "",
        "hd_thumbnail": "",
        "platform": platform,
        "max_quality": "HD READY",
    }


# =====================================================================
# 3. UNIVERSAL MULTI-ENGINE VIDEO / AUDIO DOWNLOADER
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
    """Downloads a direct Video/Audio URL and strictly rejects static images when mode is video/audio."""
    try:
        headers = {"User-Agent": CHROME_UA}
        if referer:
            headers["Referer"] = referer
        resp = cffi_requests.get(
            direct_url, headers=headers, impersonate="chrome", allow_redirects=True, timeout=45
        )
        if resp.status_code != 200 or not resp.content:
            return None

        ctype = (resp.headers.get("content-type") or "").lower()
        if "image/" in ctype or "text/html" in ctype:
            return None

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
        if f.is_file()
        and not f.name.endswith((".part", ".ytdl", ".jpg", ".jpeg", ".png", ".webp"))
        and f.stat().st_size > 0
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


def download_with_gallery_dl_video(url: str, job_dir: Path) -> Optional[Path]:
    """Fallback using gallery-dl that returns video files for the Video Download bar."""
    gdl_dir = job_dir / "gdl"
    gdl_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["gallery-dl", "--no-mtime", "--range", "1", "-D", str(gdl_dir)]
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]
    cmd.append(url)
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        vids = [
            f
            for f in gdl_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in (".mp4", ".webm", ".mov", ".gif") and f.stat().st_size > 0
        ]
        if vids:
            return vids[0]
    except Exception:
        pass
    return None


def download_universal_media(
    raw_url: str,
    job_dir: Path,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool,
) -> Path:
    url = extract_clean_url(raw_url)
    if is_youtube_url(url):
        raise RuntimeError(YOUTUBE_RESTRICTION_MESSAGE)

    platform = detect_platform_name(url)

    with CACHE_LOCK:
        cached = INFO_CACHE.get(url.strip()) or {}
    cached_vid_url = (
        cached.get("_audio_url") if (mode == "audio" and cached.get("_audio_url")) else cached.get("_video_url")
    )
    if cached_vid_url:
        res = download_direct_url_to_file(
            cached_vid_url,
            job_dir,
            mode,
            format_type,
            quality,
            is_muted,
            referer=f"https://{urlparse(url).netloc}/",
        )
        if res:
            return res

    if platform == "TWITCH":
        tw_clip = extract_twitch_clip(url)
        if tw_clip and tw_clip.get("video_url"):
            res = download_direct_url_to_file(
                tw_clip["video_url"], job_dir, mode, format_type, quality, is_muted, referer="https://www.twitch.tv/"
            )
            if res:
                return res

    if platform == "SNAPCHAT":
        sc = extract_snapchat_media(url)
        if sc:
            if sc.get("video_url"):
                res = download_direct_url_to_file(
                    sc["video_url"], job_dir, mode, format_type, quality, is_muted, referer="https://www.snapchat.com/"
                )
                if res:
                    return res
            if sc.get("canonical_url"):
                res = download_with_ytdlp(
                    sc["canonical_url"],
                    job_dir,
                    mode,
                    format_type,
                    quality,
                    is_muted,
                    referer="https://www.snapchat.com/",
                )
                if res:
                    return res

    if platform == "INSTAGRAM":
        ig = extract_instagram_media(url)
        if ig and ig.get("video_url"):
            res = download_direct_url_to_file(
                ig["video_url"], job_dir, mode, format_type, quality, is_muted, referer="https://www.instagram.com/"
            )
            if res:
                return res

    if platform == "TIKTOK":
        tk = extract_tiktok_no_watermark(url)
        if tk:
            target_dl = tk["audio_url"] if mode == "audio" else tk["video_url"]
            res = download_direct_url_to_file(target_dl, job_dir, mode, format_type, quality, is_muted)
            if res:
                return res

    if platform == "X / TWITTER":
        tw = extract_twitter_x_api(url)
        if tw and tw.get("video_url"):
            res = download_direct_url_to_file(tw["video_url"], job_dir, mode, format_type, quality, is_muted)
            if res:
                return res

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

    cobalt_stream = fetch_from_cobalt_instances(url, mode, quality)
    if cobalt_stream:
        res = download_direct_url_to_file(cobalt_stream, job_dir, mode, format_type, quality, is_muted)
        if res:
            return res

    res = download_with_ytdlp(url, job_dir, mode, format_type, quality, is_muted)
    if res:
        return res

    scraped = extract_design_or_photo_page(url, platform)
    if scraped and scraped.get("video_url"):
        res = download_direct_url_to_file(
            scraped["video_url"],
            job_dir,
            mode,
            format_type,
            quality,
            is_muted,
            referer=f"https://{urlparse(url).netloc}/",
        )
        if res:
            return res

    gdl_vid = download_with_gallery_dl_video(url, job_dir)
    if gdl_vid:
        return gdl_vid

    raise RuntimeError(
        f"No video stream found on this {platform.title()} link (or the post is a photo — use the Left Photo & Thumbnail Box to save images)."
    )


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
    raw_url: str,
    mode: str,
    format_type: str,
    quality: str,
    is_muted: bool,
    safe_header_title: str,
    start_sec: Optional[float],
    end_sec: Optional[float],
):
    url = extract_clean_url(raw_url)
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    platform = detect_platform_name(url)
    has_trim = start_sec is not None and end_sec is not None and end_sec > start_sec

    try:
        update_job(job_id, progress=55)
        downloaded_file = download_universal_media(
            url, job_dir, mode, format_type, quality, is_muted and not has_trim
        )

        if has_trim:
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
# 6. API ROUTES (INCLUDING SEO ROBOTS.TXT & SITEMAP.XML)
# =====================================================================
@app.api_route("/health", methods=["GET", "HEAD"])
def health_check():
    return {"status": "ok", "service": "GlobiFree.Pro"}


@app.get("/", response_class=HTMLResponse)
def serve_home():
    html_path = Path("index.html")
    return html_path.read_text(encoding="utf-8") if html_path.exists() else "<h1>index.html not found</h1>"


@app.get("/robots.txt", response_class=Response)
def get_robots():
    content = """User-agent: *
Allow: /

Sitemap: https://globifree.onrender.com/sitemap.xml
"""
    return Response(content=content, media_type="text/plain")


@app.get("/sitemap.xml", response_class=Response)
def get_sitemap():
    today_date = time.strftime("%Y-%m-%d")
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://globifree.onrender.com/</loc>
    <lastmod>{today_date}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>"""
    return Response(content=content, media_type="application/xml")


@app.api_route("/api/info", methods=["GET", "POST"])
def api_info(
    url_form: Optional[str] = Form(None, alias="url"),
    url_query: Optional[str] = Query(None, alias="url"),
):
    target_url = extract_clean_url(url_form or url_query or "")
    if not target_url:
        raise HTTPException(status_code=400, detail="URL is required.")
    return fast_fetch_info(target_url)


@app.get("/api/thumbnail")
def download_hd_thumbnail(
    background_tasks: BackgroundTasks,
    url: str = Query(...),
    title: str = Query("GlobiFree_Image"),
    ext: str = Query("jpg"),
):
    clean_url = extract_clean_url(url)
    clean_ext = ext.lower() if ext.lower() in ("jpg", "png", "webp") else "jpg"
    info = fast_fetch_info(clean_url)
    thumb_url = info.get("hd_thumbnail") or info.get("thumbnail")
    if not thumb_url:
        raise HTTPException(status_code=404, detail="High-res image or thumbnail not found.")

    safe_title = ascii_safe_filename(title)
    thumb_dir = TEMP_DIR / f"thumb_{uuid.uuid4()}"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    raw_img_path = thumb_dir / "raw_image"
    final_img_path = thumb_dir / f"{safe_title}.{clean_ext}"

    try:
        headers = {"User-Agent": CHROME_UA, "Referer": f"https://{urlparse(clean_url).netloc}/"}
        resp = cffi_requests.get(thumb_url, headers=headers, impersonate="chrome", timeout=10)
        if resp.status_code != 200 and info.get("thumbnail"):
            resp = cffi_requests.get(info["thumbnail"], headers=headers, impersonate="chrome", timeout=10)
        raw_img_path.write_bytes(resp.content)

        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(raw_img_path), str(final_img_path)],
            capture_output=True,
            timeout=20,
        )
        target_send = final_img_path if (proc.returncode == 0 and final_img_path.exists()) else raw_img_path

        background_tasks.add_task(cleanup_dir, thumb_dir)
        return FileResponse(
            path=str(target_send),
            filename=f"{safe_title}_HD.{clean_ext}",
            media_type=f"image/{'jpeg' if clean_ext == 'jpg' else clean_ext}",
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
    clean_url = extract_clean_url(url)
    if not clean_url:
        raise HTTPException(status_code=400, detail="Valid URL is required.")
    if is_youtube_url(clean_url):
        raise HTTPException(status_code=400, detail=YOUTUBE_RESTRICTION_MESSAGE)

    cleanup_old_temp_files()
    is_muted = mute_video.lower() == "true"
    start_sec = time_str_to_seconds(start_time)
    end_sec = time_str_to_seconds(end_time)
    has_trim = start_sec is not None and end_sec is not None and end_sec > start_sec

    if not custom_title:
        try:
            meta = fast_fetch_info(clean_url)
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
            clean_url,
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
