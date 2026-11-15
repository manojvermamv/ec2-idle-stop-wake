#!/usr/bin/env python3
"""Stop this EC2 instance after it stays idle, with an email warning first.

ec2-idle.timer runs this once a minute. It needs Python 3.8 or newer, systemd and
an instance role. AWS calls are signed with the standard library, so the AWS CLI
is not required. Run with --check once after installing to verify the host.
"""

import argparse
import datetime
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.parse

IMDS_HOST = "169.254.169.254"
MAX_GAP_SECONDS = 150
STOP_RETRY_SECONDS = 600


class AwsError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


def run(*args, timeout=15):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


# Activity -----------------------------------------------------------------

def remote_sessions():
    """Count logged-in remote sessions: SSH, VS Code, Cursor and Instance Connect.

    Only authenticated logins create logind sessions, so internet scanners that
    open TCP connections to the SSH port do not count as activity.
    """
    listing = run("loginctl", "list-sessions", "--no-legend")
    listing.check_returncode()
    count = 0
    for line in listing.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        info = run("loginctl", "show-session", fields[0], "--property=Remote", "--property=State")
        if info.returncode:
            continue
        props = dict(item.split("=", 1) for item in info.stdout.splitlines() if "=" in item)
        if props.get("Remote") == "yes" and props.get("State") != "closing":
            count += 1
    return count


def ssm_session():
    """Return True while an AWS Systems Manager Session Manager session is open."""
    result = run("pgrep", "-f", "ssm-session-worker")
    if result.returncode not in (0, 1):
        raise RuntimeError(f"pgrep exited {result.returncode}")
    return result.returncode == 0


def busy_reason(probe, check_sessions=True):
    if check_sessions:
        if remote_sessions():
            return "remote login session"
        if ssm_session():
            return "Session Manager session"
    if probe:
        result = run(probe)
        if result.returncode == 0:
            return "workload"
        if result.returncode != 1:
            raise RuntimeError(f"workload probe exited {result.returncode}")
    return None


# AWS access without the AWS CLI -------------------------------------------

def _sha256(data):
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def _hmac(key, text):
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def sign_request(method, host, query, body, headers, credentials, region, service, amz_date):
    """Return the request headers with an AWS Signature Version 4 Authorization header."""
    headers = {name.lower(): value.strip() for name, value in headers.items()}
    headers["host"] = host
    headers["x-amz-date"] = amz_date
    if credentials.get("Token"):
        headers["x-amz-security-token"] = credentials["Token"]
    names = sorted(headers)
    signed_headers = ";".join(names)
    canonical = "\n".join([
        method, "/", query, "".join(f"{name}:{headers[name]}\n" for name in names),
        signed_headers, _sha256(body),
    ])
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, _sha256(canonical)])
    key = ("AWS4" + credentials["SecretAccessKey"]).encode("utf-8")
    for part in (amz_date[:8], region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={credentials['AccessKeyId']}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}")
    return headers


def _imds(connection, method, path, headers):
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    data = response.read().decode("utf-8")
    if response.status != 200:
        raise RuntimeError(f"Instance metadata {path} returned HTTP {response.status}")
    return data


def instance_context():
    """Read this instance's ID, Region and role credentials from IMDSv2."""
    connection = http.client.HTTPConnection(IMDS_HOST, timeout=3)
    try:
        token = _imds(connection, "PUT", "/latest/api/token",
                      {"X-aws-ec2-metadata-token-ttl-seconds": "300"})
        headers = {"X-aws-ec2-metadata-token": token}
        identity = json.loads(_imds(connection, "GET",
                                    "/latest/dynamic/instance-identity/document", headers))
        try:
            domain = _imds(connection, "GET", "/latest/meta-data/services/domain", headers).strip()
        except RuntimeError:
            domain = "amazonaws.com"
        try:
            role = _imds(connection, "GET", "/latest/meta-data/iam/security-credentials/",
                         headers).split()[0]
        except (RuntimeError, IndexError):
            raise RuntimeError("No IAM role is attached to this instance") from None
        credentials = json.loads(_imds(
            connection, "GET", f"/latest/meta-data/iam/security-credentials/{role}", headers))
    finally:
        connection.close()
    return {"instance_id": identity["instanceId"], "region": identity["region"],
            "domain": domain or "amazonaws.com", "role": role, "credentials": credentials}


