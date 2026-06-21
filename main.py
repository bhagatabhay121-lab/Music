import base64

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import requests
from urllib.parse import quote
from Crypto.Cipher import DES

app = FastAPI(title="Saavn Proxy API")

# Enable CORS so your HTML/Flutter app can call this freely
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_URL = "www.jiosaavn.com"
API_STR = "/api.php?_format=json&_marker=0&ctx=web6dot0"

ENDPOINTS = {
    'homeData': '__call=webapi.getLaunchData',
    'topSearches': '__call=content.getTopSearches',
    'fromToken': '__call=webapi.get',
    'featuredRadio': '__call=webradio.createFeaturedStation',
    'artistRadio': '__call=webradio.createArtistStation',
    'entityRadio': '__call=webradio.createEntityStation',
    'radioSongs': '__call=webradio.getSong',
    'songDetails': '__call=song.getDetails',
    'playlistDetails': '__call=playlist.getDetails',
    'albumDetails': '__call=content.getAlbumDetails',
    'getResults': '__call=search.getResults',
    'albumResults': '__call=search.getAlbumResults',
    'artistResults': '__call=search.getArtistResults',
    'playlistResults': '__call=search.getPlaylistResults',
    'getReco': '__call=reco.getreco',
    'getAlbumReco': '__call=reco.getAlbumReco',
    'artistOtherTopSongs': '__call=search.artistOtherTopSongs',
    'artistDetails': '__call=artist.getArtistPageDetails',
    'artistMoreSongs': '__call=artist.getArtistMoreSong',
    'artistMoreAlbums': '__call=artist.getArtistMoreAlbum',
}

# ============================================================
# JioSaavn media-URL decryption
# ------------------------------------------------------------
# JioSaavn encrypts every streamable URL with DES (ECB mode,
# PKCS5 padding) under a fixed, long-public key. This is the same
# scheme every unofficial JioSaavn client/API uses to play songs
# (it's obfuscation, not real DRM, for `encrypted_media_url`;
# `encrypted_drm_media_url` is the actual widevine/DRM-protected
# stream and is NOT decrypted here since that needs a license
# server, not just this key).
#
# Decrypting `encrypted_media_url` yields a plain CDN URL that
# already targets 96kbps, e.g.:
#   https://aac.saavncdn.com/.../<hash>_96.mp4
# Every other bitrate JioSaavn serves is the exact same path with
# the suffix swapped (_12 / _48 / _96 / _160 / _320), so we just
# string-replace to build the full quality ladder.
# ============================================================
DES_KEY = b"38346591"
QUALITY_SUFFIX = {
    "12kbps": "_12.mp4",
    "48kbps": "_48.mp4",
    "96kbps": "_96.mp4",
    "160kbps": "_160.mp4",
    "320kbps": "_320.mp4",
}


def _pkcs5_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 8:
        return data
    return data[:-pad_len]


def decrypt_media_url(encrypted: str):
    """Decrypt a JioSaavn `encrypted_media_url` into a plain CDN URL.
    Returns None if it isn't decryptable (e.g. not actually DES/base64)."""
    if not encrypted or not isinstance(encrypted, str):
        return None
    try:
        raw = base64.b64decode(encrypted.strip())
        cipher = DES.new(DES_KEY, DES.MODE_ECB)
        plain = _pkcs5_unpad(cipher.decrypt(raw))
        return plain.decode("utf-8")
    except Exception:
        return None


def build_quality_urls(decrypted_url: str) -> dict:
    """Turn the default-quality decrypted URL into the full ladder of
    available bitrates by swapping the trailing _96.mp4 suffix."""
    if "_96.mp4" in decrypted_url:
        base = decrypted_url.replace("_96.mp4", "{suffix}")
        return {q: base.format(suffix=suf) for q, suf in QUALITY_SUFFIX.items()}
    # Unexpected shape — just hand back what we decrypted as the only option.
    return {"320kbps": decrypted_url}


