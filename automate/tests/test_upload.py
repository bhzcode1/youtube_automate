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


class ServerNotFoundError(Exception):
    """Like httplib2's DNS error: not an OSError, no HTTP status."""


ServerNotFoundError.__module__ = "httplib2.error"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # no uploadpost_key.txt -> the own-API ("youtube") path, which the tests fake
        for name, file in {"QUEUE": "queue.json", "LOG": "run.log", "UP_KEY": "no_key.txt",
                           "TOKEN": "token.json", "CLIENT_SECRET": "client_secret.json"}.items():
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

    def write(self, entries):
        up.save_queue(entries)

    def queue(self):
        return json.loads(up.QUEUE.read_text(encoding="utf-8"))

    def run_main(self, uploader, *argv):
        """Run upload.main() with the YouTube client and upload_one faked."""
        with mock.patch.object(up, "get_youtube", return_value="yt") as client, \
                mock.patch.object(up, "upload_one", side_effect=uploader) as one:
            up.main(list(argv))
        return client, one


class UploadOne(Base):
    def test_library_does_the_retries(self):
        request = mock.Mock()
        request.next_chunk.side_effect = [(None, None), (None, {"id": "VID1"})]   # two chunks
        yt = mock.Mock()
        yt.videos.return_value.insert.return_value = request
        with mock.patch("googleapiclient.http.MediaFileUpload"):
            self.assertEqual(up.upload_one(yt, self.entry("a", 1), "private", "20"), "VID1")
        request.next_chunk.assert_called_with(num_retries=up.MAX_RETRIES)
        body = yt.videos.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(body["status"]["privacyStatus"], "private")

    def test_clean_title(self):
        self.assertEqual(up.clean_title("x" * 150), "x" * 100)
        self.assertEqual(up.clean_title("<b>Hi</b>"), "bHi/b #Shorts")

    def test_find_youtube_result_nested(self):
        self.assertEqual(up.find_youtube_result({"results": [{"youtube": {"success": True}}]}),
                         {"success": True})


class Run(Base):
    def test_records_results_and_never_reuploads(self):
        self.write([self.entry("a", 90), self.entry("b", 80)])
        ids = iter(["V1", "V2"])
        self.run_main(lambda yt, e, p, c: next(ids))
        q = self.queue()
        self.assertEqual([(e["status"], e["video_id"], e["privacy"]) for e in q],
                         [("uploaded", "V1", "private"), ("uploaded", "V2", "private")])
        client, one = self.run_main(lambda *a: "V3")   # second run: nothing left, no sign-in
        client.assert_not_called()
        one.assert_not_called()

    def test_failure_recorded_and_next_continues(self):
        self.write([self.entry("a", 90), self.entry("b", 80)])

        def uploader(yt, e, p, c):
            if e["file"].endswith("/a"):
                raise FakeHttpError(400, b"invalidTitle")
            return "V2"
        self.run_main(uploader)
        q = self.queue()
        self.assertEqual(q[0]["status"], "failed")
        self.assertIn("invalidTitle", q[0]["error"])
        self.assertEqual((q[1]["status"], q[1]["video_id"]), ("uploaded", "V2"))

    def test_quota_and_offline_stop_and_keep_rest_approved(self):
        for exc in (FakeHttpError(403, b'{"reason": "quotaExceeded"}'),
                    ServerNotFoundError("Unable to find the server"),
                    ConnectionError("network down")):
            with self.subTest(exc=exc):
                self.write([self.entry("a", 90), self.entry("b", 80)])
                _, one = self.run_main(exc)
                self.assertEqual(one.call_count, 1)
                self.assertEqual([e["status"] for e in self.queue()], ["approved", "approved"])
                up.QUEUE.unlink()

    def test_entries_added_during_upload_survive(self):
        self.write([self.entry("a", 90)])
        late = self.entry("late", 70)

        def uploader(yt, e, p, c):   # make_clips appends while the upload runs
            up.QUEUE.write_text(json.dumps(self.queue() + [late]), encoding="utf-8")
            return "V1"
        self.run_main(uploader)
        self.assertEqual([e["status"] for e in self.queue()], ["uploaded", "approved"])

    def test_max_uploads_respected(self):
        self.write([self.entry(n, s) for n, s in (("a", 1), ("b", 2), ("c", 3))])
        self.run_main(lambda *a: "V", "--max-uploads", "2")
        self.assertEqual([e["status"] for e in self.queue()], ["approved", "uploaded", "uploaded"])

    def test_match_uploads_only_that_clip(self):
        self.write([self.entry("old_high", 90), self.entry("new_kai", 60)])
        self.run_main(lambda *a: "V", "--max-uploads", "1", "--match", "NEW_KAI")
        self.assertEqual([e["status"] for e in self.queue()], ["approved", "uploaded"])

    def test_dry_run_touches_nothing(self):
        self.write([self.entry("a", 90)])
        client, one = self.run_main(lambda *a: "V", "--dry-run")
        client.assert_not_called()
        self.assertEqual(self.queue()[0]["status"], "approved")

    def test_missing_file_marked_failed(self):
        e = self.entry("a", 1)
        Path(e["file"]).unlink()
        self.write([e])
        _, one = self.run_main(lambda *a: "V")
        one.assert_not_called()
        self.assertEqual(self.queue()[0]["status"], "failed")