def aws_call(ctx, service, region, params):
    host = f"{service}.{region}.{ctx['domain']}"
    body = urllib.parse.urlencode(params)
    amz_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = sign_request("POST", host, "", body,
                           {"content-type": "application/x-www-form-urlencoded; charset=utf-8"},
                           ctx["credentials"], region, service, amz_date)
    connection = http.client.HTTPSConnection(host, timeout=20)
    try:
        connection.request("POST", "/", body=body.encode("utf-8"), headers=headers)
        response = connection.getresponse()
        data = response.read().decode("utf-8", "replace")
    finally:
        connection.close()
    if response.status == 200:
        return data
    code = re.search(r"<Code>([^<]+)</Code>", data)
    message = re.search(r"<Message>([^<]*)</Message>", data)
    raise AwsError(code.group(1) if code else f"HTTP {response.status}",
                   message.group(1) if message else data[:200])


def publish(ctx, topic_arn, subject, message):
    region = topic_arn.split(":")[3] or ctx["region"]
    aws_call(ctx, "sns", region, {"Action": "Publish", "Version": "2010-03-31",
                                  "TopicArn": topic_arn, "Subject": subject, "Message": message})


def stop_instance(ctx, dry_run=False):
    params = {"Action": "StopInstances", "Version": "2016-11-15",
              "InstanceId.1": ctx["instance_id"]}
    if dry_run:
        params["DryRun"] = "true"
    aws_call(ctx, "ec2", ctx["region"], params)


# Idle clock ---------------------------------------------------------------

def advance(state, now, busy):
    if busy:
        return {}
    if not 0 <= now - state.get("last", -10 * MAX_GAP_SECONDS) <= MAX_GAP_SECONDS:
        state = {"since": now, "warned": None}
    return {**state, "last": now}


def record(path, state):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state), encoding="utf-8")
    temp.replace(path)


def load_config(env):
    cfg = SimpleNamespace(
        mode=env.get("IDLE_MODE", "dry-run"),
        warn=int(env.get("WARN_MINUTES", "30")) * 60,
        stop=int(env.get("STOP_MINUTES", "45")) * 60,
        retries=int(env.get("WARN_RETRIES", "3")),
        topic=env.get("SNS_TOPIC_ARN", "").strip(),
        probe=env.get("WORK_PROBE", "").strip(),
        sessions=env.get("EC2_IDLE_IGNORE_SESSIONS") != "1",
    )
    if cfg.mode not in {"dry-run", "stop"} or not 0 < cfg.warn < cfg.stop or cfg.retries < 1:
        raise ValueError("Check IDLE_MODE, WARN_MINUTES, STOP_MINUTES and WARN_RETRIES")
    if cfg.topic and not cfg.topic.startswith("arn:"):
        raise ValueError("SNS_TOPIC_ARN must be an SNS topic ARN or empty")
    return cfg


def watch(cfg, state_path):
    def activity():
        try:
            return busy_reason(cfg.probe, cfg.sessions)
        except (OSError, subprocess.SubprocessError, RuntimeError):
            record(state_path, {})
            raise

    busy = activity()
    try:
        previous = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        previous = {}
    if not isinstance(previous, dict) or previous.get("mode") != cfg.mode:
        previous = {}
    now = time.monotonic()
    state = advance(previous, now, busy)
    if busy:
        record(state_path, state)
        print(f"Busy ({busy}). Idle clock reset.")
        return

    state["mode"] = cfg.mode
    elapsed = now - state["since"]
    record(state_path, state)
    print(f"Idle for {elapsed / 60:.1f} minutes ({cfg.mode}).", flush=True)

    ctx = None
    if elapsed >= cfg.warn and state.get("warned") is None:
        if cfg.mode == "stop":
            ctx = instance_context()
        name = ctx["instance_id"] if ctx else "This EC2 instance"
        message = (f"{name} has been idle for {cfg.warn // 60} minutes. It will stop in "
                   f"{(cfg.stop - cfg.warn) // 60} minutes unless you reconnect or start work. "
                   "Your files stay on the disk. Use your wake page to start it again.")
        if cfg.mode != "stop":
            print(f"Dry run. Would publish warning. {message}")
        elif not cfg.topic:
            print("No SNS topic configured. Continuing without a warning email.")
        else:
            try:
                publish(ctx, cfg.topic, "EC2 idle warning", message)
                print("Warning email accepted by SNS.")
            except (OSError, RuntimeError) as error:
                state["warn_failures"] = state.get("warn_failures", 0) + 1
                if state["warn_failures"] < cfg.retries:
                    record(state_path, state)
                    raise
                print(f"Warning email failed {state['warn_failures']} times ({error}). "
                      "Continuing without it.", file=sys.stderr)
        state["warned"] = time.monotonic()
        record(state_path, state)

    requested = state.get("stop_requested")
    if (elapsed < cfg.stop or state.get("warned") is None
            or now - state["warned"] < cfg.stop - cfg.warn
            or (requested is not None and now - requested < STOP_RETRY_SECONDS)):
        return
    if cfg.mode == "stop" and ctx is None:
        ctx = instance_context()
    busy = activity()
    if busy:
        record(state_path, {})
        print(f"Busy ({busy}) on final check. Idle clock reset.")
        return
    if cfg.mode == "stop":
        stop_instance(ctx)
        print("Requested EC2 StopInstances.")
    else:
        print("Dry run. Would call EC2 StopInstances.")
    state["stop_requested"] = now
    record(state_path, state)


