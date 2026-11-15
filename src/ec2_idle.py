#!/usr/bin/env python3
"""Stop this EC2 instance after it stays idle, with one warning email first.

ec2-idle.timer runs this once a minute. It needs Python 3.8 or newer, systemd and
an instance role. AWS calls are signed with the standard library, so the AWS CLI
is not required. Run with --check once after installing to verify the host.

Built to stay light on small or busy hosts: a normal check starts no other
programs (sessions come from systemd's sd-login library, Session Manager from
/proc), and the network code is imported only when an email or stop is due.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

IMDS_HOST = "169.254.169.254"
MAX_GAP_SECONDS = 300          # tolerate delayed runs on a busy host
STOP_RETRY_SECONDS = 600
PROC_ROOT = "/proc"
SSM_WORKER = b"ssm-session-wor"  # /proc/<pid>/comm is cut to 15 characters


class AwsError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


def run(*args, timeout=15):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


# Activity -----------------------------------------------------------------

def count_remote(session_ids, is_remote, get_state):
    """Count sessions that are remote and not closing."""
    return sum(1 for sid in session_ids if is_remote(sid) and get_state(sid) != "closing")


def _sessions_sd_login():
    """Read logind sessions through libsystemd (no new process)."""
    import ctypes
    lib = ctypes.CDLL("libsystemd.so.0")
    lib.sd_get_sessions.argtypes = [ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p))]
    lib.sd_session_is_remote.argtypes = [ctypes.c_char_p]
    lib.sd_session_get_state.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p)]
    array = ctypes.POINTER(ctypes.c_char_p)()
    total = lib.sd_get_sessions(ctypes.byref(array))
    if total < 0:
        raise OSError(-total, "sd_get_sessions failed")
    ids = [array[i] for i in range(total)]

    def get_state(sid):
        state = ctypes.c_char_p()
        if lib.sd_session_get_state(sid, ctypes.byref(state)) < 0 or not state.value:
            return "closing"  # the session ended between the two calls
        return state.value.decode()

    return count_remote(ids, lambda sid: lib.sd_session_is_remote(sid) > 0, get_state)


def _sessions_loginctl():
    """Fallback for hosts without libsystemd: ask loginctl."""
    listing = run("loginctl", "list-sessions", "--no-legend")
    listing.check_returncode()
    props = {}
    for line in listing.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        info = run("loginctl", "show-session", fields[0], "--property=Remote", "--property=State")
        if info.returncode == 0:
            props[fields[0]] = dict(item.split("=", 1) for item in info.stdout.splitlines()
                                    if "=" in item)
    return count_remote(props, lambda sid: props[sid].get("Remote") == "yes",
                        lambda sid: props[sid].get("State"))


def remote_sessions():
    """Count logged-in remote sessions: SSH, VS Code, Cursor and Instance Connect.

    Only authenticated logins create logind sessions, so internet scanners that
    open TCP connections to the SSH port do not count as activity.
    """
    try:
        return _sessions_sd_login()
    except (OSError, AttributeError):
        return _sessions_loginctl()


def ssm_session():
    """Return True while an AWS Systems Manager Session Manager session is open."""
    if not os.path.isdir(PROC_ROOT):
        result = run("pgrep", "-f", "ssm-session-worker")
        if result.returncode not in (0, 1):
            raise RuntimeError(f"pgrep exited {result.returncode}")
        return result.returncode == 0
    with os.scandir(PROC_ROOT) as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(os.path.join(entry.path, "comm"), "rb") as comm:
                    if comm.read(len(SSM_WORKER)) == SSM_WORKER:
                        return True
            except OSError:
                continue  # the process ended while scanning
    return False


def busy_reason(probe, check_sessions=True):
    if check_sessions:
        if remote_sessions():
            return "remote login session"
        if ssm_session():
            return "Session Manager session"
    if probe:
        result = run(probe, timeout=30)
        if result.returncode == 0:
            return "workload"
        if result.returncode != 1:
            raise RuntimeError(f"workload probe exited {result.returncode}")
    return None


# AWS access without the AWS CLI (imported only when needed) ---------------

def _sha256(data):
    import hashlib
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def _hmac(key, text):
    import hashlib
    import hmac
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def sign_request(method, host, query, body, headers, credentials, region, service, amz_date):
    """Return the request headers with an AWS Signature Version 4 Authorization header."""
    import hashlib
    import hmac
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
    """Read this instance's ID, Region, Name tag and role credentials from IMDSv2."""
    import http.client
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
        try:  # works only when "instance tags in metadata" is enabled
            name = _imds(connection, "GET", "/latest/meta-data/tags/instance/Name", headers).strip()
        except RuntimeError:
            name = ""
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
            "domain": domain or "amazonaws.com", "name": name, "role": role,
            "credentials": credentials}