def enrich_media_urls(node):
    """Walk any JSON structure returned by JioSaavn and, wherever an
    `encrypted_media_url` field is found, decrypt it in place and attach:
      - media_urls: { "12kbps": url, "48kbps": url, ... "320kbps": url }
      - media_url:  the 160kbps URL (good default quality), as a convenience
    This runs on every proxied response, so song objects coming back from
    /api/home, /api/search, /api/song_details, /api/album_details,
    /api/playlist_details, /api/from_token etc. are ALL playable without
    the frontend needing to know which endpoint it came from."""
    if isinstance(node, dict):
        enc = node.get("encrypted_media_url")
        if enc and "media_urls" not in node:
            decrypted = decrypt_media_url(enc)
            if decrypted:
                qualities = build_quality_urls(decrypted)
                node["media_urls"] = qualities
                node["media_url"] = qualities.get("160kbps") or next(iter(qualities.values()), None)
        for v in node.values():
            enrich_media_urls(v)
    elif isinstance(node, list):
        for item in node:
            enrich_media_urls(item)


def find_song_object(data, song_id: str):
    """song.getDetails responses show up in a few different shapes
    depending on JioSaavn's mood (keyed-by-id dict, {"songs":[...]},
    {"data":[...]}, or just the bare song object). Try them in order."""
    if not isinstance(data, dict):
        return None
    if song_id in data and isinstance(data[song_id], dict):
        return data[song_id]
    for key in ("songs", "data"):
        v = data.get(key)
        if isinstance(v, list) and v:
            for item in v:
                if isinstance(item, dict) and str(item.get("id")) == str(song_id):
                    return item
            if isinstance(v[0], dict):
                return v[0]
    if "more_info" in data or "encrypted_media_url" in data:
        return data
    if len(data) == 1:
        only = next(iter(data.values()))
        if isinstance(only, dict):
            return only
    return None


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def fetch_from_saavn(params: str, use_v4: bool = True, request: Request = None):
    """Make the actual server-side request to JioSaavn (this is the part
    that's blocked from a browser/Flutter http client but works fine
    from a plain Python/requests process)."""
    # api_version=4 is required by most endpoints except autocomplete
    base = f"{API_STR}&api_version=4" if use_v4 else API_STR
    url = f"https://{BASE_URL}{base}&{params}"

    headers = {'Accept': '*/*', 'User-Agent': USER_AGENT, 'Referer': 'https://www.jiosaavn.com/'}
    if request is not None and 'preferred-language' in request.headers:
        headers['cookie'] = f"L={request.headers['preferred-language']}"
    else:
        headers['cookie'] = "L=hindi%2Cenglish"

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        return JSONResponse(content={"status": "failure", "error": str(e)}, status_code=502)

    try:
        enrich_media_urls(data)
    except Exception:
        # Never let a decryption hiccup break an otherwise-good response.
        pass

    return JSONResponse(content=data, status_code=200)


@app.get("/")
async def root():
    return {"status": "ok", "service": "Saavn Proxy API", "docs": "/docs"}


@app.get("/api/home")
async def home_data(request: Request):
    return await fetch_from_saavn(ENDPOINTS['homeData'], request=request)


@app.get("/api/search")
async def search_combined(query: str, count: int = 20, page: int = 1, request: Request = None):
    """Combined top-results search (songs+albums+artists+playlists in one
    payload) — this is what search.getResults returns, used for the main
    search bar in music.html."""
    params = f"p={page}&q={quote(query)}&n={count}&{ENDPOINTS['getResults']}"
    return await fetch_from_saavn(params, request=request)


@app.get("/api/autocomplete")
async def search_autocomplete(query: str, request: Request):
    """Lightweight top-search/autocomplete, mirrors content.getTopSearches style search."""
    params = f"__call=autocomplete.get&cc=in&includeMetaTags=1&query={quote(query)}"
    return await fetch_from_saavn(params, use_v4=False, request=request)


@app.get("/api/search_songs")
async def search_songs(query: str, count: int = 20, page: int = 1, request: Request = None):
    params = f"p={page}&q={quote(query)}&n={count}&{ENDPOINTS['getResults']}"
    return await fetch_from_saavn(params, request=request)


@app.get("/api/search_albums")
async def search_albums(query: str, count: int = 20, page: int = 1, request: Request = None):
    params = f"p={page}&q={quote(query)}&n={count}&{ENDPOINTS['albumResults']}"
    return await fetch_from_saavn(params, request=request)


