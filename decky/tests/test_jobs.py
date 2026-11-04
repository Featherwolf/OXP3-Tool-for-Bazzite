import json
import os
import tempfile
import threading
import time
import unittest

from oxp3deck import jobs
from oxp3deck.jobs import Jobs


def wait_done(job, timeout=10):
    end = time.monotonic() + timeout
    while job.state == "running" and time.monotonic() < end:
        time.sleep(0.01)
    return job.state


def wait_idle(js, timeout=10):
    end = time.monotonic() + timeout
    while js.running() is not None and time.monotonic() < end:
        time.sleep(0.01)


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.js = Jobs(self.tmp.name)

    def saved(self, job):
        with open(os.path.join(self.tmp.name, "jobs", job.id + ".json")) as f:
            return json.load(f)

    def test_done(self):
        def fn(job):
            job.say("step 1")
            return {"ok": True, "error": None, "version": "0.4.0"}

        job, err = self.js.start("oxp3_install", "Install oxp3-tool", fn)
        self.assertIsNone(err)
        wait_idle(self.js)
        self.assertEqual(job.state, "done")
        self.assertIsNone(job.error)
        self.assertEqual(job.result["version"], "0.4.0")
        self.assertIsNotNone(job.ended)
        self.assertIn("step 1", job.log)
        d = self.saved(job)
        self.assertEqual((d["state"], d["title"], d["log"]), ("done", "Install oxp3-tool", ["step 1"]))

    def test_one_at_a_time(self):
        go = threading.Event()
        first, _ = self.js.start("xess_install", "Install XeSS", lambda job: {"ok": go.wait(10)})
        self.assertEqual(self.js.running(), first)
        self.assertEqual(self.js.get(None), first)
        second, err = self.js.start("oxp3_apply", "Apply fixes", lambda job: {"ok": True})
        self.assertIsNone(second)
        self.assertEqual(err, "another task is running: Install XeSS")
        self.assertEqual(self.saved(first)["state"], "running")     # saved at the start
        go.set()
        wait_idle(self.js)
        self.assertEqual(first.state, "done")
        third, err = self.js.start("oxp3_apply", "Apply fixes", lambda job: {"ok": True})
        self.assertIsNone(err)
        wait_idle(self.js)
        self.assertEqual(self.js.get(None), third)       # nothing running: the newest
        self.assertEqual(self.js.get(first.id), first)
        self.assertIsNone(self.js.get("nope"))
        self.assertEqual(self.js.recent(), [third, first])
        self.assertEqual(self.js.recent(1), [third])

    def test_exception(self):
        def fn(job):
            raise ValueError("boom")

        with self.assertLogs("oxp3deck", "INFO"):
            job, _ = self.js.start("diagnose", "Diagnose", fn)
            wait_idle(self.js)
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error, "diagnose: ValueError: boom")
        self.assertIsNone(job.result)
        self.assertTrue(any(line.startswith("Traceback") for line in job.log))
        self.assertEqual(self.saved(job)["error"], "diagnose: ValueError: boom")
        self.assertIsNone(self.js.running())

    def test_failed_result(self):
        job, _ = self.js.start("oxp3_apply", "Apply", lambda job: {"ok": False, "error": "apply: limine failed"})
        wait_idle(self.js)
        self.assertEqual((job.state, job.error), ("failed", "apply: limine failed"))
        job, _ = self.js.start("oxp3_apply", "Apply", lambda job: {"ok": False})
        wait_idle(self.js)
        self.assertEqual(job.error, "oxp3_apply: failed")
        job, _ = self.js.start("x", "X", lambda job: "not a dict")
        wait_idle(self.js)
        self.assertEqual((job.state, job.error), ("failed", "x: returned str, not a dict"))

    def test_result_made_json(self):
        job, _ = self.js.start("x", "X", lambda job: {"ok": True, "odd": {1}, "t": (1, 2)})
        wait_idle(self.js)
        self.assertEqual(job.result, {"ok": True, "odd": "{1}", "t": [1, 2]})
        json.dumps(job.to_dict())

    def test_log_lines(self):
        job = jobs.Job("k", "t")
        job.say("a\nb\r\nc  ")
        job.say(42)
        self.assertEqual(job.log, ["a", "b", "c", "42"])
        for i in range(600):
            job.say(f"line {i}")
        self.assertEqual(len(job.log), 500)
        self.assertEqual(job.log[-1], "line 599")
        self.assertEqual(job.to_dict(tail=2)["log"], ["line 598", "line 599"])
        self.assertEqual(job.to_dict(tail=0)["log"], [])
        self.assertEqual(len(job.to_dict()["log"]), 500)
        self.assertEqual(set(job.to_dict()), {"id", "kind", "title", "state", "started", "ended", "log",
                                              "result", "error"})

    def test_persistence_and_interrupted(self):
        job, _ = self.js.start("x", "Old", lambda job: {"ok": True})
        wait_idle(self.js)
        folder = os.path.join(self.tmp.name, "jobs")
        cut = {"id": "20261104-120000-abcdef", "kind": "xess_install", "title": "Install XeSS",
               "state": "running", "started": time.time() + 1, "ended": None, "log": ["downloading"],
               "result": None, "error": None}
        with open(os.path.join(folder, cut["id"] + ".json"), "w") as f:
            json.dump(cut, f)
        for name, content in (("garbage.json", "{"), ("list.json", "[]"),
                              ("renamed.json", json.dumps(dict(cut, id="other"))), ("notes.txt", "x")):
            with open(os.path.join(folder, name), "w") as f:
                f.write(content)
        again = Jobs(self.tmp.name)
        self.assertEqual(again.get(job.id).title, "Old")
        self.assertEqual(again.get(job.id).state, "done")
        j = again.get(cut["id"])
        self.assertEqual((j.state, j.error, j.log), ("failed", jobs.INTERRUPTED, ["downloading"]))
        self.assertIsNotNone(j.ended)
        self.assertEqual(self.saved(j)["state"], "failed")
        self.assertIsNone(again.running())
        self.assertEqual(again.get(None), j)
        self.assertEqual([x.id for x in again.recent()], [cut["id"], job.id])
        self.assertEqual(again.interrupted, [j])
        again.set_error(j, "worded again")
        self.assertEqual((j.error, self.saved(j)["error"]), ("worded again", "worded again"))
        self.assertEqual(Jobs(self.tmp.name).interrupted, [])      # saved as failed: not cut short twice

    def test_linked_job_file_not_read(self):
        with tempfile.TemporaryDirectory() as d:
            outside = os.path.join(d, "x.json")
            with open(outside, "w") as f:
                json.dump({"id": "x", "kind": "k", "title": "secret", "state": "done", "started": 1}, f)
            os.symlink(outside, os.path.join(self.tmp.name, "jobs", "x.json"))
            self.assertIsNone(Jobs(self.tmp.name).get("x"))

    def test_deletes_do_not_follow_a_swapped_jobs_folder(self):
        """SEC-1: the user swaps jobs/ for a symlink after the load; pruning the oldest job must not make root
        delete <id>.json in the folder the link points at."""
        folder = os.path.join(self.tmp.name, "jobs")
        for i in range(jobs.KEEP_JOBS):
            jid = "intel_icd.x86_64" if i == 0 else f"planted-{i}"
            with open(os.path.join(folder, jid + ".json"), "w") as f:
                json.dump({"id": jid, "kind": "k", "title": "t", "state": "done", "started": i + 1}, f)
        js = Jobs(self.tmp.name)
        self.assertIsNotNone(js.get("intel_icd.x86_64"))
        victim = os.path.join(self.tmp.name, "icd.d")
        os.mkdir(victim)
        open(os.path.join(victim, "intel_icd.x86_64.json"), "w").close()
        os.rename(folder, folder + ".moved")
        os.symlink(victim, folder)
        with self.assertLogs("oxp3deck", "WARNING"):      # the saves are refused too
            js.start("k", "t", lambda job: {"ok": True})
            wait_idle(js)
        self.assertIsNone(js.get("intel_icd.x86_64"))     # dropped from memory
        self.assertEqual(os.listdir(victim), ["intel_icd.x86_64.json"])
        os.unlink(folder)
        os.rename(folder + ".moved", folder)
        js.start("k", "t", lambda job: {"ok": True})        # the real folder: pruned as before
        wait_idle(js)
        self.assertNotIn("planted-1.json", os.listdir(folder))

    def test_linked_jobs_folder_not_read(self):
        with tempfile.TemporaryDirectory() as d:
            other = os.path.join(d, "other")
            os.mkdir(other)
            with open(os.path.join(other, "x.json"), "w") as f:
                json.dump({"id": "x", "kind": "k", "title": "secret", "state": "done", "started": 1}, f)
            os.symlink(other, os.path.join(d, "jobs"))
            with self.assertLogs("oxp3deck", "WARNING"):
                js = Jobs(d)
            self.assertEqual(js.recent(), [])

    def test_user_owned_link_above_the_runtime_folder(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "etc")
            os.makedirs(os.path.join(target, "OXP3Deck"))
            link = os.path.join(d, "data")
            os.symlink(target, link)
            if os.geteuid() == 0:
                os.lchown(link, 1000, 1000)     # as the user would have made it
            with self.assertLogs("oxp3deck", "WARNING"):
                js = Jobs(os.path.join(link, "OXP3Deck"))
                js.start("k", "t", lambda job: {"ok": True})
                wait_idle(js)       # its last save is logged too
            self.assertEqual(os.listdir(os.path.join(target, "OXP3Deck")), [])   # no jobs/ and no job file

    def test_running_job_saved_now_and_then(self):
        go = threading.Event()
        self.addCleanup(go.set)
        orig = jobs.SAVE_EVERY
        jobs.SAVE_EVERY = 0.0
        self.addCleanup(setattr, jobs, "SAVE_EVERY", orig)

        def fn(job):
            job.say("half way")
            go.wait(10)
            return {"ok": True}

        job, _ = self.js.start("x", "X", fn)
        end = time.monotonic() + 5
        while "half way" not in self.saved(job)["log"] and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(self.saved(job)["log"], ["half way"])
        self.assertEqual(self.saved(job)["state"], "running")
        go.set()
        wait_idle(self.js)

    def test_old_jobs_pruned(self):
        for i in range(jobs.KEEP_JOBS + 3):
            self.js.start("x", f"J{i}", lambda job: {"ok": True})
            wait_idle(self.js)
        self.assertEqual(len(os.listdir(os.path.join(self.tmp.name, "jobs"))), jobs.KEEP_JOBS)
        self.assertEqual(self.js.recent(1)[0].title, f"J{jobs.KEEP_JOBS + 2}")


if __name__ == "__main__":
    unittest.main()
