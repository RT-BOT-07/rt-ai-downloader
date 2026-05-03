"""
BACKEND
"""

from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import yt_dlp
import os
import sys
import uuid
import zipfile
import tempfile
import shutil
import threading
import re

app = Flask(__name__)
CORS(app)

HOME     = os.path.expanduser("~")
TEMP_DIR = os.path.join(HOME, ".rtdown_tmp")
os.makedirs(TEMP_DIR, exist_ok=True)

BASE_OPTS = {
    "quiet"              : True,
    "no_warnings"        : True,
    "nocheckcertificate" : True,
    "geo_bypass"         : True,
    "age_limit"          : 99,
    "ffmpeg_location"    : shutil.which("ffmpeg") or "/data/data/com.termux/files/usr/bin/ffmpeg",
    "http_headers"       : {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Mobile Safari/537.36"
        )
    },
}

VIDEO_HEIGHTS = [144, 240, 360, 480, 720, 1080, 1440, 2160]
QUALITY_LABEL = {
    144:  "144p",  240:  "240p",  360:  "360p",
    480:  "480p",  720:  "720p",  1080: "1080p",
    1440: "1440p", 2160: "2160p (4K)",
}

def clean_url(url):
    """Clean YouTube / YouTube Music URLs."""
    url = url.strip()
    # music.youtube.com → www.youtube.com
    url = url.replace("music.youtube.com", "www.youtube.com")
    # Remove extra params
    url = re.sub(r'&playnext=[^&]*', '', url)
    url = re.sub(r'&si=[^&]*', '', url)
    url = re.sub(r'&index=[^&]*', '', url)
    url = re.sub(r'[?&]$', '', url)
    return url.strip()

def get_available_qualities(formats):
    seen = set()
    result = []
    for f in formats:
        h = f.get("height")
        if h and h in VIDEO_HEIGHTS and h not in seen:
            seen.add(h)
            result.append({
                "quality"  : QUALITY_LABEL.get(h, f"{h}p"),
                "height"   : h,
                "ext"      : "mp4",
                "filesize" : f.get("filesize") or f.get("filesize_approx"),
                "has_audio": (f.get("acodec","none") not in ("none","") and
                              f.get("vcodec","none") not in ("none","")),
            })
    result.sort(key=lambda x: x["height"])
    return result

def get_best_audio_info(formats):
    audio = [f for f in formats if
             f.get("vcodec","none") in ("none","") and
             f.get("acodec","none") not in ("none","") and
             f.get("url")]
    if not audio:
        audio = [f for f in formats if f.get("ext") in ("m4a","mp3","aac") and f.get("url")]
    if not audio:
        return None
    audio.sort(key=lambda x: x.get("abr") or x.get("tbr") or 0, reverse=True)
    best = audio[0]
    return {
        "ext"      : best.get("ext","m4a"),
        "abr"      : best.get("abr") or best.get("tbr"),
        "filesize" : best.get("filesize") or best.get("filesize_approx"),
    }

# ─────────────────────────────────────────────────────
# /extract
# ─────────────────────────────────────────────────────
@app.route("/extract", methods=["POST"])
def extract():
    body = request.json or {}
    url  = clean_url(body.get("url") or "")
    if not url:
        return jsonify({"status": "error", "message": "URL is required"})

    is_playlist_url = "list=" in url
    opts = {**BASE_OPTS, "extract_flat": "in_playlist", "noplaylist": False if is_playlist_url else True}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

    # ── Playlist ──
    if info.get("_type") == "playlist":
        entries     = info.get("entries") or []
        track_count = len(entries)
        thumb = None
        for e in entries:
            if e and e.get("thumbnail"):
                thumb = e["thumbnail"]; break

        return jsonify({
            "status"      : "success",
            "is_playlist" : True,
            "title"       : info.get("title", "Playlist"),
            "track_count" : track_count,
            "thumbnail"   : thumb,
            "duration"    : f"{track_count} tracks",
            "formats"     : [],
            "qualities"   : [],
        })

    # ── Single video ──
    raw_formats = info.get("formats") or []
    qualities   = get_available_qualities(raw_formats)
    best_audio  = get_best_audio_info(raw_formats)

    dur = info.get("duration")
    dur_str = (f"{int(dur)//60}:{int(dur)%60:02d}" if dur else "")

    return jsonify({
        "status"      : "success",
        "is_playlist" : False,
        "title"       : info.get("title", ""),
        "thumbnail"   : info.get("thumbnail", ""),
        "duration"    : dur_str,
        "uploader"    : info.get("uploader", ""),
        "qualities"   : qualities,
        "best_audio"  : best_audio,
        "formats"     : [],
    })