@app.get("/api/search_artists")
async def search_artists(query: str, count: int = 20, page: int = 1, request: Request = None):
    params = f"p={page}&q={quote(query)}&n={count}&{ENDPOINTS['artistResults']}"
    return await fetch_from_saavn(params, request=request)


@app.get("/api/search_playlists")
async def search_playlists(query: str, count: int = 20, page: int = 1, request: Request = None):
    params = f"p={page}&q={quote(query)}&n={count}&{ENDPOINTS['playlistResults']}"
    return await fetch_from_saavn(params, request=request)


@app.get("/api/song_details")
async def song_details(song_id: str, request: Request):
    params = f"pids={song_id}&{ENDPOINTS['songDetails']}"
    return await fetch_from_saavn(params, request=request)


@app.get("/api/album_details")
async def album_details(album_id: str, request: Request):
    params = f"{ENDPOINTS['albumDetails']}&cc=in&albumid={album_id}"
    return await fetch_from_saavn(params, request=request)


@app.get("/api/playlist_details")
async def playlist_details(playlist_id: str, request: Request):
    params = f"{ENDPOINTS['playlistDetails']}&cc=in&listid={playlist_id}"
    return await fetch_from_saavn(params, request=request)


@app.get("/api/from_token")
async def from_token(token: str, type: str, n: int = 10, p: int = 1, request: Request = None):
    """Generic resolver used for album/playlist/artist/mix/show tokens, like
    SaavnAPI.getSongFromToken in api.dart."""
    params = f"token={token}&type={type}&n={n}&p={p}&{ENDPOINTS['fromToken']}"
    return await fetch_from_saavn(params, request=request)


@app.get("/api/artist_details")
async def artist_details(
    artist_id: str = None,
    token: str = None,
    n_song: int = 20,
    n_album: int = 20,
    page: int = 1,
    request: Request = None,
):
    """Full artist page: bio, top songs, top albums, similar artists.
    Pass either artist_id OR token (the artist's URL slug)."""
    if not artist_id and not token:
        return JSONResponse({"status": "failure", "error": "artist_id or token required"}, status_code=400)
    ident = f"artistId={quote(artist_id)}" if artist_id else f"token={quote(token)}&type=artist"
    params = (
        f"{ident}&n_song={n_song}&n_album={n_album}&page={page}"
        f"&category=alphabetical&sort_order=asc&{ENDPOINTS['artistDetails']}"
    )
    return await fetch_from_saavn(params, request=request)


@app.get("/api/artist_songs")
async def artist_songs(artist_id: str, page: int = 1, count: int = 20, request: Request = None):
    params = f"artistId={quote(artist_id)}&page={page}&n_song={count}&category=alphabetical&sort_order=asc&{ENDPOINTS['artistMoreSongs']}"
    return await fetch_from_saavn(params, request=request)


@app.get("/api/artist_albums")
async def artist_albums(artist_id: str, page: int = 1, count: int = 20, request: Request = None):
    params = f"artistId={quote(artist_id)}&page={page}&n_album={count}&category=alphabetical&sort_order=asc&{ENDPOINTS['artistMoreAlbums']}"
    return await fetch_from_saavn(params, request=request)


@app.get("/api/reco")
async def song_reco(song_id: str, request: Request = None):
    """Songs related to a given song — used to auto-queue more tracks
    once the current song/queue ends (infinite play).

    Unlike most JioSaavn endpoints (which return a keyed dict),
    reco.getreco returns a BARE JSON ARRAY of song objects, e.g.:
      [ { "id": "...", "title": "...", "more_info": {...} }, ... ]
    So this gets its own handler instead of going through the generic
    fetch_from_saavn (which just passes the raw shape straight through),
    to guarantee the frontend always gets a consistent {"songs": [...]}
    shape to work with.
    """
    url = f"https://{BASE_URL}{API_STR}&api_version=4&__call=reco.getreco&pid={quote(song_id)}"
    headers = {'Accept': '*/*', 'User-Agent': USER_AGENT, 'Referer': 'https://www.jiosaavn.com/'}
    if request is not None and 'preferred-language' in request.headers:
        headers['cookie'] = f"L={request.headers['preferred-language']}"
    else:
        headers['cookie'] = "L=hindi%2Cenglish"

    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return JSONResponse({"status": "failure", "error": str(e)}, status_code=502)

    # Normalize: bare array (the common case) OR an already-keyed dict
    # ({"songs": [...]} / {"data": [...]}), just in case JioSaavn changes
    # its mind on response shape for a given pid/region.
    if isinstance(data, list):
        songs = data
    elif isinstance(data, dict):
        songs = data.get("songs") or data.get("data") or []
    else:
        songs = []

    try:
        enrich_media_urls(songs)
    except Exception:
        # Never let a decryption hiccup break an otherwise-good response.
        pass

    return JSONResponse({"status": "success", "songs": songs})


