# Reel Recipes 📸

Scrolling Instagram and see a reel like *"here's how I get this warm film look"*? Paste the link into Reel Recipes and it writes down every camera setting and editor slider for you, then saves the look in your library.

**How it works:** downloads the reel → Google's Gemini watches the whole video with sound (reading slider numbers shown on screen and listening to the voiceover) along with the caption → you get a tidy card like:

| Lightroom › Light | |
|---|---|
| Exposure | +0.35 |
| Contrast | −20 |

Every value is tagged by where it came from (*on screen*, *said*, *caption*, or *estimated*), and anything it couldn't read (like a paid preset) goes under **Couldn't get**. You can also rename looks, add your own notes, search, and copy the settings as text.

## Setup (one time)

You need **Python 3.10+**, **ffmpeg**, and a **free Gemini API key**: go to https://aistudio.google.com, sign in with a Google account, click **Get API key → Create API key**. No credit card needed.

```bash
# macOS: brew install ffmpeg      Windows: winget install ffmpeg
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run it

```bash
export GEMINI_API_KEY=your-key-here        # Windows: set GEMINI_API_KEY=your-key-here
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
| `GEMINI_API_KEY` | — | Required (free). |
| `COOKIES_FROM_BROWSER` / `INSTAGRAM_COOKIES_FILE` | — | Lets it download reels as you. |
| `GEMINI_MODEL` | `gemini-flash-latest` | Always Google's newest free Flash model. |
| `GEMINI_FPS` | `2` | Frames per second Gemini looks at. Higher catches quick slider changes but uses more of the free quota. |
| `PORT` | `5000` | |

### Is it really free?

Yes, on Gemini's free tier. Google caps it at a number of requests per minute and per day. That's way more than you'd use checking reels, but if you hit it the app tells you to wait. One catch: on the free tier Google may use what you send to improve their models. These are public reels, so that's usually fine.

Your library lives in `data/looks.json`.

### Using Claude instead (paid)

Set `AI_PROVIDER=claude` and `ANTHROPIC_API_KEY`, and `pip install anthropic faster-whisper`. It costs a few cents up to about 20¢ per reel. Extra options: `CLAUDE_MODEL` (default `claude-opus-5-5`), `MAX_FRAMES` (default `40`), `WHISPER_MODEL` (default `base`).

> Heads-up: the app has no password and listens on your local network, so only run it on Wi-Fi you trust.

## iPhone version (no computer, free)

`phone/reel-recipes.html` is a single page published as a claude.ai Artifact. It runs on your free Claude account: no API key, no server.

1. In Instagram, save the reel (**⋯ → Download**, or screen-record it).
2. Open the Reel Recipes link and choose the video (or a few screenshots).
3. The page pulls frames from the video and Claude reads the settings. Looks are saved to your Claude account.

It can't hear the voiceover, so type anything they say out loud into the optional "what they said" box.
