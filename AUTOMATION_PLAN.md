# Plan: fully automated topic → clips → YouTube upload

## Context
The user wants the whole job automated with no human step: find streams for a topic, have Claude choose the streams and approve each clip, cut vertical 9:16 clips with English captions, and upload them to the user's YouTube channel.

Building on the installed `stream-clips` skill (`C:\Users\HP\.claude\skills\stream-clips`). It already does find → listen (Whisper) → Claude picks moments with a 1-100 score → 9:16 clips → `clips.json` + `POSTING.md` + `checks/*.jpg`. What's missing is Claude choosing the streams, a Claude review of the finished clips, the upload, and a schedule.

Already confirmed on this machine: Python 3.11, ffmpeg (full build), node, the `claude` CLI on PATH, and `~/.streamclips/venv` (setup already ran).

## What gets built
Two scripts, `automate/make_clips.py` and `automate/upload.py` (see "Split between two people"), in the user's repo (`Youtube-clipper-skill` → `bhzcode1/youtube_automate`). It uses only the stdlib, plus the two Google libraries for the upload step. Run it like this:
```
python automate/run.py --topic "GTA 6" [--keywords "gta 6,gta vi"] [--streams 2] [--clips 3] [--max-uploads 5] [--privacy private]
```

Steps, each one a `subprocess` call or a small function:

1. **Find:** run `python $SC/scripts/streamclips.py find --topic T --keywords K --count 10`. Collect the indented `https://` lines (same output format as `print_found` in `scripts/sc/cli.py`).
2. **Claude chooses streams:** run `claude -p --model sonnet` with the numbered list, the topic, and the URLs already in `automate/state.json`. The prompt asks for a JSON array of the best `--streams` numbers, skipping reuploads, off-topic results and anything already clipped. Parse the first `[...]` in the reply. If parsing fails, fall back to the top N by views.
3. **Clip:** run `streamclips clip <urls> --topic T --clips C --brain claude --model sonnet --language en`. Moment picking is already Claude's job here. Raise `min_score` in `~/StreamClips/settings.json` (currently 40, see `scripts/sc/settings.py:29`) to about 60 so only strong picks get cut.
4. **Claude reviews each clip:** for every new `~/StreamClips/<today>/<topic>/*/clips.json`, run `claude -p --allowedTools Read`. Claude gets the clip's entry (title, caption, hashtags, score, why, sync) and the path of `checks/<clip>.jpg`, and returns `{"ok": bool, "reason": "...", "yt_title": "...", "yt_description": "..."}`. It rejects a clip if the face or captions are cut off, the title is wrong, `sync` is below 0.5, or the clip doesn't stand on its own. The YouTube title gets `#Shorts`. The description credits the original stream (the creator's name plus the `source.url` from clips.json).
5. **Upload:** YouTube Data API v3 `videos.insert` as a resumable upload (`google-api-python-client`, `google-auth-oauthlib`). Uploads at most `--max-uploads` approved clips per run, highest score first. OAuth is one-time: `automate/client_secret.json` (the user creates it in Google Cloud) → browser sign-in → `automate/token.json` is refreshed automatically after that.
6. **Record:** append every uploaded file, video id, source URL and time to `automate/state.json`, and the rejected clips with Claude's reason. The next run skips anything listed there.
7. **Schedule:** a Windows Task Scheduler entry runs it on a timer, created with one command: `schtasks /Create /SC DAILY /ST 18:00 /TN youtube_automate /TR "python E:\...\automate\run.py --topic \"GTA 6\""`. Output goes to `automate/run.log`.

`.gitignore` gets `automate/client_secret.json`, `automate/token.json`, `automate/state.json` and `automate/run.log`. Keeping secrets and the channel token out of the repo is required, not optional.

## Split between two people
The script becomes two files that meet at one shared file. Each person can build and test their half alone.

**Shared contract (agree on this first):** `automate/queue.json`, a JSON array. Person A appends to it and Person B reads it and updates it:
```json
{"file": "C:/Users/HP/StreamClips/2026-11-25/gta-6/x__y/01_title.mp4",
 "yt_title": "... #Shorts", "yt_description": "... Credit: <creator> <source_url>",
 "tags": ["gta6", "shorts"], "score": 82, "source_url": "https://...",
 "status": "approved"}
```
`status` moves from `approved` to `uploaded` (and gets a `video_id`), or to `failed` (and gets an `error`). Person B can test with one hand-written entry that points at any short .mp4, without waiting for Person A.

### Person A: making clips (`automate/make_clips.py`): steps 1–4 above
- Find streams → Claude chooses the streams → `streamclips clip` → Claude reviews each clip.
- Writes approved clips to `queue.json`, and rejected ones (with Claude's reason) to `automate/rejected.json`.
- Skips source URLs that are already in `queue.json`, so the same stream isn't clipped twice.
- Raises `min_score` to 60 in `~/StreamClips/settings.json`.
- Done when: `python automate/make_clips.py --topic chess --streams 1 --clips 1` leaves at least one `approved` entry in `queue.json` whose file plays as a 9:16 clip with captions.

### Person B: uploading and scheduling (`automate/upload.py` + scheduler): steps 5–7 above
- The one-time Google Cloud setup and OAuth (`client_secret.json` → `token.json`).
- Reads `approved` entries in `queue.json`, uploads the highest scores first, at most `--max-uploads` per run, using `--privacy private` by default. Writes back `uploaded` with the `video_id`, or `failed` with the error.
- Adds the secrets, queue and log files to `.gitignore`.
- Creates the Task Scheduler entry that runs `make_clips.py` and then `upload.py`, logging to `automate/run.log`.
- Done when: a hand-written queue entry shows up as a private Short in YouTube Studio, and a second run doesn't upload it again.

**Final joint check:** run the scheduled task once and confirm a clip goes all the way from search to a private video in YouTube Studio.

## Things the user must do once (can't be automated)
1. In Google Cloud Console: create a project, enable **YouTube Data API v3**, set up the OAuth consent screen (External, add the user's own account as a test user), create an OAuth client of type **Desktop app**, and download it as `automate/client_secret.json`.
2. Run the script once by hand and sign in with the channel's Google account.

## Limits that shape the design
- **Uploads stay private until the API project is audited.** YouTube locks videos uploaded from an unverified API project to private. So the default is `--privacy private`: the user publishes from YouTube Studio, or applies for the free YouTube API audit, then switches to `public`.
- **Quota:** 10,000 units a day, and one upload costs about 1,600, so roughly 6 uploads a day. The default `--max-uploads 5` keeps it under that.
- **Rights:** auto-uploading other people's streams can bring copyright strikes on the channel. Safest is to limit sources to creators who allow clipping (clipping programs) by listing them in `~/StreamClips/watchlist.txt`. The credit line in each description is always added.

## Skipped (add when needed)
- Several topics per run: run the scheduled task once per topic.
- Custom thumbnails and `publishAt` scheduled publishing: they need an audited project anyway.
- No changes to the stream-clips skill or the old Youtube-clipper-skill scripts.

## Verification
1. `python $SC/scripts/streamclips.py doctor`: everything `ok`, brain `claude`.
2. Dry run: `python automate/run.py --topic chess --streams 1 --clips 1 --max-uploads 0`. Check that Claude's stream choice and the review verdict show up in `run.log` and `state.json`, and that nothing is uploaded.
3. Real run with `--max-uploads 1 --privacy private`. The video shows up as private in YouTube Studio with the `#Shorts` title and the credit line.
4. Run the same command again. It skips the stream and clip it already did (state.json).
5. `schtasks /Run /TN youtube_automate`, then check that `run.log` filled in.