@app.get("/api/album_reco")
async def album_reco(album_id: str, count: int = 20, language: str = "hindi", request: Request = None):
    """Albums related to a given album."""
    params = f"pid={quote(album_id)}&language={quote(language)}&k={count}&{ENDPOINTS['getAlbumReco']}"
    
    return await fetch_from_saavn(params, request=request)


@app.get("/api/artist_other_top_songs")
async def artist_other_top_songs(
    artist_id: str,
    song_id: str = "",
    language: str = "hindi,english",
    count: int = 20,
    request: Request = None,
):
    """Other top songs by the same artist(s) as the current song — another
    good source of "up next" suggestions for infinite play."""
    params = (
        f"artist_id={quote(artist_id)}&song_id={quote(song_id)}"
        f"&language={quote(language)}&count={count}&{ENDPOINTS['artistOtherTopSongs']}"
    )
    headers = {
        'Accept': '*/*',
        'User-Agent': "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36",
        'Referer': 'https://www.jiosaavn.com/',
        "Cookie": "_ga_BXVL6HHR7F=GS2.1.s1754145673$o1$g0$t1754145673$j60$l0$h0; _ga=GA1.1.1586258144.1754145673; B=c69be7ae799c7dfe35494f836b9773aa; L=hindi; mm_latlong=22.3008%2C73.2043; CT=NzU4OTY0MDcz; _pl=web6dot0-; DL=english; geo=2409%3A40c1%3A4159%3Af53f%3Ac890%3A4ae0%3A74f6%3A8ce3%2CIN%2CGujarat%2CVadodara%2C390001; CH=G03%2CA07%2CO00%2CL03; FCCDCF=%5Bnull%2Cnull%2Cnull%2Cnull%2Cnull%2Cnull%2C%5B%5B32%2C%22%5B%5C%224da6488f-d42f-426a-b87a-c50e5faf33d7%5C%22%2C%5B1776507851%2C315000000%5D%5D%22%5D%5D%5D; FCNEC=%5B%5B%22AKsRol-Hmsc_FCXl23o582kKTSQilzKmm18VPCvQdfA-LW-23AvHCAoykC3P5yC9deMUKWnX-nF_Nixq6uIqgOPS7mkXI-qg9k9l5_3_M2Ie2CNWOPuRs7QKSxL3Z38I_k1cBZQrnifQtBC9ItYx30i-rDIhQtVu9g%3D%3D%22%5D%5D; network=phone; SG=u; __gads=ID=5b22d2fec7dac0da:T=1776507852:RT=1781969877:S=ALNI_MY3NWpSsgxbbA2vnZAz7DsD7SBNfw; __gpi=UID=00001269353d8f69:T=1776507852:RT=1781969877:S=ALNI_MZcYgX4X4HSX_b_iuBL2gA8LjEP0A; __eoi=ID=df1961e6f11b0da9:T=1776507852:RT=1781969877:S=AA-AfjaR5mPytphVRsz-O2pc8aiv; _ga_0S33EMSFSM=GS2.1.s1781968890$o19$g1$t1781970147$j60$l0$h0; I=qp1cy7Jy5j%2FpUKV4EbYJ23N2y7hNqTaRadiYDkJc0bUiGxtz4HzvNkh7s0XL2KeObf%2BPx43m2zzv4dhoI5IMCZvPzSt%2BOWEBgQ948Ew1r4FH5JXmF87OwPCzH%2B%2BSMTYxssTedISjtfrull8xh%2FhOfifiswT%2B248v7gwGg9QvqVpi1FjvZ4dSffkG%2FMNn1RvjlrttHwb3%2BklnFJP%2BZPWFdRwP89t%2Fenl6GkTr4xjHZ%2FN6%2F5tx0RN%2FwQUAX7Rcfwd470%2F9iOB7%2F5z2tIJnlgu%2FcCCX5eUpx6s9Mz1RFQoLIjGIbJSJ1jRsBvNEK5wfDBFu"
    }
    return await fetch_from_saavn(params, request=request)


