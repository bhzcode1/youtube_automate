"""Run the stream-clips CLI with per-run language options, without editing the skill.

Must be started with the stream-clips venv's python:
    <venv python> sc_runner.py <skill scripts dir> clip URL ... --language hi

Environment (all optional):
    SC_SCAN_TASK      transcribe | translate   Whisper task for the whole-stream scan
    SC_CAPTION_TASK   transcribe | translate   Whisper task for each clip's captions
    SC_SETTINGS       JSON object merged over ~/StreamClips/settings.json for this run only
    SC_COOKIES_FILE   Netscape cookies.txt passed to every yt-dlp call (--cookies FILE), for
                      when YouTube asks to sign in and the browser's cookies can't be read

"translate" turns non-English speech into English text (Whisper's built-in translation).
"""

import json
import os
import sys
import threading
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

scripts = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(scripts))
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

import numpy as np  # noqa: E402
from sc import cli, discover, listen, media, produce, settings  # noqa: E402

SCAN_TASK = os.environ.get("SC_SCAN_TASK", "transcribe")
CAPTION_TASK = os.environ.get("SC_CAPTION_TASK", "transcribe")
OVERRIDES = json.loads(os.environ.get("SC_SETTINGS") or "{}")
COOKIES_FILE = os.environ.get("SC_COOKIES_FILE")

_state = threading.local()   # set while a clip is being made, so we know which task applies


def transcribe(model_name, samples, language=None, offset=0.0):
    """Same as sc.listen.transcribe, plus the Whisper task."""
    np.seterr(all="ignore")
    task = CAPTION_TASK if getattr(_state, "captioning", False) else SCAN_TASK
    segments, info = listen.whisper(model_name).transcribe(
        samples, language=language, task=task, word_timestamps=True, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        beam_size=1 if model_name == "tiny" else 4, condition_on_previous_text=False)
    words = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"s": round(w.start + offset, 2), "e": round(w.end + offset, 2),
                              "w": text})
    return words, info.language


_make = produce.make


def make(*args, **kwargs):
    _state.captioning = True
    try:
        return _make(*args, **kwargs)
    finally:
        _state.captioning = False


_load = settings.load


def load(cli_values=None):
    cfg = _load(cli_values)
    cfg.update(OVERRIDES)
    return cfg


_ytdlp_args = media.ytdlp_args


def ytdlp_args(url, cookies=None):
    args = _ytdlp_args(url, cookies)
    if COOKIES_FILE and "--cookies-from-browser" not in args:
        args += ["--cookies", COOKIES_FILE]
    return args


listen.transcribe = transcribe
produce.make = make
settings.load = load
media.ytdlp_args = ytdlp_args
discover.ytdlp_args = ytdlp_args   # discover imported the name directly

sys.exit(cli.main(sys.argv[2:]) or 0)
