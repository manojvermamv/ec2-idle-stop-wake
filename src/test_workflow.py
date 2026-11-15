"""Run with: python -B -m unittest discover -s src -v  (Python 3.8 or newer)."""

import base64
import contextlib
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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
        self.env = {"IDLE_MODE": "dry-run", "WARN_MINUTES": "1", "STOP_MINUTES": "2",
                    "WORK_PROBE": "", "SNS_TOPIC_ARN": "arn:aws:sns:test-region:1:topic",
                    "WAKE_PAGE_URL": "https://wake.example/", "INSTANCE_NAME": "dev-box"}
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

    def test_observed_idle_warns_once_then_simulates_stop(self):
        self.assertEqual(self.check(1000)["since"], 1000)
        self.assertEqual(self.check(1060)["warned"], 1060)
        self.assertEqual(self.check(1120)["stop_requested"], 1120)
        self.check(1180)
        self.assertEqual(self.output.getvalue().count("Would publish warning"), 1)
        self.assertEqual(self.output.getvalue().count("Would call EC2 StopInstances"), 1)
        self.publish.assert_not_called()
        self.stop.assert_not_called()
        self.context.assert_not_called()

    def test_busy_cancels_warning_and_resets_idle(self):
        self.check(1000)
        self.check(1060)
        self.activity.return_value = "remote login session"
        self.assertEqual(self.check(1119), {})
        self.activity.return_value = None
        self.assertEqual(self.check(1120)["since"], 1120)
        self.assertNotIn("Would call EC2 StopInstances", self.output.getvalue())

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

    def test_stop_mode_publishes_before_stop(self):
        self.env["IDLE_MODE"] = "stop"
        calls = Mock()
        calls.attach_mock(self.publish, "publish")
        calls.attach_mock(self.stop, "stop")
        for now in (1000, 1060, 1120):
            self.check(now)
        self.assertEqual([name for name, _, _ in calls.mock_calls], ["publish", "stop", "publish"])
        (_, _, warn_subject, warn_body), (_, _, stop_subject, stop_body) = [
            c.args for c in self.publish.call_args_list]
        self.assertEqual(warn_subject, "EC2 dev-box (i-test) will stop in 1 minute")
        self.assertIn("has had no activity for 1 minute.", warn_body)
        self.assertEqual(stop_subject, "EC2 dev-box (i-test) has stopped")
        self.assertIn("https://wake.example/", stop_body)
        self.assertIn("curl -d pin=YOUR_PIN -d action=wake https://wake.example/", stop_body)
        for text in (warn_subject, stop_subject):
            self.assertTrue(text.isascii() and len(text) < 100)

    def test_one_warning_per_hour_even_across_idle_cycles(self):
        self.env["IDLE_MODE"] = "stop"
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
        warnings = [c for c in self.publish.call_args_list if "will stop" in c.args[2]]
        self.assertEqual(len(warnings), 2)

    def test_stopped_email_is_sent_once_and_never_blocks_the_stop(self):
        self.env["IDLE_MODE"] = "stop"
        for now in (1000, 1060):
            self.check(now)
        self.publish.side_effect = RuntimeError("SNS down")
        self.check(1120)                       # stop requested although the email failed
        self.stop.assert_called_once()
        self.publish.side_effect = None
        for now in range(1180, 1780, 60):      # still running 10 min later: stop is retried
            self.check(now)
        self.assertEqual(self.stop.call_count, 2)
        stopped = [c for c in self.publish.call_args_list if "has stopped" in c.args[2]]
        self.assertEqual(len(stopped), 1)

    def test_delayed_email_preserves_full_warning_window(self):
        self.env["IDLE_MODE"] = "stop"
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
        self.env.update(IDLE_MODE="stop", WARN_RETRIES="2")
        self.check(1000)
        self.publish.side_effect = RuntimeError("topic deleted")
        with self.assertRaises(RuntimeError):
            self.check(1060)
        self.assertEqual(self.check(1120)["warned"], 1120)
        self.check(1180)
        self.stop.assert_called_once()

    def test_without_topic_stops_without_email(self):
        self.env.update(IDLE_MODE="stop", SNS_TOPIC_ARN="")
        for now in (1000, 1060, 1120):
            self.check(now)
        self.publish.assert_not_called()
        self.stop.assert_called_once()

    def test_stop_is_retried_if_the_instance_keeps_running(self):
        self.env["IDLE_MODE"] = "stop"
        for now in (1000, 1060, 1120):
            self.check(now)
        for now in range(1180, 1720, 60):
            self.check(now)
        self.assertEqual(self.stop.call_count, 1)
        self.check(1720)
        self.assertEqual(self.stop.call_count, 2)

    def test_mode_change_restarts_the_idle_clock(self):
        self.check(1000)
        self.env["IDLE_MODE"] = "stop"
        self.assertEqual(self.check(1060)["since"], 1060)

    def test_invalid_config_is_rejected(self):
        for change in ({"IDLE_MODE": "maybe"}, {"WARN_MINUTES": "5", "STOP_MINUTES": "5"},
                       {"SNS_TOPIC_ARN": "topic"}, {"WARN_RETRIES": "0"},
                       {"WAKE_PAGE_URL": "http://insecure.example/"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ec2_idle.load_config({**self.env, **change})


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
        body = connection.request.call_args.kwargs["body"].decode()
        self.assertIn("DryRun=true", body)
        self.assertIn("InstanceId.1=i-test", body)


class CheckTests(Case):
    def setUp(self):
        self.enter(contextlib.redirect_stdout(io.StringIO()))
        self.enter(patch.object(ec2_idle, "ssm_session", return_value=False))
        self.enter(patch.object(ec2_idle, "instance_context", return_value={
            "instance_id": "i-test", "region": "test-region", "role": "test-role"}))
        self.cfg = ec2_idle.load_config({"IDLE_MODE": "stop"})

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
        self.args = SimpleNamespace(url="https://wake.example", pin="k", timeout=300,
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
        self.assertEqual({c.args[1] for c in call.call_args_list}, {"wake"})
        self.assertEqual(connect.call_count, 2)

    def test_no_wake_only_reads_status(self):
        self.args.no_wake = True
        with patch.object(ec2_up, "call", return_value=(200, {"state": "stopped"})) as call, \
                self.assertRaisesRegex(RuntimeError, "no-wake"):
            ec2_up.wait_for_ssh(self.args)
        call.assert_called_once_with(self.args, "status")

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
            listener.settimeout(10)

            def server():
                with listener.accept()[0] as connection:
                    connection.settimeout(10)
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
                                    timeout=10, check=True, cwd=HERE)
            worker.join(timeout=10)
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
                    ExpressionAttributeValues, ReturnValues="NONE"):
        if self.fail:
            raise RuntimeError("DynamoDB is down")
        values = {name: value["N"] for name, value in ExpressionAttributeValues.items()}
        item = self.item = self.item or {}
        if UpdateExpression.startswith("ADD #f"):
            item["fails"] = {"N": str(int(item.get("fails", {"N": "0"})["N"]) + 1)}
            item.setdefault("since", {"N": values[":now"]})
            item["expires"] = {"N": values[":exp"]}
        elif UpdateExpression == "SET #f = :one, #s = :now":
            item["fails"], item["since"] = {"N": values[":one"]}, {"N": values[":now"]}
        elif UpdateExpression.startswith("SET #u"):
            item.update(until={"N": values[":until"]}, fails={"N": values[":zero"]},
                        expires={"N": values[":exp"]})
            item.pop("since", None)
        else:
            raise AssertionError(UpdateExpression)
        return {"Attributes": dict(item)} if ReturnValues == "ALL_NEW" else {}

    def delete_item(self, TableName, Key):
        self.deleted += 1
        self.item = None


class LambdaTests(Case):
    PIN = "4821"

    def setUp(self):
        class ClientError(Exception):
            def __init__(self, code):
                super().__init__(code)
                self.response = {"Error": {"Code": code}}

        self.error = ClientError
        self.ec2 = Mock()
        self.ddb = FakeDynamo()
        spec = importlib.util.spec_from_file_location("wake_ec2", HERE / "wake_ec2.py")
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            "boto3": SimpleNamespace(client=lambda service: {"ec2": self.ec2, "dynamodb": self.ddb}[service]),
            "botocore.exceptions": SimpleNamespace(ClientError=ClientError),
        }):
            spec.loader.exec_module(self.module)
        self.enter(patch.dict(os.environ, {
            "INSTANCE_ID": "i-test", "DRY_RUN": "false", "SSH_USER": "ubuntu",
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

    def test_wake_with_header_key_and_base64_body(self):
        self.state("running", "192.0.2.10")
        body = base64.b64encode(json.dumps({"action": "wake"}).encode()).decode()
        status, data = self.post({}, body=body, isBase64Encoded=True,
                                 headers={"X-Wake-Key": self.PIN})
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

    def test_dry_run_checks_permission_only(self):
        os.environ["DRY_RUN"] = "true"
        self.state("stopped")
        self.ec2.start_instances.side_effect = self.error("DryRunOperation")
        status, data = self.post({"action": "wake", "pin": self.PIN})
        self.assertEqual((status, data["dryRun"]), (200, True))
        self.ec2.start_instances.assert_called_once_with(InstanceIds=["i-test"], DryRun=True)

    def lock_on(self):
        os.environ["LOCK_TABLE"] = "locks"
        self.state("stopped")
        self.ec2.start_instances.return_value = {
            "StartingInstances": [{"CurrentState": {"Name": "pending"}}]}
        return self.enter(patch.object(self.module.time, "time", return_value=1_000_000))

    def wrong(self):
        return self.post({"action": "status", "pin": "0000"})

    def test_pin_aliases_and_one_tap_page(self):
        self.state("running", "192.0.2.10")
        for payload, extra in (({"action": "status", "key": self.PIN}, {}),
                               ({"action": "status"}, {"headers": {"x-wake-pin": self.PIN}}),
                               ({"action": "status"}, {"headers": {"X-Wake-Key": self.PIN}})):
            self.assertEqual(self.post(payload, **extra)[0], 200)
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


class TemplateTests(Case):
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
