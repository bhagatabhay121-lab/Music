"""
YouTube Audio/Video URL Proxy
==============================
Extracts stream URLs using yt-dlp and serves them to your Flutter app.
Supports multiple quality levels for both video and audio-only downloads.

Install:
    pip install fastapi uvicorn yt-dlp

Run:
    python server.py
    (or: uvicorn server:app --host 0.0.0.0 --port 8080 --reload)
"""

import asyncio
import logging
from typing import Optional

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="YT Audio/Video Proxy", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Shared yt-dlp base options ─────────────────────────────────────────────────
_BASE_OPTS = {
    "skip_download": True,
    "quiet": True,
    "no_warnings": True,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    },
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios", "web"],
        }
    },
}

# ── Format selectors per quality ───────────────────────────────────────────────
# Video quality → yt-dlp format string
_VIDEO_FORMATS = {
    "2160p": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[height<=2160][ext=mp4]/best",
    "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
    "720p":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
    "480p":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best",
    "360p":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best",
    "best":  "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
}

# Audio quality → yt-dlp format string (audio-only)
_AUDIO_FORMATS = {
    "high":   "bestaudio[abr>=128]/bestaudio",   # ~160-320 kbps
    "medium": "bestaudio[abr<=128]/bestaudio",   # ~128 kbps
    "low":    "worstaudio",                       # lowest for data saving
    "best":   "bestaudio/best",
}


# ── Response models ────────────────────────────────────────────────────────────
class StreamResponse(BaseModel):
    video_id: str
    title: str
    url: str          # primary stream URL
    ext: str
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    abr: Optional[float] = None   # audio bitrate kbps


class QualityInfo(BaseModel):
    """Available qualities for a video."""
    video_id: str
    title: str
    thumbnail: Optional[str]
    duration: Optional[int]
    video_qualities: list[str]   # e.g. ["2160p","1080p","720p","480p","360p"]
    audio_qualities: list[str]   # e.g. ["high","medium","low"]


# ── In-memory caches ───────────────────────────────────────────────────────────
_info_cache: dict[str, dict] = {}           # raw yt-dlp info per video_id
_stream_cache: dict[str, StreamResponse] = {}  # resolved streams per cache key


def _cache_key(video_id: str, mode: str, quality: str) -> str:
    return f"{video_id}:{mode}:{quality}"


# ── Core extraction ────────────────────────────────────────────────────────────
def _extract_info(video_id: str) -> dict:
    """Extract full yt-dlp info dict (cached, format-agnostic)."""
    if video_id in _info_cache:
        return _info_cache[video_id]

    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    log.info("Fetching info: %s", yt_url)

    opts = dict(_BASE_OPTS)
    opts["format"] = "bestaudio/best"   # doesn't matter — we only want info

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(yt_url, download=False)

    if not info:
        raise ValueError("yt-dlp returned no info")

    _info_cache[video_id] = info
    return info


def _extract_stream(video_id: str, fmt_selector: str) -> dict:
    """Extract a stream URL using a specific format selector."""
    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    log.info("Extracting stream  id=%s  fmt=%r", video_id, fmt_selector)

    opts = dict(_BASE_OPTS)
    opts["format"] = fmt_selector

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(yt_url, download=False)

    if not info:
        raise ValueError("yt-dlp returned no info")
    return info


def _pick_url(info: dict) -> tuple[str, str, int | None, int | None, float | None]:
    """
    Pick the best stream URL from a yt-dlp info dict.
    Returns (url, ext, width, height, abr).
    """
    url = info.get("url")
    if url:
        return (
            url,
            info.get("ext", "mp4"),
            info.get("width"),
            info.get("height"),
            info.get("abr"),
        )

    # Merged/muxed format — try requested_formats first
    fmts = info.get("requested_formats") or info.get("formats") or []
    if fmts:
        # Prefer video format with highest resolution that has a URL
        video_fmts = [f for f in fmts if f.get("vcodec") != "none" and f.get("url")]
        if video_fmts:
            video_fmts.sort(key=lambda f: (f.get("height") or 0), reverse=True)
            best = video_fmts[0]
            return (
                best["url"],
                best.get("ext", "mp4"),
                best.get("width"),
                best.get("height"),
                best.get("abr"),
            )

        audio_fmts = [f for f in fmts if f.get("vcodec") == "none" and f.get("url")]
        if audio_fmts:
            audio_fmts.sort(key=lambda f: f.get("abr") or 0, reverse=True)
            best = audio_fmts[0]
            return (
                best["url"],
                best.get("ext", "webm"),
                None,
                None,
                best.get("abr"),
            )

    raise ValueError("No stream URL found in yt-dlp output")


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok", "message": "YT Audio/Video Proxy v2 is running"}


