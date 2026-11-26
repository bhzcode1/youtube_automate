"""Unit tests for automate/make_clips.py. Standard library only.

    python -m unittest discover -s automate/tests -v
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import make_clips as mc  # noqa: E402

FIND_OUTPUT = """
  1. Techno Gamerz             35,762,672 views  2h07m  MY FIRST DAY IN VICE CITY | GTA 6 GAMEPLAY #1
     https://www.youtube.com/watch?v=169ZzvPxvvM   (youtube search)
  2. MrBeast Gaming            72,048,019 views  0h30m  I Survived 100 Hours In GTA 6
     https://www.youtube.com/watch?v=ymCHX_Dcc-U   (youtube search)
  10. IGN                               ? views  0h27m  GTA 6 Review
     https://www.youtube.com/watch?v=agBEh5aZ_Go   (youtube search)
"""


class TempFiles(unittest.TestCase):
    """Points every state file of make_clips at a fresh temp folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patches = {"QUEUE": "queue.json", "REJECTED": "rejected.json", "LOG": "run.log",
                   "CREATORS": "creators.json", "SETTINGS": "settings.json"}
        for name, file in patches.items():
            p = mock.patch.object(mc, name, self.tmp / file)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)


class FirstJson(unittest.TestCase):
    def test_array_after_prose(self):
        self.assertEqual(mc.first_json("Sure! The best are [3, 1].", "["), [3, 1])

    def test_object_with_nested_braces(self):
        text = 'verdict: {"ok": true, "x": {"y": [1, {"z": 2}]}} trailing'
        self.assertEqual(mc.first_json(text, "{"), {"ok": True, "x": {"y": [1, {"z": 2}]}})

    def test_skips_invalid_block_then_finds_valid(self):
        self.assertEqual(mc.first_json("[not json] then [2]", "["), [2])

    def test_none_and_missing(self):
        self.assertIsNone(mc.first_json(None, "["))
        self.assertIsNone(mc.first_json("no json here", "{"))


class FindStreams(TempFiles):
    def test_parses_print_found_output(self):
        fake = SimpleNamespace(stdout=FIND_OUTPUT, stderr="")
        with mock.patch.object(mc.subprocess, "run", return_value=fake):
            found = mc.find_streams(Path("streamclips.py"), "GTA 6", None, 10)
        self.assertEqual([s["url"][-11:] for s in found], ["169ZzvPxvvM", "ymCHX_Dcc-U", "agBEh5aZ_Go"])
        self.assertEqual(found[0]["name"], "Techno Gamerz")
        self.assertEqual(found[0]["views"], 35762672)
        self.assertEqual(found[2]["views"], 0)          # "?" views
        self.assertEqual(found[2]["name"], "IGN")       # two-digit index


class FilterSources(TempFiles):
    found = [{"url": "https://www.youtube.com/watch?v=a"}, {"url": "https://youtu.be/b"},
             {"url": "https://www.twitch.tv/videos/1"}, {"url": "https://kick.com/x/videos/2"}]

    def test_filters(self):
        urls = lambda fs: [s["url"][-1] for s in fs]   # noqa: E731
        self.assertEqual(urls(mc.filter_sources(self.found, ["twitch"])), ["1"])
        self.assertEqual(urls(mc.filter_sources(self.found, ["twitch", "kick"])), ["1", "2"])
        self.assertEqual(urls(mc.filter_sources(self.found, ["youtube"])), ["a", "b"])
        self.assertEqual(mc.filter_sources(self.found, None), self.found)


class ChooseStreams(TempFiles):
    found = [{"line": "a", "url": "u1", "views": 10}, {"line": "b", "url": "u2", "views": 99},
             {"line": "c", "url": "u3", "views": 50}]

    def test_uses_claude_choice(self):
        with mock.patch.object(mc, "claude", return_value="I pick [3]"):
            self.assertEqual([s["url"] for s in mc.choose_streams(self.found, "t", 2, "m", set())], ["u3"])

    def test_falls_back_to_views(self):
        with mock.patch.object(mc, "claude", return_value="sorry, no idea"):
            self.assertEqual([s["url"] for s in mc.choose_streams(self.found, "t", 2, "m", set())],
                             ["u2", "u3"])

    def test_skips_done_and_ignores_out_of_range(self):
        with mock.patch.object(mc, "claude", return_value="[9, 1, 2]") as ask:
            chosen = mc.choose_streams(self.found, "t", 5, "m", {"u2"})
        self.assertNotIn("u2", ask.call_args[0][0])     # done stream never shown to Claude
        self.assertEqual([s["url"] for s in chosen], ["u1", "u3"])

    def test_nothing_fresh(self):
        with mock.patch.object(mc, "claude") as ask:
            self.assertEqual(mc.choose_streams(self.found, "t", 2, "m", {"u1", "u2", "u3"}), [])
        ask.assert_not_called()


