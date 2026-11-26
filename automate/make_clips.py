#!/usr/bin/env python3
"""Person A: topic -> streams -> clips -> Claude review -> automate/queue.json.

    python automate/make_clips.py --topic "GTA 6" [--keywords "gta 6,gta vi"]
                                  [--streams 2] [--clips 3] [--model sonnet]
    python automate/make_clips.py --topic chess --urls https://youtu.be/...   # skip find/choose

Approved clips are appended to automate/queue.json (status "approved"), rejected ones to
automate/rejected.json. Source URLs already in either file are never clipped again.

Captions are always English. Each creator's spoken language comes from automate/creators.json
({"Techno Gamerz": "hi", ...}); unknown creators are guessed by Claude and saved there, so
the file can be corrected by hand. Non-English speech is translated to English by Whisper,
both for the transcript Claude picks moments from and for the burned-in captions.
Standard library only.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = HERE / "queue.json"
REJECTED = HERE / "rejected.json"
LOG = HERE / "run.log"
CREATORS = HERE / "creators.json"
DEFAULT_COOKIES = HERE / "cookies.txt"   # optional YouTube login, never committed
RUNNER = HERE / "sc_runner.py"
VENV = Path.home() / ".streamclips" / "venv"
VENV_BIN = VENV / ("Scripts" if os.name == "nt" else "bin")
VENV_PY = VENV_BIN / ("python.exe" if os.name == "nt" else "python")

DATA = Path(os.environ.get("STREAMCLIPS_HOME", Path.home() / "StreamClips"))
SETTINGS = DATA / "settings.json"
# The plan guessed ~60, but on streams without chat replay (all Twitch VODs, many YouTube
# streams) Claude scores good streamer moments 50-58, so 60 cut everything. Claude's review
# of the finished clip is the real quality gate.
MIN_SCORE = 50
MIN_SYNC = 0.5
# Clip quality, merged over ~/StreamClips/settings.json for our runs only (via sc_runner).
# The facecam is a small box of the stream, enlarged ~2.5x into the top panel: a 1080p source
# makes it blurry. Downloading up to 4K (falls back to 1440p/1080p) keeps it sharp. The output
# stays 1080x1920 (Shorts' size; caption sizes are set for it).
QUALITY = {"video_height": 2160, "crf": 16, "x264_preset": "slow"}
YT_TITLE_MAX = 100
CLAUDE_TIMEOUT = 600
CLIP_TIMEOUT = 6 * 3600
# The transcript mixes the streamer's voice with game audio (cutscenes, NPCs), and the
# picker can't tell them apart unless told to.
FOCUS = ("the transcript also contains GAME AUDIO (cutscenes, NPC and character dialogue). "
         "Only pick moments driven by the streamer's OWN voice: their reactions, rage, laughs, "
         "jokes, commentary, clutch plays. Never pick a cutscene or a game character's line, "
         "and never title a clip with a line the streamer did not say.")


# ---------------------------------------------------------------- helpers

def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} [make_clips] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def find_skill():
    candidates = [os.environ.get("STREAMCLIPS_SKILL"),
                  Path.home() / ".claude" / "skills" / "stream-clips",
                  Path.home() / ".config" / "opencode" / "skills" / "stream-clips"]
    for c in candidates:
        if c and (Path(c) / "scripts" / "streamclips.py").exists():
            return Path(c) / "scripts" / "streamclips.py"
    sys.exit("stream-clips skill not found. Set STREAMCLIPS_SKILL to its folder.")


COOKIES_FILE = None   # set from --cookies-file in main()


def child_env():
    env = dict(os.environ)
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):   # nested claude -p refuses otherwise
        env.pop(key, None)
    env["NO_COLOR"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if COOKIES_FILE:
        env["SC_COOKIES_FILE"] = str(COOKIES_FILE)
    return env


def claude(prompt, model, cwd=None, tools=None):
    """Ask the claude CLI; returns its text reply, or None on failure."""
    exe = shutil.which("claude")
    if not exe:
        log("claude CLI not on PATH")
        return None
    cmd = [exe, "-p", "--model", model]
    if tools:
        cmd += ["--allowedTools", tools]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=CLAUDE_TIMEOUT,
                              env=child_env(), cwd=cwd)
    except subprocess.TimeoutExpired:
        log("claude timed out")
        return None
    if proc.returncode != 0:
        log(f"claude failed: {(proc.stderr or proc.stdout).strip()[:300]}")
        return None
    return proc.stdout


def first_json(text, opener):
    """Parse the first balanced [...] or {...} in text."""
    closer = "]" if opener == "[" else "}"
    start = (text or "").find(opener)
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
        start = text.find(opener, start + 1)
    return None


def done_urls():
    seen = set()
    for path in (QUEUE, REJECTED):
        for e in read_json(path, []):
            if e.get("source_url"):
                seen.add(e["source_url"].strip())
    return seen


def ensure_min_score(exact=None):
    """Raise min_score to MIN_SCORE, or set it to `exact` when --min-score is given."""
    cfg = read_json(SETTINGS, {})
    if not isinstance(cfg, dict):
        sys.exit(f"{SETTINGS} is not valid JSON")
    current = cfg.get("min_score") or 0
    target = exact if exact is not None else max(current, MIN_SCORE)
    if current != target:
        cfg["min_score"] = target
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        write_json(SETTINGS, cfg)
        log(f"set min_score={target} in {SETTINGS}")


# ---------------------------------------------------------------- 1. find

ITEM = re.compile(r"^\s{2}(\d+)\.\s+(.*?)\s+([\d,?]+) views")
URL = re.compile(r"^\s{4,}(https?://\S+)")


def find_streams(sc, topic, keywords, count):
    cmd = [str(VENV_PY), str(RUNNER), str(sc.parent), "find", "--topic", topic,
           "--count", str(count)]
    if keywords:
        cmd += ["--keywords", keywords]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=child_env(), timeout=1800)
    found, current = [], None
    for line in proc.stdout.splitlines():
        m = ITEM.match(line)
        if m:
            views = m.group(3).replace(",", "")
            current = {"line": line.strip(), "name": m.group(2).strip(),
                       "views": int(views) if views.isdigit() else 0}
            continue
        m = URL.match(line)
        if m and current:
            current["url"] = m.group(1)
            found.append(current)
            current = None
    if not found:
        log(f"find returned nothing: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return found


HOSTS = {"youtube": ("youtube.com", "youtu.be"), "twitch": ("twitch.tv",), "kick": ("kick.com",)}


def filter_sources(found, sources):
    """Keep candidates hosted on the given platforms (e.g. when YouTube blocks downloads)."""
    if not sources:
        return found
    wanted = [h for name in sources for h in HOSTS.get(name, (name,))]
    kept = [s for s in found if any(h in s["url"] for h in wanted)]
    if len(kept) < len(found):
        log(f"--sources {','.join(sources)}: kept {len(kept)} of {len(found)} candidate(s)")
    return kept


# ---------------------------------------------------------------- 2. choose

def choose_streams(found, topic, n, model, skip):
    fresh = [s for s in found if s["url"] not in skip]
    if not fresh:
        return []
    listing = "\n".join(f"{i}. {s['line']}\n   {s['url']}" for i, s in enumerate(fresh, 1))
    prompt = (
        f"You are choosing livestreams/videos to cut into YouTube Shorts about: {topic}.\n\n"
        f"Candidates:\n{listing}\n\n"
        f"Choose the best {n}. Prefer original creator streams that are clearly on-topic and "
        f"likely to have strong moments. Skip reuploads, compilations, other people's clips, "
        f"news/reaction channels re-hosting content, and anything off-topic.\n"
        f"Reply with ONLY a JSON array of candidate numbers, best first, e.g. [3, 1]. "
        f"Reply [] if none are suitable.")
    reply = claude(prompt, model)
    picks = first_json(reply, "[")
    if isinstance(picks, list) and all(isinstance(p, int) for p in picks):
        chosen = [fresh[p - 1] for p in picks if 1 <= p <= len(fresh)][:n]
        log(f"claude chose streams {picks}")
        return chosen
    log("could not parse claude's stream choice; falling back to top by views")
    return sorted(fresh, key=lambda s: s["views"], reverse=True)[:n]


# ---------------------------------------------------------------- 2b. language per creator

def channel_name(url):
    """Channel name for a link given with --urls (find results already carry it)."""
    exe = VENV_BIN / ("yt-dlp.exe" if os.name == "nt" else "yt-dlp")
    try:
        extra = ["--cookies", str(COOKIES_FILE)] if COOKIES_FILE else []
        proc = subprocess.run([str(exe), "--print", "channel", "--skip-download", "--no-warnings",
                               "--playlist-items", "1", *extra, url], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120, env=child_env())
        return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def speech_languages(streams, model):
    """{url: language code} from creators.json, asking Claude about unknown creators."""
    known = {k.lower(): v for k, v in read_json(CREATORS, {}).items()}
    langs, unknown = {}, []
    for s in streams:
        lang = known.get((s.get("name") or "").lower())
        if lang:
            langs[s["url"]] = lang
        else:
            unknown.append(s)
    if unknown:
        listing = "\n".join(f"{i}. channel: {s.get('name') or '?'} | title: {s.get('title') or s.get('line', '')}"
                            for i, s in enumerate(unknown, 1))
        reply = claude(
            "For each YouTube channel below, what language does the creator mainly SPEAK on "
            "stream? Use what you know about the creator, and the title as a hint (Indian "
            "creators often write English titles but speak Hindi).\n\n" + listing +
            '\n\nReply with ONLY a JSON object of number -> ISO 639-1 code, e.g. {"1": "hi", "2": "en"}.',
            model)
        guess = first_json(reply, "{") or {}
        creators = read_json(CREATORS, {})
        for i, s in enumerate(unknown, 1):
            answered = guess.get(str(i)) if isinstance(guess, dict) else None
            lang = str(answered or "en").lower()[:2]
            langs[s["url"]] = lang
            if not answered:   # a failed call must not be remembered as a fact
                log(f"no language for {s.get('name') or s['url']!r}; assuming English this run")
            elif s.get("name"):
                creators[s["name"]] = lang
                log(f"creator {s['name']!r} speaks {lang!r} (claude's guess, saved to creators.json)")
        write_json(CREATORS, creators)
    return langs


# ---------------------------------------------------------------- 3. clip

# stream-clips' own "fix it and run again" failures (login, bot check, network, cookies):
# the stream is fine, so it must not be recorded as "no clips made" and skipped forever.
RETRY_LATER = ("yt-dlp failed", "needs a login", "not a bot", "blocked the download",
               "subscriber-only")


def retry_later(line):
    """True for a stream-clips failure line ("[mm:ss] x ...") that a later run can fix.
    Warnings ("!"), e.g. a skipped chat replay, don't count."""
    return (re.match(r"^\[[\d:]+\] x ", line) is not None
            and any(m in line.lower() for m in RETRY_LATER))


