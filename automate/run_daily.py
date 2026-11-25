#!/usr/bin/env python3
"""One scheduled run: make clips (Person A), then upload approved ones (Person B).

    python automate/run_daily.py --topic "GTA 6" [--keywords "gta 6,gta vi"] [--streams 2]
                                 [--clips 3] [--max-uploads 5] [--privacy private]

    python automate/run_daily.py --topic "GTA 6" --install 18:00          # daily task
    python automate/run_daily.py --topic "GTA 6" --install 18:00 --print  # show, don't create

Both steps log to automate/run.log. Upload runs even when no new clip was made, so a
backlog of approved clips still goes up. Exit code: the upload step's only (make_clips
exits 1 on a normal "no clips made" day); see run.log for make_clips errors.
"""

import argparse
import datetime
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "run.log"
TASK_NAME = "youtube_automate"


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} [run_daily] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def step_commands(opts):
    py = sys.executable
    make = [py, str(HERE / "make_clips.py"), "--topic", opts.topic,
            "--streams", str(opts.streams), "--clips", str(opts.clips)]
    if opts.keywords:
        make += ["--keywords", opts.keywords]
    if opts.sources:
        make += ["--sources", opts.sources]
    upload = [py, str(HERE / "upload.py"), "--max-uploads", str(opts.max_uploads)]
    if opts.privacy:   # unset: upload.py picks (public via Upload-Post, private via own API)
        upload += ["--privacy", opts.privacy]
    return make, upload


TASK_CMD = HERE / "scheduled_task.cmd"
# Console output of a scheduled run. Must NOT be run.log: cmd's ">>" keeps that file open and
# Windows then refuses the scripts' own appends to it (PermissionError).
TASK_OUTPUT = HERE / "task_output.log"
TR_LIMIT = 261   # schtasks rejects a /TR longer than this


def task_script(opts):
    """Contents of the .cmd the task runs: the full command lives here, not in /TR."""
    args = [sys.executable, str(Path(__file__).resolve()), "--topic", opts.topic,
            "--streams", str(opts.streams), "--clips", str(opts.clips),
            "--max-uploads", str(opts.max_uploads)]
    if opts.privacy:
        args += ["--privacy", opts.privacy]
    if opts.keywords:
        args += ["--keywords", opts.keywords]
    if opts.sources:
        args += ["--sources", opts.sources]
    line =subprocess.list2cmdline(args).replace("%", "%%")   # % is special in .cmd files
    return ("@echo off\r\nchcp 65001 >nul\r\nset PYTHONUTF8=1\r\n"
            f"{line} >> \"{TASK_OUTPUT}\" 2>&1\r\n")


def task_command(opts):
    """schtasks arguments that run TASK_CMD daily at opts.install."""
    tr = f'"{TASK_CMD}"' if " " in str(TASK_CMD) else str(TASK_CMD)
    if len(tr) > TR_LIMIT:
        raise SystemExit(f"task path too long for schtasks ({len(tr)} > {TR_LIMIT}): {TASK_CMD}")
    return ["schtasks", "/Create", "/SC", "DAILY", "/ST", opts.install, "/TN", TASK_NAME,
            "/TR", tr, "/F"]


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--keywords")
    ap.add_argument("--streams", type=int, default=2)
    ap.add_argument("--clips", type=int, default=3)
    ap.add_argument("--sources", help="only clip from these hosts, e.g. twitch,kick")
    ap.add_argument("--max-uploads", type=int, default=5)
    ap.add_argument("--privacy", choices=["private", "unlisted", "public"],
                    help="default: upload.py's (public via Upload-Post, private via own API)")
    ap.add_argument("--install", metavar="HH:MM", help="create the daily Windows scheduled task")
    ap.add_argument("--print", action="store_true", help="with --install: only show the command")
    opts = ap.parse_args(argv)

    if opts.install:
        script, cmd = task_script(opts), task_command(opts)
        print(f"{TASK_CMD}:\n{script}\n{subprocess.list2cmdline(cmd)}")
        if opts.print:
            return 0
        if os.name != "nt":
            print("--install uses Windows Task Scheduler; on macOS/Linux add a cron entry instead.")
            return 1
        TASK_CMD.write_text(script, encoding="utf-8")
        return subprocess.call(cmd)

    make, upload = step_commands(opts)
    log(f"start topic={opts.topic!r}")
    made = subprocess.call(make)
    log(f"make_clips exited {made}")   # 1 can just mean "nothing new to clip today"
    sent = subprocess.call(upload)
    log(f"upload exited {sent}")
    return 0 if sent == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