class UploadPost(Base):
    def test_status_check_network_error_keeps_waiting(self):
        accepted = mock.Mock(status_code=200, json=lambda: {"success": True, "request_id": "r1"})
        done = mock.Mock(json=lambda: {"results": {"youtube": {"success": True, "video_id": "VID"}}})
        with mock.patch("requests.post", return_value=accepted), \
                mock.patch("requests.get", side_effect=[ConnectionError("wifi"), done]), \
                mock.patch.object(up.time, "sleep"):
            self.assertEqual(up.upload_via_uploadpost(self.entry("a", 1), "public", "24", "k", "p"),
                             ("VID", "r1"))

    def test_ai_title_failure_does_not_block_the_queue(self):
        up.UP_KEY.write_text("key", encoding="utf-8")   # -> Upload-Post path
        added = {**self.entry("added", 1000), "yt_title": ""}
        self.write([added, self.entry("b", 80)])
        with mock.patch.object(up, "auto_title_description", side_effect=RuntimeError("no AI left")), \
                mock.patch.object(up, "upload_via_uploadpost", return_value=("V", "r")) as send:
            up.main(["--max-uploads", "1", "--profile", "p"])
            send.assert_not_called()
            self.assertEqual(self.queue()[0]["status"], "failed")
            up.main(["--max-uploads", "1", "--profile", "p"])   # next run moves on to b
        self.assertEqual([e["status"] for e in self.queue()], ["failed", "uploaded"])


class SignIn(Base):
    def test_browser_sign_in_has_timeout(self):
        self.clip("client_secret.json")
        with mock.patch("google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file") as flow, \
                mock.patch("googleapiclient.discovery.build"), \
                mock.patch.object(up.sys, "stdin", mock.Mock(isatty=lambda: True)):
            flow.return_value.run_local_server.return_value.to_json.return_value = "{}"
            up.get_youtube()
        self.assertEqual(flow.return_value.run_local_server.call_args.kwargs["timeout_seconds"], 300)

    def test_unattended_exits_instead_of_opening_browser(self):
        self.clip("client_secret.json")
        with mock.patch.object(up.sys, "stdin", None), self.assertRaises(SystemExit) as cm:
            up.get_youtube()
        self.assertEqual(cm.exception.code, 3)


class RunDaily(unittest.TestCase):
    def opts(self, **kw):
        base = dict(topic='GTA 6', keywords="gta 6,gta vi", streams=2, clips=3, max_uploads=5,
                    privacy=None, install="18:00", sources=None)
        return SimpleNamespace(**{**base, **kw})

    def test_steps(self):
        make, upload = run_daily.step_commands(self.opts())
        self.assertEqual(Path(make[1]).name, "make_clips.py")
        self.assertIn("GTA 6", make)
        self.assertEqual(upload[-2:], ["--max-uploads", "5"])   # upload.py picks the privacy
        _, upload = run_daily.step_commands(self.opts(privacy="unlisted"))
        self.assertEqual(upload[-2:], ["--privacy", "unlisted"])

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
        self.assertNotIn("--privacy", script)
        self.assertIn(f'>> "{run_daily.TASK_OUTPUT}" 2>&1', script)
        self.assertNotIn(str(run_daily.LOG), script)   # run.log stays writable by the scripts

    def test_main_runs_both_steps_and_uploads_even_if_nothing_new(self):
        with mock.patch.object(run_daily.subprocess, "call", side_effect=[1, 0]) as call, \
                mock.patch.object(run_daily, "LOG", Path(tempfile.mkdtemp()) / "run.log"):
            self.assertEqual(run_daily.main(["--topic", "chess"]), 0)
        self.assertEqual(call.call_count, 2)


if __name__ == "__main__":
    unittest.main()
