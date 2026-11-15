"""Run with: python -B -m unittest discover -s src -v  (Python 3.6 or newer)."""

import base64
import calendar
import contextlib
import importlib.util
import io
import itertools
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import ec2_idle
import ec2_up

HERE = Path(__file__).resolve().parent


class Case(unittest.TestCase):
    def enter(self, manager):
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value


def completed(code=0, stdout=""):
    return subprocess.CompletedProcess(["test"], code, stdout, "")


class IdleTests(Case):
    def setUp(self):
        folder = self.enter(tempfile.TemporaryDirectory())
        self.state = Path(folder, "state.json")
        self.env = {"WARN_MINUTES": "1", "STOP_MINUTES": "2",
                    "WORK_PROBE": "", "SNS_TOPIC_ARN": "arn:aws:sns:test-region:1:topic",
                    "WAKE_PAGE_URL": "https://wake.example/", "INSTANCE_NAME": "dev-box",
                    "MONTHLY_EMAIL": "off"}
        self.activity = self.enter(patch.object(ec2_idle, "busy_reason", return_value=None))
        self.publish = self.enter(patch.object(ec2_idle, "publish"))
        self.stop = self.enter(patch.object(ec2_idle, "stop_instance"))
        self.context = self.enter(patch.object(ec2_idle, "instance_context", return_value={
            "instance_id": "i-test", "region": "test-region", "name": ""}))
        self.clock = self.enter(patch.object(ec2_idle.time, "monotonic"))
        self.wall = self.enter(patch.object(ec2_idle.time, "time", return_value=50_000))
        self.output = self.enter(contextlib.redirect_stdout(io.StringIO()))
        self.enter(contextlib.redirect_stderr(io.StringIO()))

    def check(self, now):
        self.clock.return_value = now
        ec2_idle.watch(ec2_idle.load_config(self.env), self.state)
        return json.loads(self.state.read_text())

    def test_observed_idle_warns_once_then_stops_once(self):
        self.assertEqual(self.check(1000)["since"], 1000)
        self.assertEqual(self.check(1060)["warned"], 1060)
        self.assertEqual(self.check(1120)["stop_requested"], 1120)
        self.check(1180)
        self.assertEqual(self.output.getvalue().count("Warning email accepted"), 1)
        self.assertEqual(self.output.getvalue().count("Requested EC2 StopInstances"), 1)
        self.stop.assert_called_once()
        self.assertEqual(self.publish.call_count, 2)    # one warning, one "stopped"

    def test_busy_cancels_warning_and_resets_idle(self):
        self.check(1000)
        self.check(1060)
        self.activity.return_value = "remote login session"
        self.assertEqual(self.check(1119), {})
        self.activity.return_value = None
        self.assertEqual(self.check(1120)["since"], 1120)
        self.stop.assert_not_called()

    def test_gap_and_probe_failure_reset_idle(self):
        self.check(1000)
        self.assertEqual(self.check(1250)["since"], 1000)   # a slow, busy host is tolerated
        self.assertEqual(self.check(1600)["since"], 1600)   # a long pause restarts the clock
        self.activity.side_effect = RuntimeError("probe unavailable")
        with self.assertRaisesRegex(RuntimeError, "probe"):
            self.check(1660)
        self.assertEqual(json.loads(self.state.read_text()), {})

    def test_final_recheck_cancels_stop(self):
        self.check(1000)
        self.check(1060)
        self.activity.side_effect = [None, "workload"]
        self.assertEqual(self.check(1120), {})
        self.stop.assert_not_called()

    def test_publishes_the_warning_before_the_stop(self):
        calls = Mock()
        calls.attach_mock(self.publish, "publish")
        calls.attach_mock(self.stop, "stop")
        for now in (1000, 1060, 1120):
            self.check(now)
        self.assertEqual([name for name, _, _ in calls.mock_calls], ["publish", "stop", "publish"])
        (_, _, warn_subject, warn_body), (_, _, stop_subject, stop_body) = [
            c[0] for c in self.publish.call_args_list]
        self.assertEqual(warn_subject, "EC2 dev-box (i-test) will stop in 1 minute")
        self.assertIn("has had no activity for 1 minute.", warn_body)
        self.assertEqual(stop_subject, "EC2 dev-box (i-test) has stopped")
        self.assertIn("https://wake.example/", stop_body)
        self.assertIn("curl -d pin=YOUR_PIN -d action=wake https://wake.example/", stop_body)
        for text in (warn_subject, stop_subject):
            self.assertTrue(all(ord(ch) < 128 for ch in text) and len(text) < 100)

    def test_one_warning_per_hour_even_across_idle_cycles(self):
        self.check(1000)
        self.check(1060)                       # warning email
        self.activity.return_value = "remote login session"
        self.check(1100)                       # you came back
        self.activity.return_value = None
        self.check(1110)
        self.wall.return_value = 50_000 + 30 * 60
        self.check(1170)                       # idle again 30 min later: no second warning
        self.assertEqual(self.publish.call_count, 1)
        self.assertIn("Warning email skipped", self.output.getvalue())
        self.activity.return_value = "remote login session"
        self.check(1180)                       # you came back again
        self.activity.return_value = None
        self.wall.return_value = 50_000 + 61 * 60
        self.check(1190)
        self.check(1250)                       # new cycle after an hour may warn again
        warnings = [c for c in self.publish.call_args_list if "will stop" in c[0][2]]
        self.assertEqual(len(warnings), 2)

    def test_stopped_email_is_sent_once_and_never_blocks_the_stop(self):
        for now in (1000, 1060):
            self.check(now)
        self.publish.side_effect = RuntimeError("SNS down")
        self.check(1120)                       # stop requested although the email failed
        self.stop.assert_called_once()
        self.publish.side_effect = None
        for now in range(1180, 1780, 60):      # still running 10 min later: stop is retried
            self.check(now)
        self.assertEqual(self.stop.call_count, 2)
        stopped = [c for c in self.publish.call_args_list if "has stopped" in c[0][2]]
        self.assertEqual(len(stopped), 1)

    def test_delayed_email_preserves_full_warning_window(self):
        self.check(1000)
        self.publish.side_effect = RuntimeError("SNS error")
        with self.assertRaisesRegex(RuntimeError, "SNS error"):
            self.check(1060)
        self.publish.side_effect = None
        state = self.check(1120)
        self.assertEqual(state["warned"], 1120)
        self.assertNotIn("stop_requested", state)
        self.assertEqual(self.check(1180)["stop_requested"], 1180)

    def test_broken_email_cannot_block_the_stop_forever(self):
        self.env.update(WARN_RETRIES="2")
        self.check(1000)
        self.publish.side_effect = RuntimeError("topic deleted")
        with self.assertRaises(RuntimeError):
            self.check(1060)
        self.assertEqual(self.check(1120)["warned"], 1120)
        self.check(1180)
        self.stop.assert_called_once()

    def test_without_topic_stops_without_email(self):
        self.env.update(SNS_TOPIC_ARN="")
        for now in (1000, 1060, 1120):
            self.check(now)
        self.publish.assert_not_called()
        self.stop.assert_called_once()

    def test_an_unreadable_monthly_note_file_never_blocks_the_stop(self):
        self.env.update(MONTHLY_EMAIL="on")
        folder = self.state.parent / "health.json"
        folder.mkdir()                                       # reading it raises, as an unreadable file would
        for now in (1000, 1060, 1120):
            self.check(now)
        self.stop.assert_called_once()

    def test_unreadable_instance_details_do_not_hold_back_the_stop(self):
        # Credentials fail once while the warning is due: that counts as a failed email, then the stop proceeds.
        self.env.update(WARN_RETRIES="1")
        good = {"instance_id": "i-test", "region": "test-region", "name": ""}
        self.context.side_effect = [RuntimeError("metadata unavailable"), good, good]
        self.check(1000)
        self.assertEqual(self.check(1060)["warned"], 1060)
        self.publish.assert_not_called()
        self.check(1120)
        self.stop.assert_called_once()

    def test_a_stopped_email_is_allowed_again_after_you_come_back(self):
        self.check(1000)
        self.check(1060)
        self.check(1120)                       # stop requested, "stopped" email sent
        self.activity.return_value = "remote login session"
        self.check(1180)                       # you are back before it finished stopping
        self.activity.return_value = None
        for now in (1240, 1300, 1360):
            self.check(now)
        stopped = [c for c in self.publish.call_args_list if "has stopped" in c[0][2]]
        self.assertEqual(len(stopped), 2)

    def test_monthly_note_stop_count_and_other_emails_live_side_by_side(self):
        self.env["MONTHLY_EMAIL"] = "on"
        health = self.state.with_name("health.json")
        health.write_text(json.dumps({"month": "1969-12", "stops": {"1969-12": 4}}))
        self.check(1000)                       # first run of the month: the note goes out
        self.check(1060)
        self.check(1120)
        subjects = [c[0][2] for c in self.publish.call_args_list]
        self.assertEqual(len(subjects), 3)
        self.assertIn("idle stop is still working (January 1970)", subjects[0])
        self.assertIn("Times it stopped the instance in December 1969: 4", self.publish.call_args_list[0][0][3])
        self.assertIn("will stop in", subjects[1])
        self.assertIn("has stopped", subjects[2])
        saved = json.loads(health.read_text())
        self.assertEqual((saved["month"], saved["stops"]), ("1970-01", {"1969-12": 4, "1970-01": 1}))
        emails = json.loads(self.state.with_name("emails.json").read_text())
        self.assertTrue(emails["stopped_sent"] and "warned_at" in emails and "health_month" not in emails)
        for now in range(1180, 1780, 60):      # a retried stop is still one stop, and no second note
            self.check(now)
        self.assertEqual(json.loads(health.read_text())["stops"]["1970-01"], 1)
        self.assertEqual(self.publish.call_count, 3)

    def test_email_subject_is_plain_ascii_on_one_line(self):
        self.assertEqual(ec2_idle.plain_subject("EC2 caf\u00e9\nbox"), "EC2 caf??box")
        self.assertEqual(len(ec2_idle.plain_subject("x" * 500)), 99)

    def test_stop_is_retried_if_the_instance_keeps_running(self):
        for now in (1000, 1060, 1120):
            self.check(now)
        for now in range(1180, 1720, 60):
            self.check(now)
        self.assertEqual(self.stop.call_count, 1)
        self.check(1720)
        self.assertEqual(self.stop.call_count, 2)

    def test_invalid_config_is_rejected(self):
        for change in ({"WARN_MINUTES": "5", "STOP_MINUTES": "5"},
                       {"SNS_TOPIC_ARN": "topic"}, {"WARN_RETRIES": "0"},
                       {"WAKE_PAGE_URL": "http://insecure.example/"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ec2_idle.load_config({**self.env, **change})

    def test_an_old_dry_run_settings_file_is_refused_not_turned_into_a_real_stop(self):
        with self.assertRaisesRegex(ValueError, "Dry-run mode was removed"):
            ec2_idle.load_config({**self.env, "IDLE_MODE": "dry-run"})
        self.assertIsNotNone(ec2_idle.load_config({**self.env, "IDLE_MODE": "stop"}))   # the old stop setting is ignored


class ActivityTests(Case):
    def sessions(self, listing, details):
        def fake(*args, **kwargs):
            if args[1] == "list-sessions":
                return completed(0, listing)
            return details[args[2]]
        return patch.object(ec2_idle, "run", side_effect=fake)

    def test_counts_only_open_remote_sessions(self):
        listing = "1 1000 ubuntu\n2 1000 ubuntu\nc3 1000 ubuntu tty1\n4 0 root\n"
        details = {"1": completed(0, "Remote=yes\nState=active\n"),
                   "2": completed(0, "Remote=yes\nState=closing\n"),
                   "c3": completed(0, "Remote=no\nState=active\n"),
                   "4": completed(1)}
        with self.sessions(listing, details):
            self.assertEqual(ec2_idle._sessions_loginctl(), 1)
        with self.sessions("", {}):
            self.assertEqual(ec2_idle._sessions_loginctl(), 0)

    def test_session_counting_rules_and_library_fallback(self):
        remote = {"1": True, "2": True, "3": False}
        states = {"1": "active", "2": "closing", "3": "active"}
        self.assertEqual(ec2_idle.count_remote(remote, remote.get, states.get), 1)
        with patch.object(ec2_idle, "_sessions_sd_login", return_value=2) as library, \
                patch.object(ec2_idle, "_sessions_loginctl") as fallback:
            self.assertEqual(ec2_idle.remote_sessions(), 2)
            fallback.assert_not_called()
        with patch.object(ec2_idle, "_sessions_sd_login", side_effect=OSError("no libsystemd")), \
                patch.object(ec2_idle, "_sessions_loginctl", return_value=3):
            self.assertEqual(ec2_idle.remote_sessions(), 3)

    def test_session_manager_found_by_scanning_proc(self):
        with tempfile.TemporaryDirectory() as proc:
            for pid, comm in (("1", b"systemd\n"), ("42", b"sshd\n"), ("self", b"x\n")):
                Path(proc, pid).mkdir()
                Path(proc, pid, "comm").write_bytes(comm)
            with patch.object(ec2_idle, "PROC_ROOT", proc), \
                    patch.object(ec2_idle, "run") as run:
                self.assertFalse(ec2_idle.ssm_session())
                Path(proc, "77").mkdir()
                Path(proc, "77", "comm").write_bytes(b"ssm-session-wor\n")
                self.assertTrue(ec2_idle.ssm_session())
                run.assert_not_called()

    def test_busy_reasons_and_probe_exit_codes(self):
        with patch.object(ec2_idle, "remote_sessions", return_value=1):
            self.assertEqual(ec2_idle.busy_reason("/probe"), "remote login session")
        with patch.object(ec2_idle, "remote_sessions", return_value=0), \
                patch.object(ec2_idle, "ssm_session", return_value=True):
            self.assertEqual(ec2_idle.busy_reason(""), "Session Manager session")
        for code, expected in [(0, "workload"), (1, None), (2, RuntimeError)]:
            with self.subTest(code=code), \
                    patch.object(ec2_idle, "run", return_value=completed(code)):
                if expected is RuntimeError:
                    with self.assertRaises(RuntimeError):
                        ec2_idle.busy_reason("/probe", check_sessions=False)
                else:
                    self.assertEqual(ec2_idle.busy_reason("/probe", check_sessions=False),
                                     expected)

    def test_ssm_session_detection_without_proc_uses_pgrep(self):
        self.enter(patch.object(ec2_idle, "PROC_ROOT", "/no/such/proc"))
        for code, expected in [(0, True), (1, False)]:
            with patch.object(ec2_idle, "run", return_value=completed(code)):
                self.assertEqual(ec2_idle.ssm_session(), expected)
        with patch.object(ec2_idle, "run", return_value=completed(3)), \
                self.assertRaises(RuntimeError):
            ec2_idle.ssm_session()


def find_bash():
    """A working bash for the installer snippets, or None (a Windows stub does not count)."""
    try:
        result = subprocess.run(["bash", "-c", "echo ok"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return "bash" if result.stdout.strip() == "ok" else None


class WatchTests(Case):
    """The ready-made long-job check, against fake /proc folders (like the Session Manager test)."""

    def setUp(self):
        self.proc = self.enter(tempfile.TemporaryDirectory())
        self.enter(patch.object(ec2_idle, "PROC_ROOT", self.proc))
        self.enter(patch.object(ec2_idle, "CLK_TCK", 100))
        Path(self.proc, "uptime").write_text("100000.00 5.00\n")
        self.cpu_file = Path(self.enter(tempfile.TemporaryDirectory()), "watch.json")
        self.cfg = ec2_idle.load_config({"WATCH": "builds,agents,training"})
        self.pids = itertools.count(100)

    def add(self, *argv, comm=None, state="S", hours=0.1, cpu=0, pid=None):
        pid = pid or next(self.pids)
        folder = Path(self.proc, str(pid))
        folder.mkdir()
        fields = ["0"] * 40
        fields[0], fields[11], fields[19] = state, str(cpu), str(int((100000 - hours * 3600) * 100))
        name = comm or os.path.basename(argv[0])[:15]
        (folder / "stat").write_text("%d (%s) %s\n" % (pid, name, " ".join(fields)))
        (folder / "cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")
        return folder

    def busy(self):
        return ec2_idle.watch_busy(self.cfg, self.cpu_file)

    def test_every_group_keeps_the_instance_on(self):
        jobs = [("make", "-j4"), ("cmake", "--build", "."), ("ninja",), ("cargo", "build", "--release"),
                ("npm", "install"), ("npm", "ci"), ("npm", "run", "build"), ("yarn",), ("yarn", "build"),
                ("pnpm", "install"), ("pnpm", "run", "build:prod"), ("pip", "install", "-r", "x.txt"),
                ("pip3", "install", "numpy"), ("python3", "-m", "pip", "install", "numpy"),
                ("docker", "build", "-t", "x", "."), ("docker", "buildx", "build", "."),
                ("node", "/usr/lib/node_modules/npm/bin/npm-cli.js", "run", "build"),
                ("python3", "train.py", "--epochs", "3"), ("python3.11", "/home/u/finetune_llm.py"),
                ("python", "-m", "trainer.main")]
        for argv in jobs:
            with self.subTest(argv=argv):
                folder = self.add(*argv)
                self.assertTrue((self.busy() or "").startswith("watched job: "), argv)
                for child in folder.iterdir():
                    child.unlink()
                folder.rmdir()
        self.assertIsNone(self.busy())

    def test_names_say_what_is_running(self):
        self.add("npm", "run", "build")
        self.assertEqual(self.busy(), "watched job: npm build")
        self.assertEqual([job["name"] for job in ec2_idle.watch_scan(("builds",))], ["npm build"])

    def test_programs_that_never_finish_do_not_count(self):
        for argv in [("npm", "start"), ("npm", "run", "dev"), ("yarn", "dev"), ("docker", "run", "x"),
                     ("cargo", "run"), ("pip", "list"), ("python3", "app.py"), ("python3", "fitness.py"),
                     ("python3", "-m", "http.server"), ("sshd",), ("npm", "test")]:
            self.add(*argv)
        self.assertIsNone(self.busy())

    def test_agents_count_only_while_their_cpu_time_grows(self):
        folder = self.add("claude", cpu=1000)
        self.assertEqual(self.busy(), "watched job: claude")  # first look: it has just started, so it counts once
        self.assertIsNone(self.busy())                       # flat CPU: open but idle
        (folder / "stat").write_text((folder / "stat").read_text().replace(" 1000 ", " 1100 "))
        self.assertEqual(self.busy(), "watched job: claude")  # 1 CPU second since the last look
        self.assertIsNone(self.busy())                       # flat again
        (folder / "stat").write_text((folder / "stat").read_text().replace(" 1100 ", " 1110 "))
        self.assertIsNone(self.busy())                       # 0.1 s is background noise, not work

    def test_agents_started_through_node_or_python_are_found(self):
        self.assertEqual(ec2_idle.classify(["node", "/usr/local/bin/claude"]), ("agents", "claude"))
        self.assertEqual(ec2_idle.classify(["python3", "/usr/local/bin/aider", "--model", "x"]), ("agents", "aider"))
        self.assertEqual(ec2_idle.classify(["codex", "exec"]), ("agents", "codex"))

    def test_wrapped_and_option_heavy_commands_are_found(self):
        found = ec2_idle.classify
        self.assertEqual(found(["npm", "--prefix", "/workspace", "run", "build"]), ("builds", "npm build"))
        self.assertEqual(found(["sudo", "make"]), ("builds", "make"))
        self.assertEqual(found(["timeout", "3h", "make", "all"]), ("builds", "make"))
        self.assertEqual(found(["sudo", "-u", "bob", "npm", "run", "build"]), ("builds", "npm build"))
        self.assertEqual(found(["env", "A=1", "make"]), ("builds", "make"))
        self.assertEqual(found(["uv", "run", "train.py"]), ("training", "python training script"))
        self.assertEqual(found(["node", "/opt/my project/npm-cli.js", "run", "build"]), ("builds", "npm build"))
        self.assertIsNone(found(["npm", "start"]))
        self.assertIsNone(found(["python3", "manage.py", "fit_customers"]))   # only the script's own name counts

    def test_agent_cpu_survives_between_checks_and_bad_files(self):
        self.add("aider", cpu=5)
        self.busy()
        self.assertEqual(list(json.loads(self.cpu_file.read_text()).values()), [5])
        self.cpu_file.write_text("not json")
        self.assertEqual(self.busy(), "watched job: aider")  # a damaged file only means "no earlier look"
        self.assertIsNone(self.busy())                       # and the next look is a normal one
        self.cpu_file.write_text(json.dumps({"1:2": "x"}))
        self.assertEqual(self.busy(), "watched job: aider")

    def test_old_builds_are_ignored_and_the_cap_can_change(self):
        self.add("make", hours=13)
        self.add("python3", "train.py", hours=30)
        self.assertIsNone(self.busy())
        self.cfg = ec2_idle.load_config({"WATCH": "builds,training", "WATCH_MAX_HOURS": "48"})
        self.assertEqual(self.busy(), "watched job: make")
        self.cfg = ec2_idle.load_config({"WATCH": "builds", "WATCH_MAX_HOURS": "1"})
        self.assertIsNone(self.busy())                       # 13 hours is now far over the 1 hour cap
        self.add("make", hours=0.5)
        self.assertEqual(self.busy(), "watched job: make")

    def test_zombies_and_stopped_jobs_do_not_count(self):
        for state in "ZTtXx":
            self.add("make", state=state)
        self.assertIsNone(self.busy())
        self.add("make", state="R")
        self.assertEqual(self.busy(), "watched job: make")

    def test_only_the_chosen_groups_are_watched(self):
        self.add("make")
        self.add("python3", "train.py")
        self.add("claude", cpu=1)
        self.cfg = ec2_idle.load_config({"WATCH": "training"})
        self.assertEqual(self.busy(), "watched job: python training script")
        self.assertEqual([job["group"] for job in ec2_idle.watch_scan(("builds",))], ["builds"])
        self.assertEqual(len(ec2_idle.watch_scan(ec2_idle.WATCH_GROUPS)), 3)

    def test_the_timer_and_installer_never_count_themselves(self):
        self.add("python3", "-I", "-S", "/usr/local/sbin/ec2_idle.py", "--check", "train")
        self.add("bash", "install_guest.sh", "--watch", "make")
        self.add("make", pid=os.getpid())
        self.assertIsNone(self.busy())

    def test_odd_process_entries_are_skipped(self):
        self.add("make", comm="a) (b")                         # a name with brackets in it
        self.assertEqual(self.busy(), "watched job: make")
        Path(self.proc, "999").mkdir()                          # vanished: no stat file
        Path(self.proc, "1000").mkdir()
        Path(self.proc, "1000", "stat").write_text("garbage")
        Path(self.proc, "1001").mkdir()
        Path(self.proc, "1001", "stat").write_text("1 (kthreadd) S " + " ".join(["0"] * 40))
        Path(self.proc, "1001", "cmdline").write_bytes(b"")   # kernel threads have no command line
        Path(self.proc, "self").mkdir()
        self.assertEqual(self.busy(), "watched job: make")

    def test_unreadable_proc_is_an_error_not_an_idle_answer(self):
        with patch.object(ec2_idle, "PROC_ROOT", "/no/such/proc"), self.assertRaisesRegex(RuntimeError, "/proc"):
            self.busy()
        Path(self.proc, "uptime").write_text("soon")
        with self.assertRaisesRegex(RuntimeError, "uptime"):
            self.busy()

    def test_busy_reason_order_and_probe_still_works(self):
        with patch.object(ec2_idle, "remote_sessions", return_value=0), \
                patch.object(ec2_idle, "ssm_session", return_value=False), \
                patch.object(ec2_idle, "run", return_value=completed(0)) as run:
            self.assertEqual(ec2_idle.busy_reason("/probe", watched=lambda: "watched job: make"),
                             "watched job: make")
            run.assert_not_called()                          # the cheap check answers first
            self.assertEqual(ec2_idle.busy_reason("/probe", watched=lambda: None), "workload")
            self.assertEqual(ec2_idle.busy_reason("/probe", watched=None), "workload")
        with patch.object(ec2_idle, "remote_sessions", return_value=1):
            self.assertEqual(ec2_idle.busy_reason("", watched=lambda: "watched job: make"),
                             "remote login session")

    def test_timer_stays_busy_and_resets_the_clock_while_a_job_runs(self):
        self.add("make")
        state = Path(self.proc, "state.json")
        output = io.StringIO()
        with patch.object(ec2_idle, "remote_sessions", return_value=0), \
                patch.object(ec2_idle, "ssm_session", return_value=False), \
                patch.object(ec2_idle.time, "monotonic", return_value=1000), \
                contextlib.redirect_stdout(output):
            ec2_idle.watch(self.cfg, state)
        self.assertIn("Busy (watched job: make). Idle clock reset.", output.getvalue())
        self.assertEqual(json.loads(state.read_text()), {})

    def test_watch_off_by_default_and_never_scans(self):
        cfg = ec2_idle.load_config({})
        self.assertEqual((cfg.watch, cfg.watch_max), ((), 12))
        self.add("make")
        with patch.object(ec2_idle, "remote_sessions", return_value=0), \
                patch.object(ec2_idle, "ssm_session", return_value=False), \
                patch.object(ec2_idle, "watch_scan") as scan, \
                patch.object(ec2_idle.time, "monotonic", return_value=1000), \
                contextlib.redirect_stdout(io.StringIO()):
            ec2_idle.watch(cfg, Path(self.proc, "state.json"))
        scan.assert_not_called()

    def test_settings_are_checked_strictly(self):
        self.assertEqual(ec2_idle.load_config({"WATCH": " agents , builds,agents "}).watch, ("agents", "builds"))
        self.assertEqual(ec2_idle.load_config({"WATCH": ""}).watch, ())
        for bad in ({"WATCH": "builds,gpu"}, {"WATCH": "all"}, {"WATCH": "builds,"}, {"WATCH": "off"},
                    {"WATCH": "builds;rm"}, {"WATCH_MAX_HOURS": "0"}, {"WATCH_MAX_HOURS": "721"},
                    {"WATCH_MAX_HOURS": "-1"}, {"WATCH_MAX_HOURS": "x"}, {"WATCH_MAX_HOURS": ""}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ec2_idle.load_config(bad)
        self.assertEqual(ec2_idle.load_config({"WATCH_MAX_HOURS": "720"}).watch_max, 720)

    def test_the_settings_file_template_round_trips(self):
        env = {}
        for line in (HERE / "ec2-idle.conf").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                env[key] = value.strip('"')
        cfg = ec2_idle.load_config(env)
        self.assertEqual((cfg.watch, cfg.watch_max), ((), 12))
        env["WATCH"] = "builds,agents,training"
        self.assertEqual(ec2_idle.load_config(env).watch, ec2_idle.WATCH_GROUPS)

    def test_check_reports_what_is_running_and_fails_without_proc(self):
        self.add("make", hours=0.5)
        self.add("python3", "train.py", hours=20)
        self.add("claude")
        out = self.enter(contextlib.redirect_stdout(io.StringIO()))
        self.enter(patch.object(ec2_idle, "remote_sessions", return_value=1))
        self.enter(patch.object(ec2_idle, "ssm_session", return_value=False))
        self.enter(patch.object(ec2_idle, "instance_context", return_value={
            "instance_id": "i-test", "region": "test-region", "role": "test-role"}))
        self.enter(patch.object(ec2_idle, "stop_instance", side_effect=ec2_idle.AwsError("DryRunOperation", "ok")))
        self.assertEqual(ec2_idle.check(self.cfg), 0)
        text = out.getvalue()
        self.assertIn("watching: builds, agents, training", text)
        self.assertIn("make (builds, counts as busy, running 30 minutes)", text)
        self.assertIn("too old, ignored", text)
        self.assertIn("claude (agents, counts only while it uses CPU)", text)
        with patch.object(ec2_idle, "PROC_ROOT", "/no/such/proc"):
            self.assertEqual(ec2_idle.check(self.cfg), 1)
        self.assertIn("FAIL  Long-job check failed", out.getvalue())

    def test_installer_checks_the_new_files_before_it_installs_anything(self):
        script = (HERE / "install_guest.sh").read_text(encoding="utf-8")
        self.assertLess(script.index('echo "Checking this host..."'), script.index("root install -m 0640"))
        self.assertIn('"$python" -I -S "$PWD/ec2_idle.py"', script)       # the staged file, not the installed one
        self.assertIn("installing=1", script)                              # a failure part-way rolls back
        self.assertIn("roll_back", script)

    def test_installer_trust_check_covers_links_and_old_dry_run_settings(self):
        script = (HERE / "install_guest.sh").read_text(encoding="utf-8")
        self.assertIn('root_only "$real" && root_only "$(dirname -- "$given")"', script)   # target AND the link's folder
        self.assertIn("grep -Eq '^IDLE_MODE=.*dry-run'", script)           # any quoting of the old setting
        self.assertIn('[[ $topic == none ]] && topic=""', script)

    def test_stack_install_command_without_email_clears_the_topic(self):
        template = (HERE / "ec2-idle-stack.yaml").read_text(encoding="utf-8")
        self.assertIn('bash install_guest.sh --topic none --wake-url', template)

    def test_installer_accepts_only_known_groups(self):
        bash = find_bash()
        if not bash:
            self.skipTest("bash is not available")
        script = (HERE / "install_guest.sh").read_text(encoding="utf-8")
        function = next(line for line in script.splitlines() if line.startswith("valid_watch() {"))
        for value, expected in (("", 0), ("builds", 0), ("builds,agents,training", 0), ("agents,agents", 0),
                                ("gpu", 1), ("builds,", 1), (",builds", 1), ("builds agents", 1),
                                ("builds;id", 1), ("$(id)", 1), ("BUILDS", 1), ("all", 1)):
            with self.subTest(value=value):
                result = subprocess.run([bash, "-c", function + '\nvalid_watch "$1"', "_", value],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
                self.assertEqual(result.returncode, expected)

    def test_installer_keeps_and_writes_the_settings(self):
        script = (HERE / "install_guest.sh").read_text(encoding="utf-8")
        for needle in ("previous WATCH)", "previous WATCH_MAX_HOURS)", '"s|^WATCH=.*|WATCH=$watch|"',
                       '"s|^WATCH_MAX_HOURS=.*|WATCH_MAX_HOURS=$watch_hours|"',
                       "watch=builds,agents,training", "[[ $watch == off ]]", "--watch-hours)"):
            self.assertIn(needle, script)


class HealthTests(Case):
    """The monthly "still working" email."""

    NOVEMBER = calendar.timegm((2026, 11, 16, 12, 0, 0))
    DECEMBER = calendar.timegm((2026, 12, 1, 0, 1, 0))
    TOPIC = "arn:aws:sns:test-region:1:topic"

    def setUp(self):
        folder = self.enter(tempfile.TemporaryDirectory())
        self.path = Path(folder, "health.json")
        self.cfg = ec2_idle.load_config({"SNS_TOPIC_ARN": self.TOPIC, "WAKE_PAGE_URL": "https://wake.example/",
                                         "INSTANCE_NAME": "dev-box"})
        self.publish = self.enter(patch.object(ec2_idle, "publish"))
        self.context = self.enter(patch.object(ec2_idle, "instance_context", return_value={
            "instance_id": "i-test", "region": "test-region", "name": ""}))
        self.wall = self.enter(patch.object(ec2_idle.time, "time", return_value=self.NOVEMBER))
        self.enter(contextlib.redirect_stdout(io.StringIO()))
        self.errors = self.enter(contextlib.redirect_stderr(io.StringIO()))

    def seed(self, **data):
        self.path.write_text(json.dumps(data))

    def saved(self):
        return json.loads(self.path.read_text())

    def test_first_run_only_remembers_the_month(self):
        ec2_idle.health_email(self.cfg, self.path)
        self.publish.assert_not_called()
        self.assertEqual(self.saved(), {"month": "2026-11"})

    def test_sends_once_a_month_and_again_the_next(self):
        self.seed(month="2026-10", stops={"2026-10": 14})
        ec2_idle.health_email(self.cfg, self.path)
        ec2_idle.health_email(self.cfg, self.path)
        ec2_idle.health_email(self.cfg, self.path)
        self.assertEqual(self.publish.call_count, 1)
        _, topic, subject, body = self.publish.call_args[0]
        self.assertEqual(topic, self.TOPIC)
        self.assertEqual(subject, "EC2 dev-box (i-test): idle stop is still working (November 2026)")
        self.assertIn("Times it stopped the instance in October 2026: 14", body)
        self.assertIn("https://wake.example/", body)
        self.assertIn("--monthly-email off", body)
        self.assertLess(len(body.splitlines()), 12)
        self.assertEqual(self.saved()["month"], "2026-11")
        self.wall.return_value = self.DECEMBER
        ec2_idle.health_email(self.cfg, self.path)
        self.assertEqual(self.publish.call_count, 2)
        self.assertIn("in November 2026: 0", self.publish.call_args[0][3])
        self.assertEqual(self.saved()["month"], "2026-12")

    def test_off_and_no_topic_never_send(self):
        self.seed(month="2026-10")
        ec2_idle.health_email(ec2_idle.load_config({"SNS_TOPIC_ARN": self.TOPIC, "MONTHLY_EMAIL": "off"}), self.path)
        ec2_idle.health_email(ec2_idle.load_config({"MONTHLY_EMAIL": "on"}), self.path)
        self.publish.assert_not_called()
        self.assertEqual(self.saved(), {"month": "2026-10"})   # untouched, so turning it back on still sends

    def test_a_failed_send_does_not_mark_the_month_and_retries_once_a_day(self):
        self.seed(month="2026-10")
        self.publish.side_effect = RuntimeError("SNS down")
        ec2_idle.health_email(self.cfg, self.path)
        self.assertIn("Monthly note failed (SNS down)", self.errors.getvalue())
        self.assertEqual(self.saved()["month"], "2026-10")
        self.assertEqual(self.saved()["failed_at"], self.NOVEMBER)
        self.wall.return_value += 3600
        ec2_idle.health_email(self.cfg, self.path)          # an hour later: no spam
        self.assertEqual(self.publish.call_count, 1)
        self.wall.return_value += 24 * 3600
        self.publish.side_effect = None
        ec2_idle.health_email(self.cfg, self.path)
        self.assertEqual(self.publish.call_count, 2)
        self.assertEqual(self.saved(), {"month": "2026-11"})

    def test_unreadable_instance_details_count_as_a_failure_too(self):
        self.seed(month="2026-10")
        self.context.side_effect = RuntimeError("metadata unavailable")
        ec2_idle.health_email(self.cfg, self.path)
        self.publish.assert_not_called()
        self.assertEqual(self.saved()["month"], "2026-10")

    def test_damaged_files_and_odd_values_are_handled(self):
        for text in ("not json", "[]", json.dumps({"month": 5}), json.dumps({"month": "2026-13"}),
                     json.dumps({"month": "soon"}), json.dumps({"month": ""})):
            with self.subTest(text=text):
                self.path.write_text(text)
                ec2_idle.health_email(self.cfg, self.path)
                self.assertEqual(self.saved(), {"month": "2026-11"})
        self.assertEqual(self.publish.call_count, 0)
        self.seed(month="2026-10", stops="many", failed_at="yesterday")
        ec2_idle.health_email(self.cfg, self.path)
        self.assertIn("in October 2026: 0", self.publish.call_args[0][3])

    def test_a_disk_that_cannot_be_written_never_causes_repeats(self):
        self.seed(month="2026-10")
        with patch.object(ec2_idle, "save", return_value=False):
            ec2_idle.health_email(self.cfg, self.path)
        self.publish.assert_not_called()

    def test_a_clock_that_went_backwards_waits(self):
        self.seed(month="2027-01")
        ec2_idle.health_email(self.cfg, self.path)
        self.publish.assert_not_called()

    def test_stops_are_counted_per_month_and_old_months_are_dropped(self):
        for stamp in (calendar.timegm((2026, 7, 1, 0, 0, 0)), calendar.timegm((2026, 8, 1, 0, 0, 0)),
                      calendar.timegm((2026, 9, 1, 0, 0, 0)), calendar.timegm((2026, 10, 1, 0, 0, 0)),
                      self.NOVEMBER, self.NOVEMBER):
            self.wall.return_value = stamp
            ec2_idle.count_stop(self.path)
        self.assertEqual(self.saved()["stops"], {"2026-09": 1, "2026-10": 1, "2026-11": 2})
        self.seed(stops={"bad": 1, "2026-11": "x", "2026-10": 2})
        ec2_idle.count_stop(self.path)
        self.assertEqual(self.saved()["stops"], {"2026-10": 2, "2026-11": 1})

    def test_month_helpers(self):
        self.assertEqual(ec2_idle.month_key(self.NOVEMBER), "2026-11")
        self.assertEqual(ec2_idle.previous_month("2026-01"), "2025-12")
        self.assertEqual(ec2_idle.previous_month("2026-03"), "2026-02")
        self.assertEqual(ec2_idle.month_title("2026-02"), "February 2026")
        self.assertTrue(all(ec2_idle.valid_month(m) for m in ("2026-01", "1999-12")))
        self.assertFalse(any(ec2_idle.valid_month(m) for m in (None, 5, "", "2026-1", "2026-00", "2026-13",
                                                               "26-011", "abcd-ef")))

    def test_settings_and_installer_wiring(self):
        self.assertTrue(ec2_idle.load_config({}).monthly)
        self.assertFalse(ec2_idle.load_config({"MONTHLY_EMAIL": "off"}).monthly)
        for bad in ("yes", "ON", "", "true", "off;id"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ec2_idle.load_config({"MONTHLY_EMAIL": bad})
        script = (HERE / "install_guest.sh").read_text(encoding="utf-8")
        for needle in ("--monthly-email)", "previous MONTHLY_EMAIL)", "monthly=${monthly:-on}",
                       '"s|^MONTHLY_EMAIL=.*|MONTHLY_EMAIL=$monthly|"', "/run/ec2-idle /var/lib/ec2-idle"):
            self.assertIn(needle, script)
        self.assertIn("MONTHLY_EMAIL=on", (HERE / "ec2-idle.conf").read_text(encoding="utf-8"))
        unit = (HERE / "ec2-idle.service").read_text(encoding="utf-8")
        self.assertIn("Environment=STATE_DIRECTORY=/var/lib/ec2-idle", unit)    # on disk, so reboots keep it
        self.assertIn("ExecStartPre=/bin/mkdir -p -m 0700 /var/lib/ec2-idle", unit)

    def test_the_service_keeps_the_month_outside_the_memory_only_run_folder(self):
        with patch.object(ec2_idle, "watch") as watching, patch.dict(os.environ):
            os.environ.pop("STATE_DIRECTORY", None)
            os.environ.pop("RUNTIME_DIRECTORY", None)
            ec2_idle.main([])
        self.assertEqual(Path(watching.call_args[0][2]).as_posix(), "/var/lib/ec2-idle/health.json")
        self.assertEqual(Path(watching.call_args[0][1]).as_posix(), "/run/ec2-idle/state.json")


class SigningTests(Case):
    credentials = {"AccessKeyId": "AKIDEXAMPLE",
                   "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"}

    def test_matches_the_documented_aws_example(self):
        headers = ec2_idle.sign_request(
            "GET", "iam.amazonaws.com", "Action=ListUsers&Version=2010-05-08", "",
            {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
            self.credentials, "us-east-1", "iam", "20150830T123600Z")
        self.assertTrue(headers["authorization"].endswith(
            "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"))

    def test_matches_botocore_for_a_signed_post(self):
        try:
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest
            from botocore.credentials import Credentials
        except ImportError:
            self.skipTest("botocore is not installed")
        body = "Action=StopInstances&Version=2016-11-15&InstanceId.1=i-test"
        creds = {**self.credentials, "Token": "session-token"}
        ours = ec2_idle.sign_request(
            "POST", "ec2.test-region.amazonaws.com", "", body,
            {"content-type": "application/x-www-form-urlencoded; charset=utf-8"},
            creds, "test-region", "ec2", "20250101T000000Z")
        request = AWSRequest("POST", "https://ec2.test-region.amazonaws.com/", data=body,
                             headers={"content-type": ours["content-type"],
                                      "X-Amz-Date": "20250101T000000Z",
                                      "X-Amz-Security-Token": "session-token"})
        request.context["timestamp"] = "20250101T000000Z"
        auth = SigV4Auth(Credentials("AKIDEXAMPLE", self.credentials["SecretAccessKey"],
                                     "session-token"), "ec2", "test-region")
        theirs = auth.signature(auth.string_to_sign(request, auth.canonical_request(request)),
                                request)
        self.assertEqual(ours["authorization"].split("Signature=")[1], theirs)

    def test_aws_error_code_is_parsed(self):
        response = Mock(status=412)
        response.read.return_value = (b"<Response><Errors><Error><Code>DryRunOperation</Code>"
                                      b"<Message>Request would have succeeded</Message>"
                                      b"</Error></Errors></Response>")
        connection = Mock()
        connection.getresponse.return_value = response
        ctx = {"instance_id": "i-test", "region": "test-region", "domain": "amazonaws.com",
               "credentials": self.credentials}
        with patch("http.client.HTTPSConnection", return_value=connection):
            with self.assertRaises(ec2_idle.AwsError) as raised:
                ec2_idle.stop_instance(ctx, dry_run=True)
        self.assertEqual(raised.exception.code, "DryRunOperation")
        body = connection.request.call_args[1]["body"].decode()
        self.assertIn("DryRun=true", body)
        self.assertIn("InstanceId.1=i-test", body)


class CheckTests(Case):
    def setUp(self):
        self.enter(contextlib.redirect_stdout(io.StringIO()))
        self.enter(patch.object(ec2_idle, "ssm_session", return_value=False))
        self.enter(patch.object(ec2_idle, "instance_context", return_value={
            "instance_id": "i-test", "region": "test-region", "role": "test-role"}))
        self.cfg = ec2_idle.load_config({})

    def test_passes_when_role_can_stop(self):
        with patch.object(ec2_idle, "remote_sessions", return_value=1), \
                patch.object(ec2_idle, "stop_instance",
                             side_effect=ec2_idle.AwsError("DryRunOperation", "ok")):
            self.assertEqual(ec2_idle.check(self.cfg, expect_session=True), 0)

    def test_fails_when_session_or_permission_is_missing(self):
        with patch.object(ec2_idle, "remote_sessions", return_value=0), \
                patch.object(ec2_idle, "stop_instance",
                             side_effect=ec2_idle.AwsError("DryRunOperation", "ok")):
            self.assertEqual(ec2_idle.check(self.cfg, expect_session=True), 1)
        with patch.object(ec2_idle, "remote_sessions", return_value=1), \
                patch.object(ec2_idle, "stop_instance",
                             side_effect=ec2_idle.AwsError("UnauthorizedOperation", "no")):
            self.assertEqual(ec2_idle.check(self.cfg), 1)


class WakeClientTests(Case):
    def setUp(self):
        self.args = SimpleNamespace(url="https://wake.example", host=None, pin="k", timeout=300,
                                    no_wake=False, port=22, private_ip=False)
        self.enter(patch.object(ec2_up.time, "sleep"))
        self.enter(contextlib.redirect_stderr(io.StringIO()))

    def test_waits_through_stopping_and_pending_then_connects(self):
        replies = [(409, {"state": "stopping"}), (202, {"state": "pending"}),
                   (200, {"state": "running", "publicIp": "192.0.2.10"}),
                   (200, {"state": "running", "publicIp": "192.0.2.10"})]
        conn = Mock()
        with patch.object(ec2_up, "call", side_effect=replies) as call, \
                patch.object(ec2_up.socket, "create_connection",
                             side_effect=[ConnectionRefusedError, conn]) as connect:
            self.assertEqual(ec2_up.wait_for_ssh(self.args), ("192.0.2.10", conn))
        self.assertEqual({c[0][1] for c in call.call_args_list}, {"wake"})
        self.assertTrue(all(c[0][2] > 0 for c in call.call_args_list))   # the page does the waiting
        self.assertEqual(connect.call_count, 2)

    def test_running_instance_at_a_fixed_address_never_calls_the_wake_page(self):
        self.args.host = "203.0.113.5"
        conn = Mock()
        with patch.object(ec2_up, "call") as call, \
                patch.object(ec2_up.socket, "create_connection", return_value=conn) as connect:
            self.assertEqual(ec2_up.wait_for_ssh(self.args), ("203.0.113.5", conn))
        call.assert_not_called()
        self.assertEqual(connect.call_args[0][0], ("203.0.113.5", 22))
        self.assertEqual(connect.call_args[1]["timeout"], ec2_up.FAST_TIMEOUT)

    def test_stopped_instance_falls_back_to_the_wake_page_and_uses_its_ip(self):
        self.args.host = "203.0.113.5"
        conn = Mock()
        with patch.object(ec2_up, "call", return_value=(
                200, {"state": "running", "publicIp": "192.0.2.10"})) as call, \
                patch.object(ec2_up.socket, "create_connection",
                             side_effect=[OSError("timed out"), conn]) as connect:
            self.assertEqual(ec2_up.wait_for_ssh(self.args), ("192.0.2.10", conn))
        call.assert_called_once()
        self.assertEqual([c[0][0] for c in connect.call_args_list],
                         [("203.0.113.5", 22), ("192.0.2.10", 22)])

    def test_gives_up_with_the_last_state_after_the_timeout(self):
        self.args.timeout = 12
        clock = self.enter(patch.object(ec2_up.time, "monotonic"))
        clock.side_effect = itertools.count(0, 4)
        with patch.object(ec2_up, "call", return_value=(202, {"state": "pending"})), \
                self.assertRaisesRegex(RuntimeError, "not reachable after 12 seconds.*state pending"):
            ec2_up.wait_for_ssh(self.args)

    def test_ended_instance_stops_waiting(self):
        with patch.object(ec2_up, "call", return_value=(409, {"state": "terminated"})), \
                self.assertRaisesRegex(RuntimeError, "terminated"):
            ec2_up.wait_for_ssh(self.args)

    def test_no_wake_only_reads_status(self):
        self.args.no_wake = True
        with patch.object(ec2_up, "call", return_value=(200, {"state": "stopped"})) as call, \
                self.assertRaisesRegex(RuntimeError, "no-wake"):
            ec2_up.wait_for_ssh(self.args)
        call.assert_called_once_with(self.args, "status", 0)

    def test_command_line_wiring(self):
        pin_file = Path(self.enter(tempfile.TemporaryDirectory()), "pin")
        pin_file.write_text("1234\n", encoding="utf-8")
        out = self.enter(contextlib.redirect_stdout(io.StringIO()))
        with patch.object(ec2_up, "wait_for_ssh", return_value=("192.0.2.10", MagicMock())) as waiting:
            ec2_up.main(["https://wake.example", "203.0.113.5", "2222", "--pin-file", str(pin_file),
                         "--check"])
        args = waiting.call_args[0][0]
        self.assertEqual((args.pin, args.host, args.port), ("1234", "203.0.113.5", 2222))
        self.assertIn("192.0.2.10:2222", out.getvalue())
        with patch.dict(os.environ, {"EC2_WAKE_PIN": ""}), self.assertRaises(SystemExit):
            self.enter(contextlib.redirect_stderr(io.StringIO()))
            ec2_up.main(["https://wake.example"])

    def test_wrong_pin_and_lock_messages_are_reported(self):
        for code, message in ((403, "Wrong PIN"), (429, "Too many wrong PIN tries")):
            error = ec2_up.urllib.error.HTTPError("https://wake.example", code, "Rejected", {},
                                                  io.BytesIO(json.dumps({"error": message}).encode()))
            with patch.object(ec2_up.urllib.request, "urlopen", side_effect=error), \
                    self.assertRaisesRegex(RuntimeError, message):
                ec2_up.call(self.args, "wake")

    def test_proxy_relays_binary_in_both_directions(self):
        sent = b"\x00\x1a\r\nSSH-client\xff"
        returned = b"\x00\x1a\r\nSSH-server\xff"
        received = []
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(60)

            def server():
                with listener.accept()[0] as connection:
                    connection.settimeout(60)
                    while True:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        received.append(chunk)
                    connection.sendall(returned)

            worker = threading.Thread(target=server)
            worker.start()
            code = ("import ec2_up, socket; "
                    f"ec2_up.relay(socket.create_connection({listener.getsockname()!r}))")
            result = subprocess.run([sys.executable, "-B", "-c", code], input=sent,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=60, check=True, cwd=HERE)
            worker.join(timeout=60)
        self.assertEqual(b"".join(received), sent)
        self.assertEqual(result.stdout, returned)


class FakeDynamo:
    """Just enough of the DynamoDB client for the wrong-PIN counter."""

    def __init__(self):
        self.item = None
        self.fail = False
        self.deleted = 0

    def get_item(self, TableName, Key, ConsistentRead=False):
        if self.fail:
            raise RuntimeError("DynamoDB is down")
        return {"Item": dict(self.item)} if self.item else {}

    def update_item(self, TableName, Key, UpdateExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues, ReturnValues="NONE", ConditionExpression=None):
        if self.fail:
            raise RuntimeError("DynamoDB is down")
        values = {name: value["N"] for name, value in ExpressionAttributeValues.items()}
        item = self.item = self.item or {}
        if UpdateExpression == "SET #f = :one, #s = :now, #e = :exp":   # restart an expired window, only if still expired
            assert ConditionExpression, "an expired window must be restarted with a condition, or a burst can all reset it"
            if "since" not in item or int(item["since"]["N"]) >= int(values[":cutoff"]):
                raise self.error_class("ConditionalCheckFailedException")
            item["fails"], item["since"] = {"N": values[":one"]}, {"N": values[":now"]}
            item["expires"] = {"N": values[":exp"]}
        elif UpdateExpression.startswith("ADD #f"):
            item["fails"] = {"N": str(int(item.get("fails", {"N": "0"})["N"]) + 1)}
            item.setdefault("since", {"N": values[":now"]})
            item["expires"] = {"N": values[":exp"]}
        elif UpdateExpression.startswith("SET #u"):
            item.update(until={"N": values[":until"]}, expires={"N": values[":exp"]})
        else:
            raise AssertionError(UpdateExpression)
        return {"Attributes": dict(item)} if ReturnValues == "ALL_NEW" else {}

    def delete_item(self, TableName, Key):
        self.deleted += 1
        self.item = None


class LambdaTests(Case):
    PIN = "1234"

    def setUp(self):
        class ClientError(Exception):
            def __init__(self, code):
                super().__init__(code)
                self.response = {"Error": {"Code": code}}

        self.error = ClientError
        self.ec2 = Mock()
        self.ddb = FakeDynamo()
        self.ddb.error_class = ClientError
        spec = importlib.util.spec_from_file_location("wake_ec2", HERE / "wake_ec2.py")
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            "boto3": SimpleNamespace(client=lambda service: {"ec2": self.ec2, "dynamodb": self.ddb}[service]),
            "botocore.exceptions": SimpleNamespace(ClientError=ClientError),
        }):
            spec.loader.exec_module(self.module)
        self.enter(patch.dict(os.environ, {
            "INSTANCE_ID": "i-test", "SSH_USER": "ubuntu",
            "WAKE_PIN": self.PIN}))
        self.enter(contextlib.redirect_stdout(io.StringIO()))

    def post(self, payload, **extra):
        event = {"requestContext": {"http": {"method": "POST"}}, "body": json.dumps(payload)}
        event.update(extra)
        result = self.module.lambda_handler(event, None)
        return result["statusCode"], json.loads(result["body"])

    def state(self, name, ip=None):
        instance = {"State": {"Name": name}, "PrivateIpAddress": "10.0.0.5"}
        if ip:
            instance["PublicIpAddress"] = ip
        self.ec2.describe_instances.return_value = {"Reservations": [{"Instances": [instance]}]}

    def form(self, body, accept=None):
        headers = {"content-type": "application/x-www-form-urlencoded"}
        if accept:
            headers["accept"] = accept
        event = {"requestContext": {"http": {"method": "POST"}}, "body": body, "headers": headers}
        result = self.module.lambda_handler(event, None)
        return result["statusCode"], result["body"], result["headers"]["content-type"]

    def states(self, *steps):
        replies = []
        for name, ip in steps:
            instance = {"State": {"Name": name}, "PrivateIpAddress": "10.0.0.5"}
            if ip:
                instance["PublicIpAddress"] = ip
            replies.append({"Reservations": [{"Instances": [instance]}]})
        self.ec2.describe_instances.side_effect = replies

    def get(self, accept):
        event = {"requestContext": {"http": {"method": "GET"}},
                 "headers": {"accept": accept, "host": "abc.lambda-url.test-region.on.aws"}}
        return self.module.lambda_handler(event, None)

    def test_browsers_get_the_page_and_terminals_get_help(self):
        result = self.get("text/html,application/xhtml+xml")
        self.assertIn("text/html", result["headers"]["content-type"])
        self.assertIn("<title>Wake EC2</title>", result["body"])
        result = self.get("*/*")
        self.assertIn("text/plain", result["headers"]["content-type"])
        self.assertIn("curl -d pin=YOUR_PIN -d action=wake "
                      "https://abc.lambda-url.test-region.on.aws/", result["body"])
        self.ec2.describe_instances.assert_not_called()

    def test_terminal_wake_waits_and_prints_readable_progress(self):
        self.enter(patch.object(self.module.time, "sleep"))
        self.states(("stopping", None), ("stopped", None), ("pending", None),
                    ("running", "192.0.2.10"))
        self.ec2.start_instances.return_value = {
            "StartingInstances": [{"CurrentState": {"Name": "pending"}}]}
        status, body, kind = self.form(f"pin={self.PIN}&action=wake")
        self.assertEqual((status, kind.split(";")[0]), (200, "text/plain"))
        for line in ("state: running", "ready: yes", "public_ip: 192.0.2.10",
                     "ssh: ssh ubuntu@192.0.2.10", "progress:", "start requested, now pending"):
            self.assertIn(line, body)
        self.ec2.start_instances.assert_called_once_with(InstanceIds=["i-test"])

    def test_terminal_status_and_errors_are_text(self):
        self.state("stopped")
        status, body, _ = self.form(f"pin={self.PIN}")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith("state: stopped\nready: no\n"))
        self.ec2.start_instances.assert_not_called()
        self.assertEqual(self.form("pin=nope&action=wake")[:2], (403, "error: Wrong PIN\n"))
        self.assertEqual(self.form(f"pin={self.PIN}&action=wake&wait=soon")[0], 400)

    def test_accept_json_and_wait_budget_for_code(self):
        self.enter(patch.object(self.module.time, "sleep"))
        clock = self.enter(patch.object(self.module.time, "monotonic"))
        clock.side_effect = itertools.count(0, 10)
        self.states(("stopped", None), *[("pending", None)] * 10)
        self.ec2.start_instances.return_value = {
            "StartingInstances": [{"CurrentState": {"Name": "pending"}}]}
        status, body, kind = self.form(f"pin={self.PIN}&action=wake&wait=60",
                                       accept="application/json")
        data = json.loads(body)
        self.assertEqual((status, kind, data["state"], data["ready"]),
                         (202, "application/json", "pending", False))
        self.assertIn("Starting", data["message"])

    def test_wrong_or_missing_pin_is_rejected_before_aws_calls(self):
        self.assertEqual(self.post({"action": "wake", "pin": "nope"})[0], 403)
        self.assertEqual(self.post({"action": "wake"})[0], 403)
        os.environ["WAKE_PIN"] = ""
        self.assertEqual(self.post({"action": "wake", "pin": self.PIN})[0], 403)
        self.ec2.describe_instances.assert_not_called()

    def test_other_methods_and_bad_bodies(self):
        put = self.module.lambda_handler({"requestContext": {"http": {"method": "PUT"}}}, None)
        self.assertEqual(put["statusCode"], 405)
        status, _ = self.post({"action": "reboot", "pin": self.PIN})
        self.assertEqual(status, 400)
        bad = {"requestContext": {"http": {"method": "POST"}}, "body": "[1]",
               "headers": {"content-type": "application/json"}}
        self.assertEqual(self.module.lambda_handler(bad, None)["statusCode"], 400)

    def test_status_returns_addresses_and_never_starts(self):
        self.state("stopped")
        status, data = self.post({"action": "status", "pin": self.PIN})
        self.assertEqual((status, data["state"], data["privateIp"]), (200, "stopped", "10.0.0.5"))
        self.ec2.start_instances.assert_not_called()

    def test_wake_starts_a_stopped_instance(self):
        self.state("stopped")
        self.ec2.start_instances.return_value = {
            "StartingInstances": [{"CurrentState": {"Name": "pending"}}]}
        status, data = self.post({"action": "wake", "pin": self.PIN})
        self.assertEqual((status, data["state"]), (202, "pending"))
        self.ec2.start_instances.assert_called_once_with(InstanceIds=["i-test"])

    def test_a_running_instance_is_never_started_again(self):
        self.state("running", "192.0.2.10")
        status, data = self.post({"action": "wake", "pin": self.PIN})
        self.assertEqual((status, data["publicIp"]), (200, "192.0.2.10"))
        self.ec2.start_instances.assert_not_called()

    def test_transitional_states_and_races(self):
        self.state("stopping")
        self.assertEqual(self.post({"action": "wake", "pin": self.PIN})[0], 409)
        self.state("stopped")
        self.ec2.start_instances.side_effect = self.error("IncorrectInstanceState")
        status, data = self.post({"action": "wake", "pin": self.PIN})
        self.assertEqual(status, 202)
        self.assertIn("state changed during start, retrying",
                      [step["event"] for step in data["progress"]])

    def lock_on(self):
        os.environ["LOCK_TABLE"] = "locks"
        self.state("stopped")
        self.ec2.start_instances.return_value = {
            "StartingInstances": [{"CurrentState": {"Name": "pending"}}]}
        return self.enter(patch.object(self.module.time, "time", return_value=1_000_000))

    def wrong(self):
        return self.post({"action": "status", "pin": "0000"})

    def test_only_the_pin_field_is_accepted(self):
        self.state("running", "192.0.2.10")
        self.assertEqual(self.post({"action": "status", "pin": self.PIN})[0], 200)
        for payload, extra in (({"action": "status", "key": self.PIN}, {}),
                               ({"action": "status"}, {"headers": {"x-wake-pin": self.PIN}}),
                               ({"action": "status"}, {"headers": {"X-Wake-Key": self.PIN}})):
            self.assertEqual(self.post(payload, **extra)[0], 403)
        status, data = self.post({}, body="!!not base64!!", isBase64Encoded=True)
        self.assertEqual((status, data["error"]), (400, "Send a JSON or form body"))

    def test_form_body_from_curl_arrives_base64_encoded(self):
        # Lambda Function URLs deliver "curl -d pin=..." base64-encoded; the documented command must work.
        self.state("running", "192.0.2.10")
        encoded = base64.b64encode(("pin=%s&action=status" % self.PIN).encode()).decode()
        event = {"requestContext": {"http": {"method": "POST"}}, "body": encoded, "isBase64Encoded": True,
                 "headers": {"content-type": "application/x-www-form-urlencoded"}}
        result = self.module.lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        self.assertIn("state: running", result["body"])

    def test_page_has_a_pin_field_and_one_tap_bookmark(self):
        page = self.get("text/html")["body"]
        self.assertIn('id="pin"', page)
        self.assertIn("location.hash", page)
        self.assertNotIn("YOUR_WAKE_KEY", page)

    def test_five_wrong_pins_lock_the_page_even_for_the_right_pin(self):
        self.lock_on()
        for left in ("4 tries", "3 tries", "2 tries", "1 try"):
            status, data = self.wrong()
            self.assertEqual(status, 403)
            self.assertIn(f"{left} left before a 15-minute lock", data["error"])
        status, data = self.wrong()
        self.assertEqual((status, data["retryAfterSeconds"]), (429, 900))
        self.assertIn("Try again in 15 minutes", data["error"])
        self.assertEqual(self.post({"action": "wake", "pin": self.PIN})[0], 429)
        self.ec2.describe_instances.assert_not_called()
        self.ec2.start_instances.assert_not_called()

    def test_lock_ends_and_a_right_pin_works_again(self):
        clock = self.lock_on()
        for _ in range(5):
            self.wrong()
        clock.return_value += 15 * 60 - 1
        self.assertEqual(self.post({"action": "status", "pin": self.PIN})[0], 429)
        clock.return_value += 2
        self.assertEqual(self.post({"action": "status", "pin": self.PIN})[0], 200)
        status, data = self.wrong()
        self.assertEqual(status, 403)
        self.assertIn("4 tries left", data["error"])

    def test_a_right_pin_resets_the_wrong_count(self):
        self.lock_on()
        self.wrong()
        self.wrong()
        self.assertEqual(self.post({"action": "status", "pin": self.PIN})[0], 200)
        self.assertEqual(self.ddb.deleted, 1)
        for _ in range(4):
            self.assertEqual(self.wrong()[0], 403)

    def test_old_wrong_tries_do_not_pile_up(self):
        clock = self.lock_on()
        for _ in range(4):
            self.wrong()
        clock.return_value += 16 * 60
        status, data = self.wrong()
        self.assertEqual(status, 403)
        self.assertIn("4 tries left", data["error"])

    def test_counter_failure_fails_closed(self):
        self.lock_on()
        self.ddb.fail = True
        status, data = self.post({"action": "wake", "pin": self.PIN})
        self.assertEqual(status, 503)
        self.assertIn("EC2 console", data["error"])
        self.ec2.start_instances.assert_not_called()


    def test_parallel_guesses_are_counted_before_they_are_compared(self):
        # Every racer passes the lock check at the same moment, as in a burst of parallel requests.
        self.lock_on()
        self.ddb.get_item = lambda **kwargs: {}
        compared = []
        real = self.module.pin_matches
        self.module.pin_matches = lambda pin: compared.append(pin) or real(pin)
        statuses = [self.wrong()[0] for _ in range(12)]
        self.assertEqual(len(compared), 5)
        self.assertEqual(statuses[:4], [403] * 4)
        self.assertTrue(all(code == 429 for code in statuses[5:]))

    def test_an_expired_window_is_restarted_once_not_by_every_request(self):
        clock = self.lock_on()
        for _ in range(3):
            self.wrong()
        clock.return_value += 16 * 60                        # the 15-minute window has ended
        self.assertEqual(self.ddb.item["fails"]["N"], "3")
        first = self.wrong()[1]["error"]
        second = self.wrong()[1]["error"]
        self.assertIn("4 tries left", first)                 # the first request restarted the count at 1
        self.assertIn("3 tries left", second)                # the next one counts on from there
        self.assertEqual(self.ddb.item["fails"]["N"], "2")

    def test_a_failed_counter_write_refuses_the_guess(self):
        self.lock_on()

        def broken(**kwargs):
            raise RuntimeError("write denied")
        self.ddb.update_item = broken
        status, _ = self.post({"action": "wake", "pin": self.PIN})
        self.assertEqual(status, 503)
        self.ec2.start_instances.assert_not_called()


class DocsTests(Case):
    def test_docs_match_the_code(self):
        import check_docs
        self.assertEqual(check_docs.problems(), [], "the docs drifted from the code: python src/check_docs.py")


class SpendAlertDocsTests(Case):
    """The spend alert is written down, not built into the stack."""

    def test_no_budget_resource_in_the_stack(self):
        template = (HERE / "ec2-idle-stack.yaml").read_text(encoding="utf-8")
        self.assertNotIn("AWS::Budgets", template)
        self.assertNotIn("Budget", template.split("\nResources:\n", 1)[1].split("\nOutputs:\n", 1)[0].replace(
            "BackupAlarm", ""))

    def test_the_budget_command_flags_are_allowed_and_used(self):
        import check_docs
        for flag in ("--account-id", "--budget", "--notifications-with-subscribers"):
            self.assertIn(flag, check_docs.FOREIGN_FLAGS)
            self.assertIn(flag, (HERE.parent / "SETUP.md").read_text(encoding="utf-8"))
        self.assertNotIn("--budget", (HERE / "install_guest.sh").read_text(encoding="utf-8"))   # not ours

    def test_the_section_uses_placeholders_and_is_linked(self):
        setup = (HERE.parent / "SETUP.md").read_text(encoding="utf-8")
        section = setup.split("## 💸 Optional: set a spend alert", 1)[1].split("\n## ", 1)[0]
        self.assertIn("YOUR_ACCOUNT_ID", section)
        self.assertIn("<EMAIL>", section)
        self.assertIn("AWS Settings", section)       # the fallback when Budgets isn't available
        self.assertFalse(re.search(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}", section), "an email address in the docs")
        for name in ("README.md", "RUNBOOK.md", "WORKFLOW.md"):
            self.assertIn("SETUP.md#-optional-set-a-spend-alert", (HERE.parent / name).read_text(encoding="utf-8"))

    def test_no_account_numbers_in_any_doc(self):
        for path in sorted(HERE.parent.glob("*.md")):
            with self.subTest(doc=path.name):
                self.assertFalse(re.search(r"(?<![\w.-])\d{12}(?![\w.-])", path.read_text(encoding="utf-8")))


class TemplateTests(Case):
    def test_the_stack_form_stays_small(self):
        template = (HERE / "ec2-idle-stack.yaml").read_text(encoding="utf-8")
        section = template.split("\nParameters:\n", 1)[1].split("\nRules:\n", 1)[0]
        names = [line.strip().rstrip(":") for line in section.splitlines()
                 if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":")]
        self.assertEqual(sorted(names), sorted([
            "InstanceId", "WakePin", "NotificationEmail",       # Basic
            "WorkHoursStart", "WarnMinutes", "StopMinutes", "BackupAlarmHours",
            "EnableSessionManager", "InstanceRoleName", "EbsKmsKeyArn", "SshUser"]))   # Advanced

    def outputs(self):
        template = (HERE / "ec2-idle-stack.yaml").read_text(encoding="utf-8")
        return template.split("\nOutputs:\n", 1)[1]

    def test_lockout_recovery_outputs_exist_and_hold_no_literal_values(self):
        outputs = self.outputs()
        table = outputs.split("  LockTableName:\n", 1)[1].split("\n  ClearLockCommand:", 1)[0]
        command = outputs.split("  ClearLockCommand:\n", 1)[1]
        self.assertIn("Value: !Ref WakeLockTable", table)
        self.assertIn("aws dynamodb delete-item --table-name ${WakeLockTable} --region ${AWS::Region}", command)
        self.assertIn("""--key '{"id": {"S": "wake"}}'""", command)
        self.assertIn('LOCK_KEY = {"id": {"S": "wake"}}', (HERE / "wake_ec2.py").read_text(encoding="utf-8"))
        for text in (table, command):
            self.assertIsNone(re.search(r"\d{12}", text), "an account number")
            self.assertNotRegex(text, r"(?i)\bpin=|WakePin|arn:aws")
            self.assertNotIn("Fn::Sub", text)
        self.assertIn("does not reveal or change the PIN", command)

    def test_existing_outputs_are_unchanged_and_recovery_only_adds_outputs(self):
        names = re.findall(r"^  ([A-Za-z]+):\s*$", self.outputs(), re.M)
        self.assertEqual(names, ["WakePageUrl", "TerminalWakeCommand", "OneTapBookmark", "AlertTopicArn",
                                 "GuestInstallCommand", "ConnectInBrowser", "TemporarySshCommand",
                                 "AttachRoleCommand", "LockTableName", "ClearLockCommand"])

    def test_the_lock_and_pin_rules_are_unchanged(self):
        code = (HERE / "wake_ec2.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("MAX_WRONG_TRIES", "5")', code)
        self.assertIn('os.environ.get("LOCK_MINUTES", "15")', code)
        template = (HERE / "ec2-idle-stack.yaml").read_text(encoding="utf-8")
        self.assertNotIn("MAX_WRONG_TRIES:", template)      # the stack never overrides the defaults
        self.assertNotIn("LOCK_MINUTES:", template)
        pin = re.split(r"\n  [A-Za-z]", template.split("\n  WakePin:\n", 1)[1], maxsplit=1)[0]
        self.assertIn("MinLength: 4", pin)
        self.assertIn("MaxLength: 64", pin)
        pattern = re.search(r'AllowedPattern: "([^"]+)"', pin).group(1)
        self.assertEqual(pattern, "^[A-Za-z0-9]{4,64}$")
        for pin_text, allowed in (("abc", False), ("abcd", True), ("a" * 64, True), ("a" * 65, False),
                                  ("12 34", False), ("12-34", False), ("", False)):
            with self.subTest(pin=len(pin_text), text=pin_text[:6]):
                self.assertEqual(bool(re.match(pattern, pin_text)), allowed)

    def test_the_backup_alarm_is_still_off_by_default(self):
        template = (HERE / "ec2-idle-stack.yaml").read_text(encoding="utf-8")
        section = re.split(r"\n  [A-Za-z]", template.split("\n  BackupAlarmHours:\n", 1)[1], maxsplit=1)[0]
        self.assertIn("Default: 0\n", section + "\n")

    def test_template_embeds_the_current_lambda_code(self):
        lines = (HERE / "ec2-idle-stack.yaml").read_text(encoding="utf-8").splitlines()
        start = next(i for i, line in enumerate(lines) if line.strip() == "ZipFile: |") + 1
        block = []
        for line in lines[start:]:
            if line and not line.startswith("          "):
                break
            block.append(line)
        embedded = textwrap.dedent("\n".join(block)).strip("\n")
        source = (HERE / "wake_ec2.py").read_text(encoding="utf-8").strip("\n")
        self.assertEqual(embedded, source,
                         "Run: python sync_template.py")


if __name__ == "__main__":
    unittest.main()