def make_clips(sc, urls, topic, keywords, clips, model, language, section=None, repick=False,
               cookies=None):
    """language: what the creators speak. Non-English speech gets English captions.
    Returns (folders, blocked): only folders whose clips.json was written by THIS run (a
    folder can hold an older clips.json when this run made nothing), and whether a
    download/login problem stopped something (see retry_later)."""
    cmd = [str(VENV_PY), str(RUNNER), str(sc.parent), "clip", *urls, "--topic", topic,
           "--clips", str(clips), "--brain", "claude", "--model", model, "--language", language,
           "--focus", FOCUS]
    if section:
        cmd += ["--section", section]
    if repick:
        cmd += ["--repick"]
    if cookies:
        cmd += ["--cookies", cookies]
    if keywords:
        cmd += ["--keywords", keywords]
    env = child_env()
    env["SC_SETTINGS"] = json.dumps(QUALITY)
    if language != "en":   # Whisper translates the speech to English
        env["SC_SCAN_TASK"] = env["SC_CAPTION_TASK"] = "translate"
    log(f"running: streamclips clip ({language}"
        + (" -> English captions" if language != "en" else "") + ") " + " ".join(urls))
    folders, blocked = [], False
    started = time.time() - 1
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env)
    deadline = time.time() + CLIP_TIMEOUT
    with LOG.open("a", encoding="utf-8") as fh:
        for line in proc.stdout:
            print(line, end="", flush=True)
            fh.write(line)
            m = re.match(r"^\s*folder:\s*(.+?)\s*$", line)
            if m:
                folders.append(Path(m.group(1)))
            blocked = blocked or retry_later(line)
            if time.time() > deadline:
                proc.kill()
                log("streamclips clip timed out")
                break
    proc.wait()
    log(f"streamclips clip exited with {proc.returncode}")
    return fresh_folders(folders, started), blocked