def check(cfg, expect_session=False, test_email=False):
    """One-time setup check. Returns a process exit code."""
    problems = []

    def ok(text):
        print(f"OK    {text}")

    def fail(text):
        problems.append(text)
        print(f"FAIL  {text}")

    print(f"Mode {cfg.mode}. Warn after {cfg.warn // 60} min, stop after {cfg.stop // 60} min.")
    try:
        sessions = remote_sessions()
        ok(f"Login session check works ({sessions} remote session(s) now)")
        if expect_session and not sessions:
            fail("Your current SSH session was not detected. sshd must use PAM "
                 "(UsePAM yes) with pam_systemd, which is the Ubuntu default.")
    except (OSError, subprocess.SubprocessError) as error:
        fail(f"loginctl session check failed: {error}")
    try:
        ok(f"Session Manager check works (open: {'yes' if ssm_session() else 'no'})")
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        fail(f"Session Manager check failed: {error}")
    if cfg.probe:
        try:
            code = run(cfg.probe).returncode
            if code in (0, 1):
                ok(f"Workload probe works (reports {'busy' if code == 0 else 'idle'})")
            else:
                fail(f"Workload probe exited {code}. Use 0 for busy and 1 for idle.")
        except (OSError, subprocess.SubprocessError) as error:
            fail(f"Workload probe failed: {error}")

    try:
        ctx = instance_context()
        ok(f"Instance {ctx['instance_id']} in {ctx['region']} with role {ctx['role']}")
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        fail(f"Instance metadata or role: {error}. Run this on the EC2 instance itself; if you "
             "just attached the role, wait a minute and try again.")
        ctx = None
    if ctx:
        try:
            stop_instance(ctx, dry_run=True)
            fail("StopInstances dry run did not return DryRunOperation")
        except AwsError as error:
            if error.code == "DryRunOperation":
                ok("Role can stop this instance (DryRunOperation)")
            else:
                fail(f"Role cannot stop this instance: {error}")
        except OSError as error:
            fail(f"Could not reach the EC2 API: {error}")
        if not cfg.topic:
            print("NOTE  No SNS topic set. The instance will stop without a warning email.")
        elif test_email:
            try:
                publish(ctx, cfg.topic, "EC2 idle workflow test",
                        "Test message. Idle warnings for this instance will arrive like this.")
                ok("Test email accepted by SNS. Confirm the subscription email if you have not.")
            except (OSError, RuntimeError) as error:
                fail(f"SNS publish failed: {error}")
        else:
            ok("SNS topic set. Add --test-email to send a test message.")

    if problems:
        print(f"{len(problems)} problem(s) found.")
        return 1
    print("All checks passed.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify this host and exit")
    parser.add_argument("--expect-session", action="store_true",
                        help="with --check, fail unless a remote login session is detected")
    parser.add_argument("--test-email", action="store_true",
                        help="with --check, send a test message to SNS_TOPIC_ARN")
    args = parser.parse_args(argv)
    cfg = load_config(os.environ)
    if args.check or args.test_email:
        return check(cfg, args.expect_session, args.test_email)
    watch(cfg, Path(os.environ.get("RUNTIME_DIRECTORY", "/run/ec2-idle")) / "state.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
