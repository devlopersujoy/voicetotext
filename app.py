import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from flask import Flask, jsonify, render_template, request

from media_links import download, parse_link

LANGUAGES = json.loads(Path(__file__).with_name("languages.json").read_text())
YT_HOSTS = {"youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 49 * 1024**2
busy = threading.Lock()
results = {}
lock = threading.Lock()


def youtube_url(raw):
    parts = urlsplit(raw)
    host = parts.hostname
    path = parts.path.strip("/").split("/")
    video = ""
    if parts.scheme in {"http", "https"}:
        if host == "youtu.be" and len(path) == 1:
            video = path[0]
        elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
            if path == ["watch"]:
                video = parse_qs(parts.query).get("v", [""])[0]
            elif len(path) == 2 and path[0] in {"shorts", "embed", "live"}:
                video = path[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
        raise ValueError("Enter a YouTube video or Shorts link.")
    return "https://www.youtube.com/watch?v=" + video


def update(key, **data):
    with lock:
        results[key] = (time.monotonic(), data)


def run_job(key, folder, url, language):
    source = folder / "media"
    try:
        if url and urlsplit(url).hostname in YT_HOSTS:
            update(key, state="Fetching YouTube audio…")
            try:
                subprocess.run(["yt-dlp", "--ignore-config", "--no-playlist", "--max-filesize", "48M",
                                "--match-filter", "!is_live & duration <= 600", "-f", "bestaudio/best",
                                "-o", str(source), url], check=True, timeout=150,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except subprocess.CalledProcessError:
                pass
            if not source.is_file() or source.stat().st_size > 48 * 1024**2:
                raise ValueError("YouTube audio is unavailable or exceeds the limit. Try uploading the file.")
        elif url:
            update(key, state="Downloading media…")
            download(url, source)
        update(key, state="Reading audio…")
        wav = folder / "speech.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file,pipe",
                        "-format_whitelist", "mov,matroska,webm,wav,mp3,ogg,flac,aac,amr,avi,mpeg,mpegts",
                        "-i", str(source), "-map", "0:a:0", "-t", "601", "-ar", "16000", "-ac", "1",
                        "-c:a", "pcm_s16le", str(wav)], check=True, timeout=90)
        with wave.open(str(wav)) as audio:
            if audio.getnframes() > 600 * audio.getframerate():
                raise ValueError("Choose a clip of 10 minutes or less.")
        update(key, state="Transcribing speech…")
        subprocess.run(["whisper-cli", "-m", os.environ.get("MODEL_PATH", "/opt/model.bin"), "-l", language, "-t", "2",
                        "-otxt", "-of", str(folder / "result"), "-f", str(wav)],
                       check=True, timeout=1800, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        transcript = (folder / "result.txt").read_text().strip()
        if not transcript:
            raise ValueError("No speech was detected.")
        update(key, state="Complete", text=transcript)
    except ValueError as exc:
        update(key, error=str(exc))
    except subprocess.TimeoutExpired:
        update(key, error="Processing timed out. Try a shorter clip.")
    except Exception:
        app.logger.exception("Media processing failed")
        update(key, error="Could not process this clip. Check that it contains playable audio.")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        busy.release()


@app.get("/")
def index():
    return render_template("index.html", languages=LANGUAGES)


@app.get("/health")
def health():
    return "ok"


@app.get("/api/v1/languages")
def languages():
    return jsonify(languages=LANGUAGES, default="auto")


@app.get("/api/v1/transcriptions/<key>")
@app.get("/jobs/<key>")
def status(key):
    with lock:
        item = results.get(key)
        if item and time.monotonic() - item[0] < 3600:
            return jsonify(item[1]), 200, {"Cache-Control": "no-store"}
    return jsonify(error="Result expired or server restarted."), 404


@app.post("/api/v1/transcriptions")
@app.post("/jobs")
def submit():
    if not busy.acquire(blocking=False):
        return jsonify(error="Another clip is processing. Please try again shortly."), 429
    folder = None
    handed_off = False
    try:
        fields = request.get_json(silent=True) if request.is_json else request.form
        if not hasattr(fields, "get") or not isinstance(fields.get("url", ""), str):
            raise ValueError("Provide a JSON object with a URL or a multipart file.")
        raw = fields.get("url", "").strip()
        media = request.files.get("file")
        if bool(raw) == bool(media and media.filename):
            raise ValueError("Choose one file or one source URL.")
        url = youtube_url(raw) if raw and parse_link(raw).hostname in YT_HOSTS else raw
        language = fields.get("language", "auto")
        if not isinstance(language, str) or (language != "auto" and language not in LANGUAGES):
            raise ValueError("Invalid language selection.")
        folder = Path(tempfile.mkdtemp())
        if media and media.filename:
            media.save(folder / "media")
            if (folder / "media").stat().st_size > 48 * 1024**2:
                raise ValueError("Choose a file up to 48 MiB.")
        key = secrets.token_urlsafe(24)
        with lock:
            for old in list(results):
                if time.monotonic() - results[old][0] >= 3600 or len(results) >= 100:
                    del results[old]
        update(key, state="Starting…")
        threading.Thread(target=run_job, args=(key, folder, url, language), daemon=True).start()
        handed_off = True
        return jsonify(id=key, status_url=f"/api/v1/transcriptions/{key}"), 202
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    finally:
        if not handed_off:
            if folder:
                shutil.rmtree(folder, ignore_errors=True)
            busy.release()


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="Choose a file up to 48 MiB."), 413