def fresh_folders(folders, since):
    """Folders whose clips.json exists and was written at or after `since`."""
    keep = []
    for f in folders:
        sheet = f / "clips.json"
        if sheet.exists() and sheet.stat().st_mtime >= since:
            keep.append(f)
        elif sheet.exists():
            log(f"ignoring {sheet}: left over from an earlier run")
    return keep


# ---------------------------------------------------------------- 4. review

SHEET_FRAMES = 8


def contact_sheet(clip, seconds, out):
    """8 frames spread over the whole clip, tiled 4x2, so captions are actually visible."""
    rate = SHEET_FRAMES / max(float(seconds or 30), 1.0)
    proc = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(clip), "-vf",
                           f"fps={rate:.4f},scale=360:640,tile=4x2", "-frames:v", "1", str(out)],
                          capture_output=True, text=True)
    return proc.returncode == 0 and out.exists()


def review(entry, source, folder, topic, model):
    """Returns {"ok", "reason", "seen", "yt_title", "yt_description"}."""
    if entry.get("sync") is not None and entry["sync"] < MIN_SYNC:
        return {"ok": False, "reason": f"sync {entry['sync']} below {MIN_SYNC}"}
    clip = folder / entry["file"]
    if not clip.exists():
        return {"ok": False, "reason": "clip file missing"}
    check = Path("checks") / (clip.stem + "_review.jpg")
    (folder / "checks").mkdir(exist_ok=True)
    has_check = contact_sheet(clip, entry.get("seconds"), folder / check)
    if not has_check:   # fall back to the skill's own 3-frame check image
        check = Path("checks") / (clip.stem + ".jpg")
        has_check = (folder / check).exists()
    info = {k: entry.get(k) for k in ("title", "caption", "hashtags", "score", "why", "type",
                                      "seconds", "sync", "look")}
    prompt = (
        f"You are the final reviewer before a vertical 9:16 clip is uploaded to YouTube Shorts "
        f"for a channel about: {topic}.\n\n"
        f"Source stream: {source.get('name')} - {source.get('title')}\n"
        f"Clip data:\n{json.dumps(info, indent=2, ensure_ascii=False)}\n\n"
        + (f"Use the Read tool to look at {check.as_posix()}: frames from the finished clip "
           f"in time order, left to right, top to bottom. The burned-in captions are the "
           f"large uppercase words (white, current word yellow) in the lower half; small "
           f"text at the very bottom is the game's own subtitles, not ours.\n"
           if has_check else
           "No contact-sheet image exists for this clip; judge from the data only.\n")
        + "Reject the clip if ANY of these is true:\n"
        "- our captions are gibberish, not real readable English, or do not match what a "
        "streamer would plausibly say\n"
        "- our captions are mostly a game character's / cutscene dialogue rather than the "
        "streamer's own words (compare them with the game's subtitles at the very bottom)\n"
        "- a face or the captions are cut off or badly framed\n"
        "- most of the clip is a game logo, loading screen or cutscene rather than the "
        "streamer's own moment or reaction\n"
        "- the title is wrong or misleading, or the clip is off-topic\n"
        "- it does not make sense on its own without the rest of the stream\n\n"
        "If approved, write a YouTube title (max 90 characters, no hashtags, no clickbait "
        "lies) and a short description (1-3 sentences about the moment).\n"
        'Reply with ONLY JSON: {"seen": "one sentence: what the frames and our captions '
        'show", "ok": true|false, "reason": "...", "yt_title": "...", "yt_description": "..."}')
    verdict = first_json(claude(prompt, model, cwd=str(folder),
                                tools="Read" if has_check else None), "{")
    if not isinstance(verdict, dict) or "ok" not in verdict:
        return {"ok": False, "reason": "no valid verdict from claude"}
    return verdict