class YoutubeFields(unittest.TestCase):
    source = {"name": "CarryisLive", "url": "https://youtu.be/x"}

    def test_title_desc_tags(self):
        title, desc, tags = mc.youtube_fields(
            {"yt_title": "Big moment #shorts", "yt_description": "He loses it."},
            {"hashtags": ["#GTA6", "#Gaming", "#"]}, self.source)
        self.assertEqual(title, "Big moment #Shorts")
        self.assertTrue(desc.startswith("He loses it."))
        self.assertTrue(desc.endswith("Credit: CarryisLive https://youtu.be/x"))
        self.assertEqual(tags, ["GTA6", "Gaming", "shorts"])

    def test_long_title_capped_at_100(self):
        title, _, _ = mc.youtube_fields({"yt_title": "x" * 300}, {}, self.source)
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title.endswith(" #Shorts"))

    def test_fallbacks(self):
        title, desc, tags = mc.youtube_fields({}, {"title": "Clip title", "caption": "cap"}, {})
        self.assertEqual(title, "Clip title #Shorts")
        self.assertIn("Credit: the original creator", desc)
        self.assertEqual(tags, ["shorts"])


class StateFiles(TempFiles):
    def test_done_urls_reads_both_files(self):
        mc.write_json(mc.QUEUE, [{"source_url": "a "}, {"file": "no url"}])
        mc.write_json(mc.REJECTED, [{"source_url": "b"}])
        self.assertEqual(mc.done_urls(), {"a", "b"})

    def test_done_urls_without_files(self):
        self.assertEqual(mc.done_urls(), set())

    def test_min_score_raised_and_other_keys_kept(self):
        mc.write_json(mc.SETTINGS, {"scan_model": "base", "min_score": 40})
        mc.ensure_min_score()
        self.assertEqual(mc.read_json(mc.SETTINGS, {}), {"scan_model": "base", "min_score": mc.MIN_SCORE})

    def test_min_score_higher_value_kept(self):
        mc.write_json(mc.SETTINGS, {"min_score": 75})
        mc.ensure_min_score()
        self.assertEqual(mc.read_json(mc.SETTINGS, {})["min_score"], 75)

    def test_min_score_exact(self):
        mc.write_json(mc.SETTINGS, {"min_score": 75})
        mc.ensure_min_score(40)
        self.assertEqual(mc.read_json(mc.SETTINGS, {})["min_score"], 40)

    def test_min_score_creates_missing_file(self):
        mc.ensure_min_score()
        self.assertEqual(mc.read_json(mc.SETTINGS, {}), {"min_score": mc.MIN_SCORE})