@app.get("/info", response_model=QualityInfo)
async def get_info(
    id: str = Query(..., description="YouTube video ID"),
):
    """
    Returns available quality options for a video without fetching stream URLs.
    Flutter calls this first to populate the quality picker.
    """
    video_id = _validate_id(id)
    try:
        info = await asyncio.to_thread(_extract_info, video_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    fmts = info.get("formats") or []
    heights = sorted(
        {f.get("height") for f in fmts if f.get("height") and f.get("vcodec") != "none"},
        reverse=True,
    )
    # Map to named quality labels
    q_labels = []
    for h in heights:
        if h >= 2160:
            q_labels.append("2160p")
        elif h >= 1080:
            q_labels.append("1080p")
        elif h >= 720:
            q_labels.append("720p")
        elif h >= 480:
            q_labels.append("480p")
        elif h >= 360:
            q_labels.append("360p")
    # Deduplicate while preserving order
    seen = set()
    q_labels = [x for x in q_labels if not (x in seen or seen.add(x))]
    if not q_labels:
        q_labels = ["best"]

    return QualityInfo(
        video_id=video_id,
        title=info.get("title", ""),
        thumbnail=info.get("thumbnail"),
        duration=info.get("duration"),
        video_qualities=q_labels,
        audio_qualities=["high", "medium", "low"],
    )


@app.get("/audio", response_model=StreamResponse)
async def get_audio_url(
    id: str = Query(..., description="YouTube video ID"),
    quality: str = Query("best", description="audio quality: high | medium | low | best"),
    nocache: bool = Query(False),
):
    """
    Returns the best audio-only stream URL.
    Used by Flutter for background music playback.
    """
    video_id = _validate_id(id)
    q = quality if quality in _AUDIO_FORMATS else "best"
    key = _cache_key(video_id, "audio", q)

    if not nocache and key in _stream_cache:
        log.info("Cache hit: %s", key)
        return _stream_cache[key]

    fmt = _AUDIO_FORMATS[q]
    try:
        info = await asyncio.to_thread(_extract_stream, video_id, fmt)
        url, ext, w, h, abr = _pick_url(info)
        result = StreamResponse(
            video_id=video_id,
            title=info.get("title", ""),
            url=url,
            ext=ext,
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail"),
            width=w,
            height=h,
            abr=abr,
        )
        _stream_cache[key] = result
        log.info("OK  title=%r  quality=%s  abr=%s", result.title, q, abr)
        return result
    except yt_dlp.utils.DownloadError as e:
        _handle_ydl_error(video_id, e)
    except Exception as e:
        log.exception("Unexpected error for %s", video_id)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/video", response_model=StreamResponse)
async def get_video_url(
    id: str = Query(..., description="YouTube video ID"),
    quality: str = Query("720p", description="video quality: 2160p|1080p|720p|480p|360p|best"),
    nocache: bool = Query(False),
):
    """
    Returns a video stream URL at the requested quality.
    Note: YouTube serves video+audio as separate streams for 720p+.
    For download, Flutter should prefer the /audio endpoint to get a
    single-file audio stream; for in-app playback, use /audio (video_player
    handles it). This endpoint is provided for explicit video downloads.
    """
    video_id = _validate_id(id)
    q = quality if quality in _VIDEO_FORMATS else "720p"
    key = _cache_key(video_id, "video", q)

    if not nocache and key in _stream_cache:
        log.info("Cache hit: %s", key)
        return _stream_cache[key]

    fmt = _VIDEO_FORMATS[q]
    try:
        info = await asyncio.to_thread(_extract_stream, video_id, fmt)
        url, ext, w, h, abr = _pick_url(info)
        result = StreamResponse(
            video_id=video_id,
            title=info.get("title", ""),
            url=url,
            ext=ext,
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail"),
            width=w,
            height=h,
            abr=abr,
        )
        _stream_cache[key] = result
        log.info("OK  title=%r  quality=%s  %sx%s", result.title, q, w, h)
        return result
    except yt_dlp.utils.DownloadError as e:
        _handle_ydl_error(video_id, e)
    except Exception as e:
        log.exception("Unexpected error for %s", video_id)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/cache")
def clear_cache():
    count = len(_stream_cache) + len(_info_cache)
    _stream_cache.clear()
    _info_cache.clear()
    return {"cleared": count}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _validate_id(video_id: str) -> str:
    video_id = video_id.strip()
    if not video_id or len(video_id) > 20 or not video_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid video ID")
    return video_id


def _handle_ydl_error(video_id: str, e: yt_dlp.utils.DownloadError):
    err = str(e)
    log.error("yt-dlp DownloadError for %s: %s", video_id, err)
    if "Sign in" in err or "login" in err.lower():
        raise HTTPException(status_code=403, detail="Video requires sign-in")
    if "not available" in err.lower():
        raise HTTPException(status_code=404, detail="Video not available")
    raise HTTPException(status_code=502, detail=f"Extraction failed: {err}")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    print("\n" + "=" * 60)
    print("  YT Audio/Video Proxy v2  →  http://0.0.0.0:8080")
    print("  Docs:  http://localhost:8080/docs")
    print("  Endpoints:")
    print("    GET /audio?id=VIDEO_ID&quality=high|medium|low|best")
    print("    GET /video?id=VIDEO_ID&quality=1080p|720p|480p|360p|best")
    print("    GET /info?id=VIDEO_ID   (available qualities)")
    print("=" * 60 + "\n")

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8080,
        log_level="info",
    )
