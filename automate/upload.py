#!/usr/bin/env python3
"""Person B: upload approved clips from automate/queue.json to YouTube.

    python automate/upload.py [--max-uploads 5] [--privacy private] [--dry-run]

Reads entries with status "approved", uploads the highest scores first (at most
--max-uploads per run), and writes each result back to queue.json:
    "uploaded" + video_id + uploaded_at     or     "failed" + error
A second run never uploads the same entry again.

One-time setup (see AUTOMATION_PLAN.md): put the Google Cloud OAuth client (Desktop app) at
automate/client_secret.json. The first run opens a browser to sign in with the channel's
Google account and saves automate/token.json, which is refreshed automatically afterwards.

Needs: pip install -r automate/requirements.txt
"""

import argparse
import datetime
import sys
from pathlib import Path

from make_clips import read_json, write_json   # same queue.json read/safe-write as Person A

HERE = Path(__file__).resolve().parent
QUEUE = HERE / "queue.json"
LOG = HERE / "run.log"
CLIENT_SECRET = HERE / "client_secret.json"
TOKEN = HERE / "token.json"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CATEGORY_GAMING = "20"
CHUNK = 8 * 1024 * 1024
MAX_RETRIES = 6


class StopRun(Exception):
    """Quota used up or no network: stop, keep the rest approved for the next run."""


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} [upload] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def save(entry):
    """Write one entry into a fresh read of queue.json, so entries make_clips added meanwhile survive."""
    queue = read_json(QUEUE, [])
    for i, e in enumerate(queue):
        if e.get("file") == entry["file"]:
            queue[i] = entry
            break
    else:   # unreadable queue or entry gone: keep the upload record anyway
        queue.append(entry)
    write_json(QUEUE, queue)


def pending(queue, limit, match=None):
    """Approved entries (whose file contains `match`, if given), highest score first."""
    todo = [e for e in queue if e.get("status") == "approved"
            and (not match or match.lower() in e.get("file", "").lower())]
    todo.sort(key=lambda e: e.get("score") or 0, reverse=True)
    return todo[:max(0, limit)]


def youtube_client():
    """Authorised YouTube Data API v3 client; runs the browser sign-in the first time."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # noqa: BLE001 - revoked/expired refresh token
            log(f"token refresh failed ({exc}); signing in again")
            creds = None
    if not creds or not creds.valid:
        if not CLIENT_SECRET.exists():
            msg = (f"missing {CLIENT_SECRET}: create an OAuth client (Desktop app) in Google "
                   f"Cloud Console and save it there. See AUTOMATION_PLAN.md.")
            log(msg)   # also in run.log, not only on stderr
            raise SystemExit(1)
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        # timeout: a scheduled run must fail, not wait forever for a browser sign-in
        creds = flow.run_local_server(port=0, timeout_seconds=300)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def body_for(entry, privacy):
    title = (entry.get("yt_title") or Path(entry["file"]).stem)[:100]
    return {
        "snippet": {"title": title, "description": (entry.get("yt_description") or "")[:5000],
                    "tags": entry.get("tags") or [], "categoryId": CATEGORY_GAMING},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }


def _status_of(exc):
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None)


def _is_quota(exc):
    text = str(getattr(exc, "content", b"") or exc)
    return _status_of(exc) == 403 and ("quotaExceeded" in text or "uploadLimitExceeded" in text)


def upload_one(youtube, entry, privacy, media_factory=None):
    """Upload one file; returns the new video id. The library retries 5xx, 429 and network errors."""
    if media_factory is None:
        from googleapiclient.http import MediaFileUpload

        def media_factory(path):
            return MediaFileUpload(path, mimetype="video/mp4", chunksize=CHUNK, resumable=True)

    path = Path(entry["file"])
    if not path.exists():
        raise FileNotFoundError(f"clip file missing: {path}")
    request = youtube.videos().insert(part="snippet,status", body=body_for(entry, privacy),
                                      media_body=media_factory(str(path)))
    response = None
    while response is None:
        try:
            _, response = request.next_chunk(num_retries=MAX_RETRIES)
        except Exception as exc:  # noqa: BLE001 - googleapiclient raises HttpError / IO errors
            # no HTTP status = no reply at all (offline, DNS) even after the library's retries
            if _is_quota(exc) or _status_of(exc) is None:
                raise StopRun(str(exc)) from exc
            raise
    if "id" not in response:
        raise RuntimeError(f"unexpected upload response: {response}")
    return response["id"]


def run(max_uploads, privacy, dry_run=False, client_factory=youtube_client, uploader=upload_one,
        match=None):
    queue = read_json(QUEUE, [])
    todo = pending(queue, max_uploads, match)
    waiting = sum(1 for e in queue if e.get("status") == "approved")
    log(f"{waiting} approved in queue; uploading {len(todo)} (max {max_uploads}, {privacy})")
    if dry_run:
        for e in todo:
            log(f"  would upload [{e.get('score')}] {e.get('yt_title')}  <- {e['file']}")
        return 0
    if not todo:
        return 0

    youtube = client_factory()
    done = failed = 0
    for entry in todo:
        log(f"uploading [{entry.get('score')}] {entry.get('yt_title')}")
        try:
            video_id = uploader(youtube, entry, privacy)
        except StopRun as exc:
            log(f"quota reached or offline, stopping; the rest stay approved ({str(exc)[:200]})")
            break
        except Exception as exc:  # noqa: BLE001 - record and continue with the next clip
            entry["status"], entry["error"] = "failed", str(exc)[:500]
            failed += 1
            log(f"  failed: {entry['error']}")
        else:
            entry["status"], entry["video_id"] = "uploaded", video_id
            entry["privacy"] = privacy
            entry["uploaded_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            entry.pop("error", None)
            done += 1
            log(f"  uploaded: https://youtu.be/{video_id}")
        save(entry)   # after every clip, so a crash never re-uploads one
    log(f"done: {done} uploaded, {failed} failed")
    return 1 if failed and not done else 0


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-uploads", type=int, default=5,
                    help="per run; one upload costs ~1,600 of the 10,000 daily quota units")
    ap.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private",
                    help="unaudited API projects are locked to private anyway")
    ap.add_argument("--dry-run", action="store_true", help="list what would be uploaded")
    ap.add_argument("--match", help="only entries whose file path contains this text")
    opts = ap.parse_args()
    return run(opts.max_uploads, opts.privacy, opts.dry_run, match=opts.match)


if __name__ == "__main__":
    sys.exit(main())