def youtube_fields(verdict, entry, source):
    title = (verdict.get("yt_title") or entry.get("title") or "Stream moment").strip()
    title = title.replace("#Shorts", "").replace("#shorts", "").strip()
    suffix = " #Shorts"
    title = title[:YT_TITLE_MAX - len(suffix)].rstrip() + suffix
    desc = (verdict.get("yt_description") or entry.get("caption") or "").strip()
    credit = f"Credit: {source.get('name') or 'the original creator'} {source.get('url') or ''}".strip()
    desc = f"{desc}\n\n{credit}".strip()
    tags = [h.lstrip("#") for h in entry.get("hashtags") or [] if h.strip("#")]
    if "shorts" not in [t.lower() for t in tags]:
        tags.append("shorts")
    return title, desc, tags


# ---------------------------------------------------------------- main

def main():
    for stream in (sys.stdout, sys.stderr):   # Windows consoles default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap =argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--keywords")
    ap.add_argument("--streams", type=int, default=2)
    ap.add_argument("--clips", type=int, default=3, help="clips per stream")
    ap.add_argument("--find-count", type=int, default=10)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--language",
                    help="spoken language of every stream (default: per creator, see creators.json)")
    ap.add_argument("--urls", nargs="+", help="clip these links instead of finding streams")
    ap.add_argument("--section", help='only part of each stream, e.g. "0:00:00-0:30:00"')
    ap.add_argument("--cookies", choices=["chrome", "edge", "firefox", "brave", "opera", "vivaldi"],
                    help="use this browser's YouTube login (age-restricted streams)")
    ap.add_argument("--cookies-file", type=Path,
                    help=f"YouTube cookies.txt (Netscape format) for when YouTube asks to sign "
                         f"in (default: {DEFAULT_COOKIES.name} next to this script, if present)")
    ap.add_argument("--sources", type=lambda s: [x.strip().lower() for x in s.split(",") if x.strip()],
                    help="only clip streams from these hosts, e.g. twitch,kick (default: all)")
    ap.add_argument("--review-only", nargs="+", metavar="FOLDER",
                    help="skip find/clip; review the clips in these stream-clips output folders")
    ap.add_argument("--repick", action="store_true",
                    help="pick moments again instead of reusing cached picks")
    ap.add_argument("--min-score", type=int,
                    help=f"set min_score exactly (default: raise it to {MIN_SCORE})")
    opts = ap.parse_args()
    global COOKIES_FILE
    COOKIES_FILE = opts.cookies_file or (DEFAULT_COOKIES if DEFAULT_COOKIES.exists() else None)
    if COOKIES_FILE and not Path(COOKIES_FILE).exists():
        sys.exit(f"cookies file not found: {COOKIES_FILE}")

    if opts.review_only:   # clips already made (e.g. by a manual streamclips run): step 4 only
        folders = [Path(f) for f in opts.review_only if (Path(f) / "clips.json").exists()]
        log(f"review only: {len(folders)} folder(s)")
        review_folders(folders, opts.topic, opts.model)
        return 0

    log(f"start topic={opts.topic!r} streams={opts.streams} clips={opts.clips}")
    sc = find_skill()
    ensure_min_score(opts.min_score)
    skip = done_urls()

    if opts.urls:
        # Links given by hand are always clipped (already-queued files are still deduped).
        chosen = [{"url": u, "name": "" if opts.language else channel_name(u)} for u in opts.urls]
        for u in opts.urls:
            if u.strip() in skip:
                log(f"note: {u} was handled before; clipping it again because it was given by hand")
    else:
        found = filter_sources(find_streams(sc, opts.topic, opts.keywords, opts.find_count),
                               opts.sources)
        log(f"found {len(found)} candidate(s)")
        chosen = choose_streams(found, opts.topic, opts.streams, opts.model, skip)
    urls = [s["url"] for s in chosen]
    if not urls:
        log("nothing new to clip")
        return 0
    langs = ({u: opts.language for u in urls} if opts.language
             else speech_languages(chosen, opts.model))
    for s in chosen:
        log(f"chosen: {s['url']} ({s.get('name') or '?'}, speaks {langs[s['url']]})")

    folders, blocked = [], False
    for lang in sorted(set(langs.values())):   # one streamclips run per spoken language
        group = [u for u in urls if langs[u] == lang]
        made, failed = make_clips(sc, group, opts.topic, opts.keywords, opts.clips, opts.model,
                                  lang, opts.section, opts.repick, opts.cookies)
        folders, blocked = folders + made, blocked or failed
    rejected = read_json(REJECTED, [])

    # Streams that gave no clips are recorded too, so the next run doesn't redo them.
    # shortcut: one download/login failure keeps EVERY clip-less stream of this run retryable
    # (the failure lines don't always name the URL); a re-run reuses the cached transcripts.
    if blocked:
        log("download or login problem (see above); clip-less streams stay open for a retry. "
            "If YouTube wants a login, save automate/cookies.txt (see README).")
    produced = {(read_json(f / "clips.json", {}).get("source") or {}).get("url") for f in folders}
    for u in urls:
        if u not in produced and not opts.section and not blocked:   # partial run: says nothing
            rejected.append({"source_url": u, "reason": "no clips made",
                             "added": datetime.datetime.now().isoformat(timespec="seconds")})
            log(f"no clips from {u}; it will be skipped next time")
    write_json(REJECTED, rejected)
    if not folders:
        log("no clips were made")
        return 1

    review_folders(folders, opts.topic, opts.model)
    return 0


