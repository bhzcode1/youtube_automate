"""
Person B: upload approved clips from automate/queue.json to YouTube.

Usage:
    python automate/upload.py                     # upload up to 5, private
    python automate/upload.py --max-uploads 1     # just one
    python automate/upload.py --dry-run           # show what would upload, upload nothing
    python automate/upload.py --add automate/myvideo.mp4
                                                  # post one video now; AI writes title + description
    python automate/upload.py --auth-only         # just do the one-time Google sign-in

Two ways to upload (--via):
    upload-post  posts PUBLIC through upload-post.com (free plan: 10/month).
                 Used automatically when automate/uploadpost_key.txt exists.
                 Profile name goes in automate/uploadpost_profile.txt.
    youtube      your own Google API project. Videos stay locked private
                 until Google approves the API audit.

Contract with Person A (make_clips.py):
    queue.json is a JSON array. Person A appends entries with status "approved".
    This script only changes: status ("uploaded" / "failed"), video_id, error, uploaded_at.

One-time setup:
    pip install -r automate/requirements.txt
    Put your OAuth Desktop client file at automate/client_secret.json
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = HERE / "queue.json"
CLIENT_SECRET = HERE / "client_secret.json"
TOKEN = HERE / "token.json"
LOG = HERE / "run.log"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
MAX_RETRIES = 6

# Windows consoles sometimes choke on non-ASCII titles
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} [upload] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:   # same run.log as make_clips / run_daily
        fh.write(line + "\n")


# ---------------------------------------------------------------- queue file

def load_queue():
    if not QUEUE.exists():
        return []
    text = QUEUE.read_text(encoding="utf-8").strip()
    if not text:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("queue.json must be a JSON array")
    return data


def save_queue(entries):
    # keep entries make_clips appended while we were uploading (we hold an old copy)
    try:
        known = {e.get("file") for e in entries}
        entries.extend(e for e in load_queue() if e.get("file") not in known)
    except (OSError, ValueError):
        pass   # unreadable now: still save our copy, so an upload is never recorded twice
    # write to a temp file, then swap, so a crash never leaves half a file
    tmp = QUEUE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, QUEUE)


# ---------------------------------------------------------------- google auth

def get_youtube():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        log("Google libraries missing. Run:")
        log("  pip install google-api-python-client google-auth-oauthlib google-auth-httplib2")
        sys.exit(2)

    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            log(f"Saved sign-in no longer works ({e}).")
            log("If your consent screen is still in 'Testing', tokens die after 7 days:")
            log("publish the app to 'In production', then run: python automate/upload.py --auth-only")
            creds = None

    if not creds or not creds.valid:
        if not CLIENT_SECRET.exists():
            log(f"Missing {CLIENT_SECRET}. Download your OAuth Desktop client JSON there.")
            sys.exit(2)
        if not sys.stdin or not sys.stdin.isatty():
            # running from Task Scheduler: nobody is there to click the browser
            log("Need a browser sign-in but running unattended. Run once by hand:")
            log("  python automate/upload.py --auth-only")
            sys.exit(3)
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        # timeout: a scheduled task can still have a console, so isatty() alone may not catch it
        creds = flow.run_local_server(port=0, prompt="consent", timeout_seconds=300)
        log("Signed in.")

    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------- upload

def clean_title(title):
    title = (title or "Untitled").replace("<", "").replace(">", "").strip()
    if "#shorts" not in title.lower():
        title = f"{title} #Shorts"
    if len(title) > 100:  # YouTube limit
        title = title[:100].rstrip()
    return title


def clean_description(desc):
    desc = (desc or "").replace("<", "").replace(">", "")
    return desc[:5000]


def upload_one(youtube, entry, privacy, category):
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": clean_title(entry.get("yt_title")),
            "description": clean_description(entry.get("yt_description")),
            "tags": entry.get("tags") or [],
            "categoryId": str(category),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(entry["file"], mimetype="video/mp4",
                            chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        # the library retries 5xx, 429 and network errors (incl. DNS) with backoff
        status, response = request.next_chunk(num_retries=MAX_RETRIES)
        if status:
            log(f"  {int(status.progress() * 100)}%")
    return response["id"]


class QuotaUsedUp(Exception):
    pass


def is_quota_error(e):
    if isinstance(e, QuotaUsedUp):
        return True
    text = str(e)
    return "quotaExceeded" in text or "uploadLimitExceeded" in text or "dailyLimitExceeded" in text


def is_offline(e):
    """No reply at all (no network, DNS) even after retries: try again next run, don't fail it.
    requests' errors are OSErrors; httplib2's ServerNotFoundError is not."""
    return isinstance(e, OSError) or type(e).__module__.startswith("httplib2")


# ---------------------------------------------------------------- upload-post.com

UP_KEY = HERE / "uploadpost_key.txt"
UP_PROFILE = HERE / "uploadpost_profile.txt"
UP_URL = "https://api.upload-post.com/api/upload"
UP_STATUS = "https://api.upload-post.com/api/uploadposts/status"


def read_text(path):
    return path.read_text(encoding="utf-8-sig").strip() if path.exists() else ""


def find_youtube_result(obj):
    """Search Upload-Post's JSON for the YouTube result, wherever it sits."""
    if isinstance(obj, dict):
        yt = obj.get("youtube")
        if isinstance(yt, dict):
            return yt
        for v in obj.values():
            found = find_youtube_result(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_youtube_result(v)
            if found is not None:
                return found
    return None


def upload_via_uploadpost(entry, privacy, category, key, profile):
    """Returns (video_id_or_url, request_id). Raises on failure."""
    import hashlib
    import requests

    title = clean_title(entry.get("yt_title"))
    desc = clean_description(entry.get("yt_description"))
    tags = [t.strip().lstrip("#").replace(" ", "") for t in (entry.get("tags") or []) if t.strip()]
    if tags:
        desc = clean_description(desc + "\n\n" + " ".join("#" + t for t in tags))

    # same file -> same id, so Upload-Post never posts it twice even if we retry
    rid = "ya-" + hashlib.sha1(str(entry["file"]).encode("utf-8")).hexdigest()[:24]
    headers = {"Authorization": f"Apikey {key}", "Idempotency-Key": rid}
    data = [
        ("user", profile),
        ("platform[]", "youtube"),
        ("title", title),
        ("youtube_title", title),
        ("youtube_description", desc),
        ("privacyStatus", privacy),
        ("categoryId", str(category)),
        ("selfDeclaredMadeForKids", "false"),
        ("async_upload", "true"),
        ("request_id", rid),
    ]
    with open(entry["file"], "rb") as f:
        r = requests.post(UP_URL, headers=headers, data=data,
                          files={"video": (Path(entry["file"]).name, f, "video/mp4")},
                          timeout=900)
    if r.status_code == 429:
        raise QuotaUsedUp(f"Upload-Post monthly limit reached: {r.text[:300]}")
    if r.status_code in (401, 403):
        raise RuntimeError(f"Upload-Post refused ({r.status_code}). Check your API key / profile name: {r.text[:300]}")
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError(f"Upload-Post HTTP {r.status_code}: {r.text[:300]}")
    if r.status_code >= 400 or j.get("success") is False:
        raise RuntimeError(f"Upload-Post HTTP {r.status_code}: {json.dumps(j)[:400]}")
    rid = j.get("request_id") or rid

    # wait for YouTube to finish (async upload), up to ~15 minutes
    for attempt in range(60):
        yt = find_youtube_result(j)
        if yt is not None and "success" in yt:
            if not yt.get("success"):
                raise RuntimeError(f"YouTube via Upload-Post: {yt.get('error') or yt}")
            vid = yt.get("video_id") or yt.get("post_id") or yt.get("url") or "ok"
            return vid, rid
        status = str(j.get("status", "")).lower()
        if status in ("failed", "error"):
            raise RuntimeError(f"Upload-Post status {status}: {json.dumps(j)[:400]}")
        time.sleep(15)
        log(f"  waiting for YouTube... ({(attempt + 1) * 15}s)")
        # Upload-Post already has the video: a failed status check must never make the
        # caller treat it as unsent (that would post it again next run)
        try:
            j = requests.get(UP_STATUS, headers={"Authorization": f"Apikey {key}"},
                             params={"request_id": rid}, timeout=60).json()
        except (OSError, ValueError):   # requests' errors are OSErrors
            j = {}
    return None, rid  # still processing; caller records it so it is never re-sent


UP_ANALYZE = "https://api.upload-post.com/api/uploadposts/analyze-shorts"


def auto_title_description(entry, key, language="English"):
    """Ask Upload-Post's AI to watch the clip and write the YouTube title + description."""
    import requests
    with open(entry["file"], "rb") as f:
        r = requests.post(UP_ANALYZE, headers={"Authorization": f"Apikey {key}"},
                          data={"platforms": "youtube", "language": language},
                          files={"video": (Path(entry["file"]).name, f, "video/mp4")},
                          timeout=600)
    try:
        j = r.json()
    except ValueError:
        j = {}
    yt = j.get("youtube") if isinstance(j, dict) else None
    if r.status_code >= 400 or not isinstance(yt, dict) or not yt.get("title"):
        raise RuntimeError(f"AI title failed (HTTP {r.status_code}): {r.text[:300]}")
    left = j.get("remaining_analyses")
    if left is not None:
        log(f"  AI analyses left this month: {left}")
    return yt["title"], yt.get("description") or ""


def title_from_filename(path):
    name = Path(path).stem.replace("_", " ").replace("-", " ").strip()
    return name[:90] or "New Short"


def main(argv=None):
    p = argparse.ArgumentParser(description="Upload approved clips from queue.json to YouTube")
    p.add_argument("--max-uploads", type=int, default=5)
    p.add_argument("--via", choices=["upload-post", "youtube"], default=None,
                   help="upload-post (default if automate/uploadpost_key.txt exists) or youtube (your own API project)")
    p.add_argument("--profile", default=None, help="Upload-Post profile name (or put it in automate/uploadpost_profile.txt)")
    p.add_argument("--privacy", choices=["private", "unlisted", "public"], default=None,
                   help="default: public via upload-post, private via youtube")
    p.add_argument("--category", default="24", help="YouTube category id (24 Entertainment, 20 Gaming)")
    p.add_argument("--add", metavar="VIDEO", help="add a video to the queue (title/description written by AI) and post it")
    p.add_argument("--language", default="English", help="language for AI titles")
    p.add_argument("--match", help="only approved entries whose file path contains this text")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--auth-only", action="store_true")
    args = p.parse_args(argv)

    if args.auth_only:
        get_youtube()
        log(f"Sign-in saved to {TOKEN}")
        return

    via = args.via or ("upload-post" if UP_KEY.exists() else "youtube")
    privacy = args.privacy or ("public" if via == "upload-post" else "private")

    queue = load_queue()
    if args.add:
        video = Path(args.add).expanduser()
        if not video.is_file():
            log(f"Video not found: {video}")
            sys.exit(2)
        if any(Path(e.get("file", "")).resolve() == video.resolve() for e in queue):
            log(f"Already in the queue: {video} (not adding it twice)")
        else:
            queue.append({"file": str(video).replace("\\", "/"), "yt_title": "", "yt_description": "",
                          "tags": [], "score": 1000, "source_url": "", "status": "approved",
                          "added_at": datetime.now().isoformat(timespec="seconds")})
            save_queue(queue)
            log(f"added to queue: {video}")
    approved = [e for e in queue if e.get("status") == "approved"
                and (not args.match or args.match.lower() in e.get("file", "").lower())]
    approved.sort(key=lambda e: e.get("score") or 0, reverse=True)
    todo = approved[: max(args.max_uploads, 0)]

    log(f"queue: {len(queue)} entries, {len(approved)} approved, uploading {len(todo)} ({privacy}, via {via})")
    if not todo:
        return

    if args.dry_run:
        for e in todo:
            log(f"  would upload [{e.get('score')}] {clean_title(e.get('yt_title'))}  <- {e.get('file')}")
        return

    youtube = key = profile = None
    if via == "youtube":
        youtube = get_youtube()
        key = read_text(UP_KEY) or None
    else:
        key = read_text(UP_KEY)
        profile = args.profile or read_text(UP_PROFILE)
        if not key:
            log(f"Missing API key. Put your Upload-Post key in {UP_KEY}")
            sys.exit(2)
        if not profile:
            log(f"Missing profile name. Put your Upload-Post profile name in {UP_PROFILE}")
            sys.exit(2)

    done = 0
    for entry in todo:
        title = clean_title(entry.get("yt_title"))
        if not Path(entry.get("file", "")).is_file():
            entry.update(status="failed", error=f"file not found: {entry.get('file')}")
            log(f"FAILED (missing file) {title}")
            save_queue(queue)
            continue
        if not (entry.get("yt_title") or "").strip():
            if key:
                log("writing title + description with AI...")
                try:
                    t, d = auto_title_description(entry, key, args.language)
                    entry.update(yt_title=t, yt_description=d)
                except Exception as e:
                    # mark failed: left "approved" at the top, it would block every later run
                    entry.update(status="failed", error=f"AI title failed: {str(e)[:450]}")
                    log(f"  {e}")
                    log("  not posting it without a good title; marked failed (set it back to "
                        "'approved' or give it a yt_title to retry)")
                    save_queue(queue)
                    continue
            else:
                entry.update(yt_title=title_from_filename(entry["file"]))
            save_queue(queue)  # keep the AI text so it is never paid for twice
            title = clean_title(entry.get("yt_title"))
            log(f"  title: {title}")
        log(f"uploading [{entry.get('score')}] {title}")
        request_id = None
        try:
            if via == "youtube":
                video_id = upload_one(youtube, entry, privacy, args.category)
            else:
                video_id, request_id = upload_via_uploadpost(entry, privacy, args.category, key, profile)
        except Exception as e:
            if is_quota_error(e) or is_offline(e):
                log(f"Upload limit reached or offline ({e}). Leaving the rest as 'approved' for next time.")
                break
            entry.update(status="failed", error=str(e)[:500])
            log(f"FAILED {title}: {e}")
            save_queue(queue)
            continue
        entry.update(status="uploaded", video_id=video_id, via=via, privacy=privacy,
                     uploaded_at=datetime.now().isoformat(timespec="seconds"))
        if request_id:
            entry["request_id"] = request_id
        entry.pop("error", None)
        save_queue(queue)  # save after every upload so a crash never re-uploads
        done += 1
        if video_id is None:
            log("sent, but YouTube is still processing. Check the Upload-Post dashboard / YouTube Studio.")
        elif str(video_id).startswith("http"):
            log(f"done: {video_id}")
        else:
            log(f"done: https://youtube.com/shorts/{video_id}")

    log(f"finished: {done} uploaded")


if __name__ == "__main__":
    main()
