# Reel Recipes 📸

Scrolling Instagram and see a reel like *"here's how I get this warm film look"*? Paste the link into Reel Recipes and it writes down every camera setting and editor slider for you, then saves the look in your library.

**How it works:** downloads the reel → pulls ~40 frames from it → transcribes the voiceover → Claude reads the caption, the voiceover and the frames (including slider numbers shown on screen) → you get a tidy card like:

| Lightroom › Light | |
|---|---|
| Exposure | +0.35 |
| Contrast | −20 |

Every value is tagged by where it came from (*on screen*, *said*, *caption*, or *estimated*), and anything it couldn't read (like a paid preset) goes under **Couldn't get**. You can also rename looks, add your own notes, search, and copy the settings as text.

## Setup (one time)

You need **Python 3.10+**, **ffmpeg**, and an **Anthropic API key** (from https://console.anthropic.com).

```bash
# macOS: brew install ffmpeg      Windows: winget install ffmpeg
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run it

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # Windows: set ANTHROPIC_API_KEY=sk-ant-...
export COOKIES_FROM_BROWSER=chrome         # see "Instagram login" below
python app.py
```

Open http://localhost:5000. To use it **from your phone**, keep your computer on the same Wi-Fi and open `http://<your-computer's-IP>:5000` (find the IP in your Wi-Fi settings). Add it to your home screen for quick access.

### Instagram login

Instagram usually only gives videos to logged-in users. The easiest fix: be logged into Instagram in a browser on the same computer and set `COOKIES_FROM_BROWSER` to `chrome`, `firefox`, `safari`, `edge` or `brave`. (Or export a cookies.txt and set `INSTAGRAM_COOKIES_FILE=/path/to/cookies.txt`.)

If a link still won't load, save or screen-record the reel and use **"Upload the video file instead"**.

### Share straight from Instagram (optional)

- **iPhone:** make a Shortcut that accepts URLs from the Share Sheet and opens `http://<your-computer's-IP>:5000/?url=` + the shared URL. Then in Instagram: Share → your shortcut.
- **Android:** any link opened as `http://<your-computer>:5000/?url=<reel link>` starts automatically.

## Settings

| Env var | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required. |
| `COOKIES_FROM_BROWSER` / `INSTAGRAM_COOKIES_FILE` | — | Lets it download reels as you. |
| `CLAUDE_MODEL` | `claude-opus-5-5` | Model that reads the reel. |
| `MAX_FRAMES` | `40` | Frames sent per reel. More = catches quick slider changes, costs more. |
| `WHISPER_MODEL` | `base` | Voiceover transcription model (`tiny`, `base`, `small`…). |
| `PORT` | `5000` | |

Each reel costs roughly a few cents up to about 20¢, depending on its length. Your library lives in `data/looks.json`.

> Heads-up: the app has no password and listens on your local network, so only run it on Wi-Fi you trust.