def review_folders(folders, topic, model):
    """Step 4: review every clip in these folders once; approved -> queue, else rejected.
    Returns the number approved."""
    queue, rejected = read_json(QUEUE, []), read_json(REJECTED, [])
    # never review the same file twice: verdicts are not deterministic
    seen_files = {e.get("file") for e in queue} | {e.get("file") for e in rejected}
    approved = 0
    for folder in folders:
        data = read_json(folder / "clips.json", {})
        source = data.get("source", {})
        for entry in data.get("clips", []):
            path = (folder / entry["file"]).resolve().as_posix()
            if path in seen_files:
                log(f"skip {entry['file']}: already reviewed")
                continue
            verdict = review(entry, source, folder, topic, model)
            if verdict.get("seen"):
                log(f"claude saw: {verdict['seen']}")
            base = {"file": path, "score": entry.get("score"), "source_url": source.get("url"),
                    "added": datetime.datetime.now().isoformat(timespec="seconds")}
            if verdict.get("ok"):
                title, desc, tags = youtube_fields(verdict, entry, source)
                # re-read first: upload.py may have marked clips "uploaded" during this
                # (minutes-long) review; writing our old copy back would re-upload them.
                # Unreadable right now -> our copy, never [] (that would wipe the queue).
                queue = read_json(QUEUE, queue)
                queue.append({**base, "yt_title": title, "yt_description": desc, "tags": tags,
                              "status": "approved"})
                write_json(QUEUE, queue)
                approved += 1
                log(f"approved [{entry.get('score')}] {entry['file']}: {title}")
            else:
                rejected.append({**base, "title": entry.get("title"),
                                 "reason": verdict.get("reason", "")})
                log(f"rejected [{entry.get('score')}] {entry['file']}: {verdict.get('reason')}")
            seen_files.add(path)
            write_json(REJECTED, rejected)   # only make_clips writes this file
    log(f"done: {approved} clip(s) approved -> {QUEUE}")
    return approved


if __name__ == "__main__":
    sys.exit(main())