def aws_call(ctx, service, region, params):
    import datetime
    import http.client
    import re
    import urllib.parse
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
                                  "TopicArn": topic_arn, "Subject": subject[:99],
                                  "Message": message})


def stop_instance(ctx, dry_run=False):
    params = {"Action": "StopInstances", "Version": "2016-11-15",
              "InstanceId.1": ctx["instance_id"]}
    if dry_run:
        params["DryRun"] = "true"
    aws_call(ctx, "ec2", ctx["region"], params)


# Emails ---------------------------------------------------------------------

SIGNATURE = "\n--\nSent by EC2 Idle Stop and Wake, running on the instance itself.\n"


def minutes(seconds):
    count = seconds // 60
    return f"{count} minute" if count == 1 else f"{count} minutes"


def label(cfg, ctx):
    """A friendly name for emails, for example 'ubuntu-cp (i-0abc...)'."""
    name = cfg.name or (ctx or {}).get("name", "")
    instance = (ctx or {}).get("instance_id", "your EC2 instance")
    return f"{name} ({instance})" if name else instance


def warning_email(cfg, ctx):
    who = label(cfg, ctx)
    left = minutes(cfg.stop - cfg.warn)
    subject = f"EC2 {who} will stop in {left}"
    body = (
        f"Hello,\n\n"
        f"Your EC2 instance {who} in {ctx['region']} has had no activity for "
        f"{minutes(cfg.warn)}.\n\n"
        f"It will stop automatically in about {left} to save costs.\n\n"
        f"- To keep it running: connect to it again (SSH, VS Code, Cursor or EC2 Instance Connect).\n"
        f"- To let it stop: do nothing. Your files stay safe on its disk.\n"
    )
    if cfg.wake_url:
        body += f"\nAfter it stops, you can start it again from your wake page:\n{cfg.wake_url}\n"
    return subject, body + SIGNATURE


def stopped_email(cfg, ctx, now_text):
    who = label(cfg, ctx)
    subject = f"EC2 {who} has stopped"
    body = (
        f"Hello,\n\n"
        f"Your EC2 instance {who} in {ctx['region']} stopped at {now_text} after "
        f"{minutes(cfg.stop)} without activity.\n\n"
        f"- Compute charges have stopped. Disk storage is still billed.\n"
        f"- Your files, programs and settings are safe on its disk.\n\n"
    )
    if cfg.wake_url:
        body += (
            f"To start it again, open your wake page and tap Wake:\n{cfg.wake_url}\n\n"
            f"Or run this in any terminal (Windows PowerShell: use curl.exe):\n"
            f"curl -d key=YOUR_WAKE_KEY -d action=wake {cfg.wake_url}\n"
        )
    else:
        body += "To start it again, use your wake page or the EC2 console (Instance state > Start).\n"
    return subject, body + SIGNATURE


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


