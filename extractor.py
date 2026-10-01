"""Turns an Instagram reel (or any video) into a list of photo settings.

Pipeline: download the video with yt-dlp -> grab frames with ffmpeg ->
(optionally) transcribe the voiceover with faster-whisper -> send the
caption, transcript and frames to Claude, which returns structured settings.
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Literal

import anthropic
from pydantic import BaseModel, Field

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
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

You get the post caption, an automatic transcript of the voiceover (may contain mistakes), and frames sampled in order from the video with timestamps. Tutorials usually show values on screen: slider positions in Lightroom/VSCO/Snapseed/iPhone Photos, camera dials or settings menus, or text overlays.

Your job is to write down every concrete setting so the viewer can reproduce the look without rewatching.

- Read slider values carefully from the frames. If a value changes across frames, record the final value the creator lands on.
- Group settings by where they live (camera vs. each app and panel) and list them in the order they should be applied.
- Keep values exactly as shown (signs, units, decimals). For HSL / color mixer / color grading, give one row per color and property, e.g. group 'Lightroom › Color Mixer', name 'Orange Saturation', value '-15'.
- Mark source as 'on_screen' when read from a frame, 'voiceover' or 'caption' when stated there, and 'inferred' only when you are estimating (for example reading an unlabeled slider position) — say so in the note.
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


def extract_frames(video: Path, workdir: Path, duration: float) -> list[tuple[float, Path]]:
    """Sample up to MAX_FRAMES evenly spaced frames (at most 2 per second)."""
    fps = min(2.0, MAX_FRAMES / max(duration, 1.0))
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
    frames = sorted(frames_dir.glob("f*.jpg"))[:MAX_FRAMES]
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

    client = anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=MODEL,
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
        progress("Grabbing frames…")
        frames = extract_frames(video, workdir, duration)
        if not frames:
            raise RuntimeError("Couldn't read any frames from the video.")

        transcript = None
        if has_audio(video):
            progress("Listening to the voiceover…")
            transcript = transcribe(video)

        progress(f"Reading settings from {len(frames)} frames…")
        look = ask_claude(caption, transcript, frames)

        if thumb_out:
            shutil.copy(frames[min(len(frames) - 1, len(frames) // 3)][1], thumb_out)

        return {
            **look.model_dump(),
            "author": author,
            "caption": caption,
            "transcript": transcript or "",
            "duration": round(duration, 1),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
