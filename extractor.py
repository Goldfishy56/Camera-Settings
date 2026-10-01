"""Turns an Instagram reel (or any video) into a list of photo settings.

Pipeline: download the video with yt-dlp, then either
  - Gemini (default, free tier): upload the whole video, sound included, or
  - Claude: grab frames with ffmpeg, (optionally) transcribe the voiceover
    with faster-whisper, and send caption + transcript + frames.
Both return the same structured Look.
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Literal

import time

from pydantic import BaseModel, Field

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest")
GEMINI_FPS = float(os.environ.get("GEMINI_FPS", "2"))
MAX_FRAMES = int(os.environ.get("MAX_FRAMES", "40"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")

Progress = Callable[[str], None]


# ---------------------------------------------------------------------------
# What we ask Claude to fill in
# ---------------------------------------------------------------------------

Source = Literal["voiceover", "caption", "on_screen", "inferred"]


class Setting(BaseModel):
    group: str = Field(
        description="Where this setting lives, e.g. 'Camera', 'Lightroom › Light', "
        "'Lightroom › Color › HSL', 'VSCO', 'iPhone Camera app'."
    )
    name: str = Field(description="Setting name, e.g. 'ISO', 'Exposure', 'Orange Saturation'.")
    value: str = Field(description="The value exactly as given, e.g. '100', '+0.35', '-20', '1/250', 'f/1.8'.")
    note: str = Field(default="", description="Short extra context, or empty.")
    source: Source = Field(description="Where you got it from.")


class Look(BaseModel):
    title: str = Field(description="Short catchy name for the look, e.g. 'Warm film golden hour'.")
    summary: str = Field(description="1-2 sentences on what the look is and when to use it.")
    kind: Literal["camera", "editing", "camera_and_editing", "not_a_tutorial"]
    apps: list[str] = Field(description="Apps used (Lightroom, VSCO, Snapseed, CapCut...). Empty if none.")
    gear: list[str] = Field(description="Camera, lens, filters, phone etc. mentioned. Empty if none.")
    settings: list[Setting] = Field(description="Every setting with a concrete value, in the order to apply them.")
    steps: list[str] = Field(description="Any how-to steps that are not a simple setting=value (e.g. 'shoot facing the sun').")
    missing: list[str] = Field(
        description="Things the reel mentioned but did not show clearly enough to read, "
        "or that are only in a paid preset/link. Empty if none."
    )


SYSTEM_PROMPT = """You extract photography recipes from short-form tutorial videos (Instagram reels, TikToks).

You get the post caption plus the video itself (or, if you can't receive video, an automatic transcript of the voiceover — which may contain mistakes — and frames sampled in order with timestamps). Tutorials usually show values on screen: slider positions in Lightroom/VSCO/Snapseed/iPhone Photos, camera dials or settings menus, or text overlays.

Your job is to write down every concrete setting so the viewer can reproduce the look without rewatching.

- Read slider values carefully from the video. If a value changes over time, record the final value the creator lands on.
- Group settings by where they live (camera vs. each app and panel) and list them in the order they should be applied.
- Keep values exactly as shown (signs, units, decimals). For HSL / color mixer / color grading, give one row per color and property, e.g. group 'Lightroom › Color Mixer', name 'Orange Saturation', value '-15'.
- Mark source as 'on_screen' when read from the picture, 'voiceover' or 'caption' when stated there, and 'inferred' only when you are estimating (for example reading an unlabeled slider position) — say so in the note.
- Never invent values that are not supported by the video. If something is unreadable or hidden behind a paid preset, put it in 'missing'.
- If the video is not a photo/editing tutorial, set kind to 'not_a_tutorial' and leave settings empty."""


# ---------------------------------------------------------------------------
# Download / media helpers
# ---------------------------------------------------------------------------


def download(url: str, workdir: Path) -> dict:
    """Download a reel with yt-dlp. Returns its info dict with 'filepath' added."""
    import yt_dlp

    opts = {
        "outtmpl": str(workdir / "video.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    # Instagram often requires a logged-in session. Either export cookies to a
    # file, or let yt-dlp read them straight from a browser you're logged into.
    if os.environ.get("INSTAGRAM_COOKIES_FILE"):
        opts["cookiefile"] = os.environ["INSTAGRAM_COOKIES_FILE"]
    if os.environ.get("COOKIES_FROM_BROWSER"):
        opts["cookiesfrombrowser"] = (os.environ["COOKIES_FROM_BROWSER"],)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if info.get("entries"):  # carousel posts come back as a playlist
        info = next(e for e in info["entries"] if e)
    videos = sorted(workdir.glob("video.*"))
    if not videos:
        raise RuntimeError("yt-dlp finished but no video file was saved.")
    info["filepath"] = str(videos[0])
    return info


def probe_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def extract_frames(video: Path, workdir: Path, duration: float, max_frames: int = MAX_FRAMES) -> list[tuple[float, Path]]:
    """Sample up to max_frames evenly spaced frames (at most 2 per second)."""
    fps = min(2.0, max_frames / max(duration, 1.0))
    frames_dir = workdir / "frames"
    frames_dir.mkdir(exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(video),
            # 1280px on the long side keeps small slider numbers readable.
            "-vf", f"fps={fps},scale='if(gt(iw,ih),1280,-2)':'if(gt(iw,ih),-2,1280)'",
            "-q:v", "3", str(frames_dir / "f%04d.jpg"),
        ],
        check=True,
    )
    frames = sorted(frames_dir.glob("f*.jpg"))[:max_frames]
    return [(i / fps, f) for i, f in enumerate(frames)]


def transcribe(video: Path) -> str | None:
    """Transcribe the voiceover if faster-whisper is installed; otherwise skip."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(video), vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip() or None


def has_audio(video: Path) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True,
    )
    return bool(out.stdout.strip())


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