class ReviewFolders(TempFiles):
    def setUp(self):
        super().setUp()
        self.folder = self.tmp / "stream"
        self.folder.mkdir()
        mc.write_json(self.folder / "clips.json", {
            "source": {"name": "Creator", "url": "https://youtu.be/s"},
            "clips": [{"file": "01_a.mp4", "score": 80, "title": "A", "hashtags": ["#GTA6"]},
                      {"file": "02_b.mp4", "score": 70, "title": "B"}]})

    def verdicts(self, entry, *_):
        if entry["file"] == "01_a.mp4":
            return {"ok": True, "seen": "s", "yt_title": "Good one", "yt_description": "D"}
        return {"ok": False, "reason": "cutscene"}

    def test_routes_to_queue_and_rejected(self):
        with mock.patch.object(mc, "review", side_effect=self.verdicts):
            self.assertEqual(mc.review_folders([self.folder], "t", "m"), 1)
        queue, rejected = mc.read_json(mc.QUEUE, []), mc.read_json(mc.REJECTED, [])
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["status"], "approved")
        self.assertEqual(queue[0]["yt_title"], "Good one #Shorts")
        self.assertEqual(queue[0]["source_url"], "https://youtu.be/s")
        self.assertTrue(queue[0]["file"].endswith("stream/01_a.mp4"))
        self.assertEqual([r["reason"] for r in rejected], ["cutscene"])

    def test_upload_during_review_is_not_undone(self):
        mc.write_json(mc.QUEUE, [{"file": "C:/old.mp4", "status": "approved"}])

        def upload_meanwhile(entry, *_):   # upload.py finishes while Claude reviews clip 1
            if entry["file"] == "01_a.mp4":
                mc.write_json(mc.QUEUE, [{"file": "C:/old.mp4", "status": "uploaded"}])
            return self.verdicts(entry)
        with mock.patch.object(mc, "review", side_effect=upload_meanwhile):
            mc.review_folders([self.folder], "t", "m")
        queue = mc.read_json(mc.QUEUE, [])
        self.assertEqual([e["status"] for e in queue], ["uploaded", "approved"])

    def test_second_run_reviews_nothing(self):
        with mock.patch.object(mc, "review", side_effect=self.verdicts):
            mc.review_folders([self.folder], "t", "m")
        with mock.patch.object(mc, "review") as again:
            self.assertEqual(mc.review_folders([self.folder], "t", "m"), 0)
        again.assert_not_called()


class FreshFolders(TempFiles):
    def test_old_clips_json_ignored(self):
        old, new, empty = self.tmp / "old", self.tmp / "new", self.tmp / "empty"
        for d in (old, new, empty):
            d.mkdir()
        (old / "clips.json").write_text("{}")
        import os
        os.utime(old / "clips.json", (1000, 1000))     # written long before the run
        (new / "clips.json").write_text("{}")
        self.assertEqual(mc.fresh_folders([old, new, empty], since=2000), [new])


class RetryLater(unittest.TestCase):
    """Real stream-clips lines from 2026-11-26 (Live Insaan runs)."""

    def test_download_and_login_failures_are_retryable(self):
        for line in (
                "[01:15] x Live Insaan: https://www.youtube.com/watch?v=jR_nyfGVcpc: yt-dlp failed: "
                "ERROR: [youtube] jR_nyfGVcpc: Unable to download API page: ... getaddrinfo failed",
                "[55:50] x Live Insaan: clip 1 failed: YouTube needs a login for this video "
                "(age-restricted). Run again with --cookies chrome",
                "[00:58] x Live Insaan: clip 1 failed: yt-dlp failed: ERROR: Could not copy Chrome "
                "cookie database.",
                "[00:05] x Live Insaan: clip 1 failed: yt-dlp failed: ERROR: Failed to decrypt with DPAPI."):
            self.assertTrue(mc.retry_later(line), line)

    def test_warnings_and_real_no_clip_results_are_not(self):
        for line in (
                "[50:55] ! Live Insaan: chat replay skipped (yt-dlp failed: ERROR: [youtube] "
                "jR_nyfGVcpc: Sign in to confirm you're not a bot)",
                "[10:00] x Live Insaan: https://youtu.be/x: the AI found nothing strong enough to clip",
                "[00:41] > Live Insaan: listening to 81 min of audio"):
            self.assertFalse(mc.retry_later(line), line)


class MakeClipsQuality(TempFiles):
    def test_quality_settings_reach_stream_clips(self):
        proc = mock.Mock(stdout=iter([]), returncode=0)
        with mock.patch.object(mc.subprocess, "Popen", return_value=proc) as popen:
            mc.make_clips(Path("sc/streamclips.py"), ["https://youtu.be/x"], "t", None, 1,
                          "m", "hi")
        env = popen.call_args.kwargs["env"]
        self.assertEqual(json.loads(env["SC_SETTINGS"])["video_height"], 2160)
        self.assertEqual(env["SC_CAPTION_TASK"], "translate")