def load(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config(env):
    cfg = SimpleNamespace(
        mode=env.get("IDLE_MODE", "dry-run"),
        warn=int(env.get("WARN_MINUTES", "30")) * 60,
        stop=int(env.get("STOP_MINUTES", "45")) * 60,
        retries=int(env.get("WARN_RETRIES", "3")),
        email_gap=int(env.get("WARN_EMAIL_GAP_MINUTES", "60")) * 60,
        topic=env.get("SNS_TOPIC_ARN", "").strip(),
        probe=env.get("WORK_PROBE", "").strip(),
        wake_url=env.get("WAKE_PAGE_URL", "").strip(),
        name=env.get("INSTANCE_NAME", "").strip(),
        sessions=env.get("EC2_IDLE_IGNORE_SESSIONS") != "1",
    )
    if (cfg.mode not in {"dry-run", "stop"} or not 0 < cfg.warn < cfg.stop or cfg.retries < 1
            or cfg.email_gap < 0):
        raise ValueError("Check IDLE_MODE, WARN_MINUTES, STOP_MINUTES, WARN_RETRIES and "
                         "WARN_EMAIL_GAP_MINUTES")
    if cfg.topic and not cfg.topic.startswith("arn:"):
        raise ValueError("SNS_TOPIC_ARN must be an SNS topic ARN or empty")
    if cfg.wake_url and not cfg.wake_url.startswith("https://"):
        raise ValueError("WAKE_PAGE_URL must start with https:// or be empty")
    return cfg


def send_warning(cfg, ctx, state, emails, emails_path, state_path):
    """Send the warning email at most once per WARN_EMAIL_GAP_MINUTES."""
    last = emails.get("warned_at")
    if last is not None and time.time() - last < cfg.email_gap:
        print(f"Warning email skipped: one was sent {(time.time() - last) / 60:.0f} minutes ago "
              f"(limit: one per {cfg.email_gap // 60} minutes).")
        return
    if cfg.mode != "stop":
        print(f"Dry run. Would publish warning. {warning_email(cfg, ctx or {'region': '-'})[0]}")
        return
    if not cfg.topic:
        print("No SNS topic configured. Continuing without a warning email.")
        return
    try:
        publish(ctx, cfg.topic, *warning_email(cfg, ctx))
        emails["warned_at"] = time.time()
        record(emails_path, emails)
        print("Warning email accepted by SNS.")
    except (OSError, RuntimeError) as error:
        state["warn_failures"] = state.get("warn_failures", 0) + 1
        if state["warn_failures"] < cfg.retries:
            record(state_path, state)
            raise
        print(f"Warning email failed {state['warn_failures']} times ({error}). "
              "Continuing without it.", file=sys.stderr)


def send_stopped(cfg, ctx, emails, emails_path):
    """Send one 'stopped' email per stop. Failures never block the stop."""
    if emails.get("stopped_sent") or not cfg.topic:
        return
    import datetime
    now_text = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M UTC on %d %b %Y")
    try:
        publish(ctx, cfg.topic, *stopped_email(cfg, ctx, now_text))
        print("Stopped email accepted by SNS.")
    except (OSError, RuntimeError) as error:
        print(f"Stopped email failed ({error}). The stop was still requested.", file=sys.stderr)
    emails["stopped_sent"] = True
    record(emails_path, emails)


def watch(cfg, state_path):
    emails_path = state_path.with_name("emails.json")

    def activity():
        try:
            return busy_reason(cfg.probe, cfg.sessions)
        except (OSError, subprocess.SubprocessError, RuntimeError):
            record(state_path, {})
            raise

    busy = activity()
    previous = load(state_path)
    if previous.get("mode") != cfg.mode:
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
        emails = load(emails_path)
        if cfg.mode == "stop" and cfg.topic:
            ctx = instance_context()
        send_warning(cfg, ctx, state, emails, emails_path, state_path)
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
        send_stopped(cfg, ctx, load(emails_path), emails_path)
    else:
        print("Dry run. Would call EC2 StopInstances and send the stopped email.")
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
        fail(f"Login session check failed: {error}")
    try:
        ok(f"Session Manager check works (open: {'yes' if ssm_session() else 'no'})")
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        fail(f"Session Manager check failed: {error}")
    if cfg.probe:
        try:
            code = run(cfg.probe, timeout=30).returncode
            if code in (0, 1):
                ok(f"Workload probe works (reports {'busy' if code == 0 else 'idle'})")
            else:
                fail(f"Workload probe exited {code}. Use 0 for busy and 1 for idle.")
        except (OSError, subprocess.SubprocessError) as error:
            fail(f"Workload probe failed: {error}")

    try:
        ctx = instance_context()
        ok(f"Instance {label(cfg, ctx)} in {ctx['region']} with role {ctx['role']}")
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
            print("NOTE  No SNS topic set. The instance will stop without emails.")
        elif test_email:
            try:
                publish(ctx, cfg.topic, f"EC2 {label(cfg, ctx)}: idle stop is set up",
                        f"Hello,\n\nIdle stop is now set up on {label(cfg, ctx)} in "
                        f"{ctx['region']}.\n\n"
                        f"- You'll get one warning email after {minutes(cfg.warn)} without "
                        f"activity (at most one per {minutes(cfg.email_gap)}).\n"
                        f"- You'll get one email when it stops, after {minutes(cfg.stop)} "
                        f"without activity"
                        + (", with your wake page link.\n" if cfg.wake_url else ".\n")
                        + SIGNATURE)
                ok("Test email accepted by SNS. Confirm the subscription email if you have not.")
            except (OSError, RuntimeError) as error:
                fail(f"SNS publish failed: {error}")
        else:
            ok("SNS topic set. Add --test-email to send a test message.")
        if cfg.topic and not cfg.wake_url:
            print("NOTE  No wake page link set. Add --wake-url to include it in the stopped email.")

    if problems:
        print(f"{len(problems)} problem(s) found.")
        return 1
    print("All checks passed.")
    return 0


def main(argv=None):
    import argparse
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