def which_provider() -> str:
    explicit = os.environ.get("AI_PROVIDER", "").lower()
    if explicit in ("gemini", "claude"):
        return explicit
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    raise RuntimeError("No AI key found — set GEMINI_API_KEY (free at aistudio.google.com) and restart the app.")


def ask_gemini(caption: str, video: Path) -> Look:
    from google import genai
    from google.genai import errors as genai_errors, types

    client = genai.Client()  # reads GEMINI_API_KEY / GOOGLE_API_KEY
    uploaded = client.files.upload(file=video)
    try:
        deadline = time.time() + 300
        while uploaded.state == types.FileState.PROCESSING:
            if time.time() > deadline:
                raise RuntimeError("Gemini took too long to process the video — try again.")
            time.sleep(2)
            uploaded = client.files.get(name=uploaded.name)
        if uploaded.state == types.FileState.FAILED:
            raise RuntimeError("Gemini couldn't process this video.")

        request = dict(
            contents=[
                types.Part(
                    file_data=types.FileData(file_uri=uploaded.uri, mime_type=uploaded.mime_type),
                    # More frames per second than the default 1 so quick slider changes aren't missed.
                    video_metadata=types.VideoMetadata(fps=GEMINI_FPS),
                ),
                types.Part(text=f"<caption>\n{caption or '(no caption)'}\n</caption>\n\n"
                                "Extract every setting from this tutorial. Listen to the voiceover too."),
            ],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_json_schema=Look.model_json_schema(),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        # The free tier is often "overloaded" for a moment; wait and retry, and
        # fall back to the lighter Flash model if the main one stays busy.
        models = [GEMINI_MODEL] * 3 + [GEMINI_FALLBACK_MODEL] * 2
        for attempt, model in enumerate(models):
            try:
                response = client.models.generate_content(model=model, **request)
                break
            except genai_errors.ServerError:
                if attempt == len(models) - 1:
                    raise RuntimeError("Gemini's free servers are busy right now — try again in a few minutes.")
                time.sleep(5 * (attempt + 1))
        if not response.text:
            raise RuntimeError("Gemini returned an empty answer (it may have blocked the video) — try again.")
        return Look.model_validate_json(response.text)
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass  # uploads expire on their own after 48h


def ask_claude(caption: str, transcript: str | None, frames: list[tuple[float, Path]]) -> Look:
    content: list[dict] = [
        {
            "type": "text",
            "text": f"<caption>\n{caption or '(no caption)'}\n</caption>\n\n"
            f"<transcript>\n{transcript or '(no voiceover transcript available)'}\n</transcript>\n\n"
            f"Below are {len(frames)} frames from the video, in order.",
        }
    ]
    for t, path in frames:
        content.append({"type": "text", "text": f"Frame at {t:.1f}s:"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(path.read_bytes()).decode(),
            },
        })
    content.append({"type": "text", "text": "Extract every setting from this tutorial."})

    import anthropic

    client = anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_format=Look,
        output_config={"effort": "high"},
        # If a safety classifier declines, retry on Anthropic's recommended fallback model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to analyze this video.")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError("Claude's answer was cut off or unreadable — try again.")
    return response.parsed_output


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def analyze(
    *, url: str | None = None, video_path: Path | None = None,
    thumb_out: Path | None = None, progress: Progress = lambda _: None,
) -> dict:
    """Run the whole pipeline. Pass either a reel URL or a local video file."""
    workdir = Path(tempfile.mkdtemp(prefix="reel-"))
    try:
        caption, author = "", ""
        if url:
            progress("Downloading the reel…")
            info = download(url, workdir)
            video = Path(info["filepath"])
            caption = info.get("description") or info.get("title") or ""
            author = info.get("uploader") or info.get("channel") or ""
        else:
            video = video_path

        duration = probe_duration(video)
        provider = which_provider()
        transcript = None

        if provider == "gemini":
            progress("Gemini is watching the reel…")
            look = ask_gemini(caption, video)
            thumb_frames = extract_frames(video, workdir, duration, max_frames=3)
        else:
            progress("Grabbing frames…")
            thumb_frames = frames = extract_frames(video, workdir, duration)
            if not frames:
                raise RuntimeError("Couldn't read any frames from the video.")
            if has_audio(video):
                progress("Listening to the voiceover…")
                transcript = transcribe(video)
            progress(f"Reading settings from {len(frames)} frames…")
            look = ask_claude(caption, transcript, frames)

        if thumb_out and thumb_frames:
            shutil.copy(thumb_frames[len(thumb_frames) // 3][1], thumb_out)

        return {
            **look.model_dump(),
            "author": author,
            "caption": caption,
            "transcript": transcript or "",
            "duration": round(duration, 1),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
