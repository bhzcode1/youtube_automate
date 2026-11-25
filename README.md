# youtube_automate

Turns a topic into YouTube Shorts with no human step: find live-stream VODs for the topic,
let Claude pick the streams and the best moments, cut vertical 9:16 clips with English
captions, have Claude review each clip, then upload the approved ones on a daily schedule.

```
make_clips.py  ──►  automate/queue.json  ──►  upload.py  ──►  YouTube
 (Person A)          status "approved"         (Person B)      status "uploaded" / "failed"
```

`run_daily.py` runs both steps and installs the daily Windows scheduled task.

## Files

| File | What it does |
|---|---|
| `automate/make_clips.py` | Topic → streams → clips → Claude review → `queue.json` (rejects go to `rejected.json`) |
| `automate/sc_runner.py` | Runs the `stream-clips` skill with per-run language options (used by `make_clips.py`) |
| `automate/upload.py` | Uploads approved clips, best score first, via Upload-Post or your own Google API project |
| `automate/run_daily.py` | Runs both steps; `--install HH:MM` creates the daily scheduled task |
| `automate/creators.json` | Spoken language per creator (`"hi"`, `"en"`, …). Claude fills in unknown ones; fix by hand |
| `automate/queue.example.json` | Example of one queue entry |
| `automate/tests/` | Unit tests (no network, no accounts) |

## Needs

- Python 3.11+, `ffmpeg`, and the `claude` CLI on `PATH`
- The `stream-clips` Claude skill installed in `~/.claude/skills/stream-clips` (or `STREAMCLIPS_SKILL` set to its folder)
- `pip install -r automate/requirements.txt`

## One-time setup for uploading

Pick one way to upload. `upload.py` uses Upload-Post automatically when its key file exists.

**Upload-Post** (public posts; free plan: 10 posts a month)
1. Create an account at upload-post.com and connect your YouTube channel to a profile.
2. Save the API key in `automate/uploadpost_key.txt` and the profile name in `automate/uploadpost_profile.txt`.

**Your own Google API project** (videos stay private until Google audits the project; about 6 uploads a day)
1. In Google Cloud Console: create a project, enable **YouTube Data API v3**, set up the OAuth
   consent screen (External), then click **Publish app** so it is "In production". In
   "Testing" the sign-in expires after 7 days and the daily run stops.
2. Create an OAuth client of type **Desktop app** and save it as `automate/client_secret.json`.
3. Sign in once by hand: `python automate/upload.py --auth-only`

These files hold secrets and are in `.gitignore`. Never commit them.

## Run

```bash
# one full run by hand
python automate/run_daily.py --topic "GTA 6" --keywords "gta 6,gta vi" --sources twitch --max-uploads 1

# see what would be uploaded, upload nothing
python automate/upload.py --dry-run

# post one video of your own now (AI writes the title)
python automate/upload.py --add path/to/video.mp4

# daily at 18:00 (Windows Task Scheduler)
python automate/run_daily.py --topic "GTA 6" --sources twitch --max-uploads 1 --install 18:00
```

Logs: `automate/run.log`. A scheduled run's console output: `automate/task_output.log`.

On the free Upload-Post plan use `--max-uploads 1` with the daily task; when the monthly limit
is reached the run stops and the rest stay approved for later.

## Tests

```bash
python -m unittest discover -s automate/tests -v
```

## Rights

Uploading other people's streams can bring copyright strikes. Prefer creators who allow
clipping; every description credits the original stream.

See `AUTOMATION_PLAN.md` for the original design and the split between Person A and Person B.
Started as a fork of [Youtube-clipper-skill](https://github.com/op7418/Youtube-clipper-skill) (MIT).
