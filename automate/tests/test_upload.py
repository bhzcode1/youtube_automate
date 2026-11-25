"""Unit tests for automate/upload.py and automate/run_daily.py (no network, no Google account).

    python -m unittest discover -s automate/tests -v
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_daily  # noqa: E402
import upload as up  # noqa: E402


class FakeHttpError(Exception):
    def __init__(self, status, content=b""):
        super().__init__(f"HTTP {status}: {content!r}")
        self.resp = SimpleNamespace(status=status)
        self.content = content


class FakeRequest:
    def __init__(self, steps):
        self.steps = list(steps)   # each: Exception to raise, or a response dict / None

    def next_chunk(self, num_retries=0):
        assert num_retries == up.MAX_RETRIES   # retries are left to the library
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return None, step


class FakeYouTube:
    def __init__(self, steps):
        self.steps, self.bodies = steps, []

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.bodies.append(body)
        return FakeRequest(self.steps)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name, file in {"QUEUE": "queue.json", "LOG": "run.log"}.items():
            p = mock.patch.object(up, name, self.tmp / file)
            p.start()
            self.addCleanup(p.stop)

    def clip(self, name):
        path = self.tmp / name
        path.write_bytes(b"\x00" * 16)
        return path.as_posix()

    def entry(self, name, score, status="approved"):
        return {"file": self.clip(name), "score": score, "status": status,
                "yt_title": f"{name} #Shorts", "yt_description": "d", "tags": ["shorts"]}

    def queue(self):
        return json.loads(up.QUEUE.read_text(encoding="utf-8"))


class Pending(Base):
    def test_highest_score_first_and_limit(self):
        q = [self.entry("a", 50), self.entry("b", 90), self.entry("c", 70),
             self.entry("d", 99, "uploaded")]
        self.assertEqual([Path(e["file"]).name for e in up.pending(q, 2)], ["b", "c"])
        self.assertEqual(up.pending(q, 0), [])

    def test_match_filter(self):
        q = [self.entry("mrbeast", 90), self.entry("kai_wrong_gun", 60)]
        self.assertEqual([Path(e["file"]).name for e in up.pending(q, 5, "KAI")], ["kai_wrong_gun"])
        self.assertEqual(up.pending(q, 5, "nobody"), [])

    def test_body(self):
        e = self.entry("a", 1)
        e["yt_title"] = "x" * 150
        body = up.body_for(e, "unlisted")
        self.assertEqual(len(body["snippet"]["title"]), 100)
        self.assertEqual(body["snippet"]["categoryId"], "20")
        self.assertEqual(body["status"], {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False})


class UploadOne(Base):
    media = staticmethod(lambda path: path)

    def test_success(self):
        yt = FakeYouTube([None, {"id": "VID1"}])   # two chunks
        self.assertEqual(up.upload_one(yt, self.entry("a", 1), "private", self.media), "VID1")
        self.assertEqual(yt.bodies[0]["status"]["privacyStatus"], "private")

    def test_permanent_error_raised(self):
        yt = FakeYouTube([FakeHttpError(400, b"invalidTitle")])
        with self.assertRaises(FakeHttpError):
            up.upload_one(yt, self.entry("a", 1), "private", self.media)

    def test_quota(self):
        yt = FakeYouTube([FakeHttpError(403, b'{"reason": "quotaExceeded"}')])
        with self.assertRaises(up.StopRun):
            up.upload_one(yt, self.entry("a", 1), "private", self.media)

    def test_offline_stops_run(self):
        class ServerNotFoundError(Exception):   # like httplib2's: not an OSError, no HTTP status
            pass
        yt = FakeYouTube([ServerNotFoundError("Unable to find the server at youtube.googleapis.com")])
        with self.assertRaises(up.StopRun):
            up.upload_one(yt, self.entry("a", 1), "private", self.media)

    def test_missing_file(self):
        e = self.entry("a", 1)
        Path(e["file"]).unlink()
        with self.assertRaises(FileNotFoundError):
            up.upload_one(FakeYouTube([]), e, "private", self.media)


class Run(Base):
    def write(self, entries):
        up.write_json(up.QUEUE, entries)

    def test_records_results_and_never_reuploads(self):
        self.write([self.entry("a", 90), self.entry("b", 80)])
        ids = iter(["V1", "V2"])
        uploader = mock.Mock(side_effect=lambda yt, e, p: next(ids))
        self.assertEqual(up.run(5, "private", client_factory=lambda: "yt", uploader=uploader), 0)
        q = self.queue()
        self.assertEqual([(e["status"], e["video_id"], e["privacy"]) for e in q],
                         [("uploaded", "V1", "private"), ("uploaded", "V2", "private")])
        self.assertTrue(all("uploaded_at" in e for e in q))
        # second run: nothing left, and no sign-in attempted
        client = mock.Mock()
        self.assertEqual(up.run(5, "private", client_factory=client, uploader=uploader), 0)
        client.assert_not_called()
        self.assertEqual(uploader.call_count, 2)

    def test_failure_recorded_and_next_continues(self):
        self.write([self.entry("a", 90), self.entry("b", 80)])

        def uploader(yt, e, p):
            if e["file"].endswith("/a"):
                raise RuntimeError("boom")
            return "V2"
        up.run(5, "private", client_factory=lambda: "yt", uploader=uploader)
        q = self.queue()
        self.assertEqual((q[0]["status"], q[0]["error"]), ("failed", "boom"))
        self.assertEqual((q[1]["status"], q[1]["video_id"]), ("uploaded", "V2"))

    def test_quota_stops_and_keeps_rest_approved(self):
        self.write([self.entry("a", 90), self.entry("b", 80)])
        uploader = mock.Mock(side_effect=up.StopRun("quotaExceeded"))
        up.run(5, "private", client_factory=lambda: "yt", uploader=uploader)
        self.assertEqual(uploader.call_count, 1)
        self.assertEqual([e["status"] for e in self.queue()], ["approved", "approved"])

    def test_entries_added_during_upload_survive(self):
        self.write([self.entry("a", 90)])
        late = self.entry("late", 70)

        def uploader(yt, e, p):   # make_clips appends while the upload runs
            up.write_json(up.QUEUE, self.queue() + [late])
            return "V1"
        up.run(5, "private", client_factory=lambda: "yt", uploader=uploader)
        self.assertEqual([e["status"] for e in self.queue()], ["uploaded", "approved"])

    def test_sign_in_has_timeout(self):
        with mock.patch.object(up, "TOKEN", self.tmp / "token.json"), \
                mock.patch.object(up, "CLIENT_SECRET", Path(self.clip("client_secret.json"))), \
                mock.patch("google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file") as flow, \
                mock.patch("googleapiclient.discovery.build"):
            flow.return_value.run_local_server.return_value.to_json.return_value = "{}"
            up.youtube_client()
        self.assertEqual(flow.return_value.run_local_server.call_args.kwargs["timeout_seconds"], 300)

    def test_max_uploads_respected(self):
        self.write([self.entry(n, s) for n, s in (("a", 1), ("b", 2), ("c", 3))])
        uploader = mock.Mock(return_value="V")
        up.run(2, "private", client_factory=lambda: "yt", uploader=uploader)
        self.assertEqual([e["status"] for e in self.queue()], ["approved", "uploaded", "uploaded"])

    def test_dry_run_touches_nothing(self):
        self.write([self.entry("a", 90)])
        client = mock.Mock()
        self.assertEqual(up.run(5, "private", dry_run=True, client_factory=client), 0)
        client.assert_not_called()
        self.assertEqual(self.queue()[0]["status"], "approved")

    def test_empty_or_missing_queue(self):
        client = mock.Mock()
        self.assertEqual(up.run(5, "private", client_factory=client), 0)
        client.assert_not_called()


class RunDaily(unittest.TestCase):
    def opts(self, **kw):
        base = dict(topic='GTA 6', keywords="gta 6,gta vi", streams=2, clips=3, max_uploads=5,
                    privacy="private", install="18:00", sources=None)
        return SimpleNamespace(**{**base, **kw})

    def test_steps(self):
        make, upload = run_daily.step_commands(self.opts())
        self.assertEqual(Path(make[1]).name, "make_clips.py")
        self.assertIn("GTA 6", make)
        self.assertEqual(upload[-4:], ["--max-uploads", "5", "--privacy", "private"])

    def test_sources_passed_through(self):
        make, _ = run_daily.step_commands(self.opts(sources="twitch"))
        self.assertEqual(make[-2:], ["--sources", "twitch"])
        self.assertIn("--sources twitch", run_daily.task_script(self.opts(sources="twitch")))
        make, _ = run_daily.step_commands(self.opts())
        self.assertNotIn("--sources", make)

    def test_task_command_short_and_points_at_script(self):
        cmd = run_daily.task_command(self.opts())
        self.assertEqual(cmd[:8], ["schtasks", "/Create", "/SC", "DAILY", "/ST", "18:00", "/TN",
                                   "youtube_automate"])
        self.assertEqual(cmd[9].strip('"'), str(run_daily.TASK_CMD))
        self.assertLessEqual(len(cmd[9]), run_daily.TR_LIMIT)

    def test_task_script_long_topic_quoted_and_logged(self):
        script = run_daily.task_script(self.opts(topic="GTA 6 " + "x" * 300, keywords="100% gta"))
        self.assertIn('"GTA 6 ' + "x" * 300 + '"', script)
        self.assertIn("100%% gta", script)              # % escaped for cmd
        self.assertIn("run_daily.py", script)
        self.assertIn(f'>> "{run_daily.TASK_OUTPUT}" 2>&1', script)
        self.assertNotIn(str(run_daily.LOG), script)   # run.log stays writable by the scripts

    def test_main_runs_both_steps_and_uploads_even_if_nothing_new(self):
        with mock.patch.object(run_daily.subprocess, "call", side_effect=[1, 0]) as call, \
                mock.patch.object(run_daily, "LOG", Path(tempfile.mkdtemp()) / "run.log"):
            self.assertEqual(run_daily.main(["--topic", "chess"]), 0)
        self.assertEqual(call.call_count, 2)


if __name__ == "__main__":
    unittest.main()