@app.get("/api/radio/artist")
async def create_artist_radio(name: str, language: str = "hindi", request: Request = None):
    """name = artist name(s), comma separated for multiple seed artists."""
    params = f"name={quote(name)}&language={quote(language)}&{ENDPOINTS['artistRadio']}"
    return await fetch_from_saavn(params, request=request)


@app.get("/api/radio/featured")
async def create_featured_radio(name: str, language: str = "hindi", request: Request = None):
    params = f"name={quote(name)}&language={quote(language)}&{ENDPOINTS['featuredRadio']}"
    return await fetch_from_saavn(params, request=request)


@app.get("/api/radio/songs")
async def get_radio_songs(station_id: str, count: int = 10, request: Request = None):
    params = f"stationid={quote(station_id)}&k={count}&{ENDPOINTS['radioSongs']}"
    return await fetch_from_saavn(params, request=request)


@app.get("/api/song_url")
async def song_url(song_id: str, quality: str = "160kbps", request: Request = None):
    """Single-purpose endpoint: resolve ONE song id straight to a playable,
    decrypted CDN url. The frontend calls this as a fallback whenever a
    song object it already has doesn't carry a `media_url` (e.g. a song
    found via search before its full details were fetched)."""
    params = f"pids={quote(song_id)}&{ENDPOINTS['songDetails']}"
    url = f"https://{BASE_URL}{API_STR}&api_version=4&{params}"
    headers = {'Accept': '*/*', 'User-Agent': USER_AGENT, 'Referer': 'https://www.jiosaavn.com/'}
    if request is not None and 'preferred-language' in request.headers:
        headers['cookie'] = f"L={request.headers['preferred-language']}"
    else:
        headers['cookie'] = "L=hindi%2Cenglish"

    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return JSONResponse({"status": "failure", "error": str(e)}, status_code=502)

    song = find_song_object(data, song_id)
    if not song:
        return JSONResponse(
            {"status": "failure", "error": "song not found in JioSaavn response",
             "raw_top_level_keys": list(data.keys()) if isinstance(data, dict) else None},
            status_code=404,
        )

    enrich_media_urls(song)
    more_info = song.get("more_info") if isinstance(song.get("more_info"), dict) else song
    media_urls = more_info.get("media_urls") or song.get("media_urls") or {}
    chosen = (
        media_urls.get(quality)
        or media_urls.get("160kbps")
        or media_urls.get("96kbps")
        or media_urls.get("320kbps")
        or (next(iter(media_urls.values())) if media_urls else None)
    )

    if not chosen:
        return JSONResponse(
            {"status": "failure",
             "error": "Could not derive a playable URL — encrypted_media_url missing or undecryptable for this song.",
             "song_keys": list(song.keys()), "more_info_keys": list(more_info.keys()) if isinstance(more_info, dict) else None},
            status_code=502,
        )

    return JSONResponse({
        "status": "success",
        "id": song.get("id", song_id),
        "title": song.get("title") or song.get("song"),
        "duration": more_info.get("duration") or song.get("duration"),
        "image": song.get("image"),
        "media_urls": media_urls,
        "media_url": chosen,
        "quality": quality,
    })


@app.get("/api/lyrics")
async def get_lyrics(song_id: str, request: Request = None):
    """Retrieve lyrics for a given song ID from JioSaavn."""
    params = f"__call=lyrics.getLyrics&lyrics_id={quote(song_id)}"
    return await fetch_from_saavn(params, use_v4=True, request=request)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