class SpeechLanguages(TempFiles):
    def test_known_creator_case_insensitive_no_claude(self):
        mc.write_json(mc.CREATORS, {"Techno Gamerz": "hi"})
        with mock.patch.object(mc, "claude") as ask:
            langs = mc.speech_languages([{"url": "u", "name": "techno gamerz"}], "m")
        self.assertEqual(langs, {"u": "hi"})
        ask.assert_not_called()

    def test_unknown_creator_guessed_and_saved(self):
        mc.write_json(mc.CREATORS, {"Techno Gamerz": "hi"})
        streams = [{"url": "u1", "name": "Mythpat"}, {"url": "u2", "name": "theRadBrad"}]
        with mock.patch.object(mc, "claude", return_value='{"1": "hi", "2": "en"}'):
            langs = mc.speech_languages(streams, "m")
        self.assertEqual(langs, {"u1": "hi", "u2": "en"})
        self.assertEqual(mc.read_json(mc.CREATORS, {}),
                         {"Techno Gamerz": "hi", "Mythpat": "hi", "theRadBrad": "en"})

    def test_bad_reply_defaults_to_english_but_is_not_saved(self):
        with mock.patch.object(mc, "claude", return_value=None):
            self.assertEqual(mc.speech_languages([{"url": "u", "name": "X"}], "m"), {"u": "en"})
        self.assertNotIn("X", mc.read_json(mc.CREATORS, {}))

    def test_partial_reply_saves_only_answered(self):
        streams = [{"url": "u1", "name": "A"}, {"url": "u2", "name": "B"}]
        with mock.patch.object(mc, "claude", return_value='{"2": "hi"}'):
            self.assertEqual(mc.speech_languages(streams, "m"), {"u1": "en", "u2": "hi"})
        self.assertEqual(mc.read_json(mc.CREATORS, {}), {"B": "hi"})


class Review(TempFiles):
    def test_low_sync_rejected_without_claude(self):
        with mock.patch.object(mc, "claude") as ask:
            v = mc.review({"file": "a.mp4", "sync": 0.3}, {}, self.tmp, "t", "m")
        self.assertFalse(v["ok"])
        self.assertIn("sync", v["reason"])
        ask.assert_not_called()

    def test_missing_file_rejected(self):
        v = mc.review({"file": "missing.mp4", "sync": 0.9}, {}, self.tmp, "t", "m")
        self.assertEqual(v, {"ok": False, "reason": "clip file missing"})

    def test_unparseable_verdict_rejected(self):
        (self.tmp / "a.mp4").write_bytes(b"not a video")
        with mock.patch.object(mc, "contact_sheet", return_value=False), \
                mock.patch.object(mc, "claude", return_value="looks fine to me"):
            v = mc.review({"file": "a.mp4", "sync": 0.9}, {}, self.tmp, "t", "m")
        self.assertFalse(v["ok"])

    def test_verdict_passed_through_and_read_tool_used(self):
        (self.tmp / "a.mp4").write_bytes(b"x")
        reply = 'ok: {"seen": "s", "ok": true, "reason": "r", "yt_title": "T", "yt_description": "D"}'
        with mock.patch.object(mc, "contact_sheet", return_value=True), \
                mock.patch.object(mc, "claude", return_value=reply) as ask:
            v = mc.review({"file": "a.mp4", "sync": 0.9}, {}, self.tmp, "t", "m")
        self.assertTrue(v["ok"])
        self.assertEqual(ask.call_args.kwargs["tools"], "Read")
        self.assertIn("checks/a_review.jpg", ask.call_args[0][0])


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class ContactSheet(TempFiles):
    def test_builds_4x2_sheet(self):
        clip = self.tmp / "clip.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc=size=1080x1920:rate=30:duration=4", "-pix_fmt", "yuv420p",
                        str(clip)], check=True)
        out = self.tmp / "sheet.jpg"
        self.assertTrue(mc.contact_sheet(clip, 4, out))
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
                                "-of", "csv=p=0", str(out)], capture_output=True, text=True)
        self.assertEqual(probe.stdout.strip(), "1440,1280")   # 4x2 tiles of 360x640

    def test_bad_input_returns_false(self):
        bad = self.tmp / "bad.mp4"
        bad.write_bytes(b"garbage")
        self.assertFalse(mc.contact_sheet(bad, 4, self.tmp / "o.jpg"))


if __name__ == "__main__":
    unittest.main()
