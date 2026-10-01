"""Reel Recipes — paste an Instagram reel, get its photo settings saved.

Run:  python app.py   then open http://localhost:5000 (or your computer's
LAN address from your phone).
"""

import json
import os
import threading
import time
import traceback
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

import extractor

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
THUMBS_DIR = DATA_DIR / "thumbs"
UPLOADS_DIR = DATA_DIR / "uploads"
LOOKS_FILE = DATA_DIR / "looks.json"
for d in (THUMBS_DIR, UPLOADS_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024  # 300 MB uploads

jobs: dict[str, dict] = {}
looks_lock = threading.Lock()


# --- saved looks (a small JSON file is plenty for a personal library) -------

def load_looks() -> list[dict]:
    if not LOOKS_FILE.exists():
        return []
    return json.loads(LOOKS_FILE.read_text())


def save_looks(looks: list[dict]) -> None:
    tmp = LOOKS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(looks, indent=2))
    tmp.replace(LOOKS_FILE)


# --- background jobs ---------------------------------------------------------

def run_job(job_id: str, url: str | None, video_path: Path | None) -> None:
    job = jobs[job_id]
    look_id = uuid.uuid4().hex[:12]
    thumb = THUMBS_DIR / f"{look_id}.jpg"

    def progress(msg: str) -> None:
        job["message"] = msg

    try:
        result = extractor.analyze(url=url, video_path=video_path, thumb_out=thumb, progress=progress)
        look = {
            "id": look_id,
            "url": url or "",
            "created": time.time(),
            "thumb": f"/thumbs/{thumb.name}" if thumb.exists() else "",
            **result,
        }
        with looks_lock:
            looks = load_looks()
            looks.insert(0, look)
            save_looks(looks)
        job.update(status="done", look=look, message="Done")
    except Exception as e:  # report any failure back to the page
        traceback.print_exc()
        job.update(status="error", message=friendly_error(e))
    finally:
        if video_path:
            video_path.unlink(missing_ok=True)


def friendly_error(e: Exception) -> str:
    text = str(e)
    if "login" in text.lower() or "rate-limit" in text.lower() or "cookies" in text.lower():
        return ("Instagram wouldn't hand over the video without a login. Set COOKIES_FROM_BROWSER "
                "(see README), or save the reel and upload the video file instead.")
    if "ANTHROPIC_API_KEY" in text or "authentication" in text.lower():
        return "No valid Anthropic API key — set ANTHROPIC_API_KEY and restart the app."
    return text or e.__class__.__name__


# --- routes ------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/thumbs/<path:name>")
def thumbs(name: str):
    return send_from_directory(THUMBS_DIR, name)


@app.post("/api/analyze")
def analyze():
    url = (request.form.get("url") or (request.get_json(silent=True) or {}).get("url") or "").strip()
    upload = request.files.get("video")
    if not url and not upload:
        return jsonify(error="Paste a reel link or choose a video file."), 400

    video_path = None
    if upload:
        suffix = Path(upload.filename or "video.mp4").suffix or ".mp4"
        video_path = UPLOADS_DIR / f"{uuid.uuid4().hex}{suffix}"
        upload.save(video_path)
        url = ""

    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "running", "message": "Starting…"}
    threading.Thread(target=run_job, args=(job_id, url or None, video_path), daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    job = jobs.get(job_id) or abort(404)
    return jsonify(job)


@app.get("/api/looks")
def list_looks():
    return jsonify(load_looks())


@app.patch("/api/looks/<look_id>")
def update_look(look_id: str):
    changes = request.get_json(force=True)
    with looks_lock:
        looks = load_looks()
        for look in looks:
            if look["id"] == look_id:
                for key in ("title", "notes"):
                    if key in changes:
                        look[key] = str(changes[key])
                save_looks(looks)
                return jsonify(look)
    abort(404)


@app.delete("/api/looks/<look_id>")
def delete_look(look_id: str):
    with looks_lock:
        looks = [l for l in load_looks() if l["id"] != look_id]
        save_looks(looks)
    (THUMBS_DIR / f"{look_id}.jpg").unlink(missing_ok=True)
    return "", 204


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    # 0.0.0.0 so you can open it from your phone on the same Wi-Fi.
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