# ─────────────────────────────────────────────────────
# /download
# ─────────────────────────────────────────────────────
@app.route("/download")
def download():
    url    = clean_url(request.args.get("url", ""))
    height = request.args.get("height", "")
    fmt    = request.args.get("format", "")

    if not url:
        return jsonify({"status": "error", "message": "URL required"}), 400

    job_dir = os.path.join(TEMP_DIR, uuid.uuid4().hex)
    os.makedirs(job_dir, exist_ok=True)

    try:
        if fmt == "mp3":
            opts = {
                **BASE_OPTS,
                "format"         : "bestaudio/best",
                "outtmpl"        : os.path.join(job_dir, "%(title)s.%(ext)s"),
                "postprocessors" : [{
                    "key"             : "FFmpegExtractAudio",
                    "preferredcodec"  : "mp3",
                    "preferredquality": "320",
                }],
            }
        elif height:
            h = int(height)
            fmt_str = (
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={h}]+bestaudio"
                f"/best[height<={h}]"
                f"/best"
            )
            opts = {
                **BASE_OPTS,
                "format"              : fmt_str,
                "outtmpl"             : os.path.join(job_dir, "%(title)s.%(ext)s"),
                "merge_output_format" : "mp4",
            }
        else:
            return jsonify({"status": "error", "message": "Specify height or format=mp3"}), 400

        with yt_dlp.YoutubeDL(opts) as ydl:
            info     = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if not os.path.exists(filename):
                files = os.listdir(job_dir)
                if not files:
                    return jsonify({"status": "error", "message": "Download failed"}), 500
                filename = os.path.join(job_dir, files[0])

        title   = info.get("title", "download")
        ext     = os.path.splitext(filename)[1].lstrip(".")
        safe    = "".join(c for c in title if c.isalnum() or c in " -_").strip()[:60]
        dl_name = f"{safe}.{ext}"

        resp = send_file(filename, as_attachment=True, download_name=dl_name)

        @resp.call_on_close
        def cleanup():
            shutil.rmtree(job_dir, ignore_errors=True)

        return resp

    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"status": "error", "message": str(e)}), 500

# ─────────────────────────────────────────────────────
# /download_playlist_zip
# ─────────────────────────────────────────────────────
@app.route("/download_playlist_zip")
def download_playlist_zip():
    url = clean_url(request.args.get("url", ""))
    if not url:
        return jsonify({"status": "error", "message": "URL required"}), 400

    job_dir = os.path.join(TEMP_DIR, uuid.uuid4().hex)
    os.makedirs(job_dir, exist_ok=True)

    try:
        opts = {
            **BASE_OPTS,
            "format"         : "bestaudio/best",
            "outtmpl"        : os.path.join(job_dir, "%(playlist_index)02d - %(title)s.%(ext)s"),
            "postprocessors" : [{
                "key"             : "FFmpegExtractAudio",
                "preferredcodec"  : "mp3",
                "preferredquality": "320",
            }],
            "ignoreerrors"   : True,
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        mp3_files = [f for f in os.listdir(job_dir) if f.endswith(".mp3")]
        if not mp3_files:
            return jsonify({"status": "error", "message": "No tracks downloaded"}), 500

        playlist_title = (info or {}).get("title", "Playlist")
        safe_title     = "".join(c for c in playlist_title if c.isalnum() or c in " -_").strip()[:40]
        zip_path       = os.path.join(TEMP_DIR, f"{safe_title}.zip")

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(mp3_files):
                zf.write(os.path.join(job_dir, f), f)

        resp = send_file(zip_path, as_attachment=True, download_name=f"{safe_title}.zip")

        @resp.call_on_close
        def cleanup():
            shutil.rmtree(job_dir, ignore_errors=True)
            try: os.remove(zip_path)
            except: pass

        return resp

    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"status": "error", "message": str(e)}), 500

# ─────────────────────────────────────────────────────
# /ping
# ─────────────────────────────────────────────────────
@app.route("/ping")
def ping():
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    ydlp_ver  = yt_dlp.version.__version__
    return jsonify({
        "status" : "ok",
        "yt_dlp" : ydlp_ver,
        "ffmpeg" : ffmpeg_ok,
        "python" : sys.version.split()[0],
    })

if __name__ == "__main__":
    print("\n  ╔══════════════════════════════╗")
    print("  ║   RT DOWNLOADER BACKEND      ║")
    print("  ║   http://127.0.0.1:5000      ║")
    print("  ╚══════════════════════════════╝\n")
    import os
port = int(os.environ.get("PORT", 5000))
app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
