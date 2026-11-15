#!/usr/bin/env python3
"""Stop this EC2 instance after it stays idle, with one warning email first.

ec2-idle.timer runs this once a minute. It needs Python 3.6 or newer, systemd and
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

# Ready-made long-job check (the WATCH setting).
WATCH_GROUPS = ("builds", "agents", "training")
WATCH_DEFAULT_HOURS = 12
WATCH_MAX_LIMIT_HOURS = 720
AGENT_MIN_CPU_SECONDS = 0.2    # CPU an agent must use between two checks to count as working
try:
    CLK_TCK = os.sysconf("SC_CLK_TCK")
except (AttributeError, ValueError, OSError):
    CLK_TCK = 100
INTERPRETERS = frozenset(["node", "nodejs", "sh", "bash", "dash", "env"])
BUILD_TOOLS = frozenset(["make", "gmake", "cmake", "ninja"])
PACKAGE_MANAGERS = frozenset(["npm", "yarn", "pnpm"])
AGENT_NAMES = frozenset(["claude", "codex", "aider"])
WATCH_SCRIPTS = AGENT_NAMES | frozenset(["pip", "pip3"])   # programs that may be started as "python <name>"
TRAIN_WORDS = frozenset(["train", "trainer", "training", "fit", "finetune", "finetuning"])
OWN_FILES = ("ec2_idle.py", "install_guest.sh")
# Programs that only start another program ("sudo make", "timeout 3h make", "uv run train.py").
WRAPPERS = frozenset(["sudo", "nohup", "nice", "ionice", "time", "timeout", "setsid", "stdbuf", "uv", "npx"])
# Options of npm, yarn, pnpm and the wrappers that are followed by a value, so the value is not mistaken for a command.
VALUE_OPTIONS = frozenset(["--prefix", "-C", "--cwd", "--dir", "--workspace", "-w", "--filter", "-F", "-u", "-g", "-n"])


class AwsError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


def run(*args, timeout=15):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=timeout)


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


def _is_python(name):
    return name == "python" or (name.startswith("python") and name[6:].replace(".", "").isdigit())


def _is_duration(word):
    """3, 90, 1.5, 3h, 30m: the length given to timeout and nice."""
    return word[:1].isdigit() and all(c in "0123456789.smhd" for c in word)


def _positional(words):
    """The words that are not options, and not the value of an option that takes one."""
    found, skip = [], False
    for word in words:
        if skip:
            skip = False
        elif word in VALUE_OPTIONS:
            skip = True
        elif not word.startswith("-"):
            found.append(word)
    return found


def command_words(argv):
    """Turn a command line (a list of words) into [command, args...], skipping node, bash, sudo, timeout and the like."""
    words = list(argv)
    while words:
        base = os.path.basename(words[0])
        if base not in INTERPRETERS and base not in WRAPPERS:
            break
        words = words[1:]
        while words and (words[0] in VALUE_OPTIONS or words[0].startswith("-")
                         or "=" in words[0] or _is_duration(words[0])):
            words = words[2:] if words[0] in VALUE_OPTIONS else words[1:]
        if base == "uv" and words and words[0] == "run":
            words = words[1:]
    if words:
        name = os.path.basename(words[0])
        for extension in (".js", ".cjs", ".mjs"):   # "node .../npm-cli.js" is npm
            if name.endswith(extension):
                name = name[:-len(extension)]
        words[0] = name[:-4] if name.endswith("-cli") else name
    return words


def classify(argv):
    """Return (group, name) when this command line is a watched long job, otherwise None.

    Only finite commands are listed on purpose: "npm run build" ends, "npm start" does not.
    """
    words = command_words(argv)
    if not words:
        return None
    cmd, args = words[0], words[1:]
    if cmd.endswith(".py"):   # started directly, or by "uv run train.py"
        cmd, args = "python", [cmd] + args
    if _is_python(cmd):
        if args[:2] == ["-m", "pip"]:
            cmd, args = "pip", args[2:]
        elif args and not args[0].startswith("-") and os.path.basename(args[0]) in WATCH_SCRIPTS:
            cmd, args = os.path.basename(args[0]), args[1:]
    positional = _positional(args)
    first, second = (positional + ["", ""])[:2]
    if cmd in BUILD_TOOLS:
        return "builds", cmd
    if cmd == "cargo" and first == "build":
        return "builds", "cargo build"
    if cmd in PACKAGE_MANAGERS:
        if first in ("run", "run-script") and (second == "build" or second.startswith("build:")):
            return "builds", cmd + " build"
        if first == "build" and cmd != "npm":
            return "builds", cmd + " build"
        if first in ("install", "i", "ci", "add") or (cmd == "yarn" and not positional):
            return "builds", cmd + " " + ("install" if first in ("i", "") else first)
    if (cmd == "pip" or cmd == "pip3" or cmd.startswith("pip3.")) and first == "install":
        return "builds", "pip install"
    if cmd == "docker" and (first == "build" or (first == "buildx" and second in ("build", "bake"))):
        return "builds", "docker build"
    if cmd in AGENT_NAMES:
        return "agents", cmd
    if _is_python(cmd):
        target = args[1] if args[:1] == ["-m"] and len(args) > 1 else (positional[0] if positional else "")
        text = "".join(c if c.isalnum() else " " for c in os.path.basename(target).lower())
        if TRAIN_WORDS.intersection(text.split()):   # the script or module name only, not every argument
            return "training", "python training script"
    return None


def _inspect(path, uptime):
    """Describe one process as a watched job, or return None. May raise OSError or ValueError."""
    with open(os.path.join(path, "stat"), "rb") as handle:
        stat = handle.read(2048).decode("utf-8", "replace")
    rest = stat[stat.rindex(")") + 2:].split()
    if rest[0] in "ZTtXx":     # zombie, stopped or traced: not doing work
        return None
    with open(os.path.join(path, "cmdline"), "rb") as handle:
        argv = [part.decode("utf-8", "replace") for part in handle.read(4096).split(b"\0") if part]
    if any(os.path.basename(word) in OWN_FILES for word in " ".join(argv).split()):
        return None            # never count the timer itself or its installer
    found = classify(argv)
    if not found:
        return None
    start = int(rest[19])
    return {"group": found[0], "name": found[1], "key": "%s:%d" % (os.path.basename(path), start),
            "age": uptime - start / CLK_TCK,
            "cpu": sum(int(rest[index]) for index in (11, 12, 13, 14))}


def watch_scan(groups):
    """List running processes in the watched groups. Reads /proc only, starts no other program."""
    if not os.path.isdir(PROC_ROOT):
        raise RuntimeError("cannot read /proc, so the WATCH setting cannot be used here")
    try:
        with open(os.path.join(PROC_ROOT, "uptime"), "r") as handle:
            uptime = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        raise RuntimeError("cannot read /proc/uptime, so the WATCH setting cannot be used here") from None
    mine = os.getpid()
    jobs = []
    with os.scandir(PROC_ROOT) as entries:
        for entry in entries:
            if not entry.name.isdigit() or int(entry.name) == mine:
                continue
            try:
                job = _inspect(entry.path, uptime)
            except (OSError, ValueError, IndexError):
                continue       # the process ended while scanning
            if job and job["group"] in groups:
                jobs.append(job)
    return jobs


def watch_busy(cfg, cpu_path):
    """Return "watched job: <name>" while a watched long job runs, otherwise None.

    Builds and training runs older than WATCH_MAX_HOURS are ignored. An agent counts only while
    its CPU time grows between checks (the last readings are kept in cpu_path), so an agent left
    open and idle never keeps the instance on.
    """
    before = load(cpu_path)
    after = {}
    reason = None
    limit = cfg.watch_max * 3600
    for job in watch_scan(cfg.watch):
        if job["group"] == "agents":
            after[job["key"]] = job["cpu"]
            last = before.get(job["key"])
            # A newly seen agent counts as activity once, because it has just started. After that it counts
            # only while its CPU time keeps growing.
            if isinstance(last, int) and job["cpu"] - last < AGENT_MIN_CPU_SECONDS * CLK_TCK:
                continue
        elif job["age"] > limit:
            continue
        reason = reason or "watched job: " + job["name"]
    if after != before:
        record(cpu_path, after)
    return reason


def busy_reason(probe, check_sessions=True, watched=None):
    if check_sessions:
        if remote_sessions():
            return "remote login session"
        if ssm_session():
            return "Session Manager session"
    if watched:
        reason = watched()
        if reason:
            return reason
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
            raise RuntimeError("No IAM role is attached to this instance. Attach the role the stack created "
                               "(the AttachRoleCommand output, or EC2 console -> Actions -> Security -> "
                               "Modify IAM role), wait a minute, then run this again") from None
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


def plain_subject(subject):
    """SNS accepts only a single line of printable ASCII (up to 99 characters) as a subject."""
    return "".join(c if " " <= c <= "~" else "?" for c in subject)[:99]


def publish(ctx, topic_arn, subject, message):
    region = topic_arn.split(":")[3] or ctx["region"]
    aws_call(ctx, "sns", region, {"Action": "Publish", "Version": "2010-03-31",
                                  "TopicArn": topic_arn, "Subject": plain_subject(subject),
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
        f"Your EC2 instance {who} in {ctx['region']} was told to stop at {now_text} after "
        f"{minutes(cfg.stop)} without activity.\n\n"
        f"- Compute charges end once it has fully stopped (usually within a minute). Disk storage is still billed.\n"
        f"- Your files, programs and settings are safe on its disk.\n\n"
    )
    if cfg.wake_url:
        body += (
            f"To start it again, open your wake page and tap Wake:\n{cfg.wake_url}\n\n"
            f"Or run this in any terminal (Windows PowerShell: use curl.exe):\n"
            f"curl -d pin=YOUR_PIN -d action=wake {cfg.wake_url}\n"
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


def parse_watch(text):
    """WATCH is empty (off) or a comma-separated list of: builds, agents, training."""
    groups = []
    for item in text.strip().split(","):
        item = item.strip()
        if not item and not text.strip():
            break
        if item not in WATCH_GROUPS:
            raise ValueError("WATCH must be empty or a comma-separated list of: %s (got %r)"
                             % (", ".join(WATCH_GROUPS), item))
        if item not in groups:
            groups.append(item)
    return tuple(groups)


def parse_on_off(name, text):
    if text.strip() not in ("on", "off"):
        raise ValueError("%s must be on or off" % name)
    return text.strip() == "on"


def parse_watch_hours(text):
    text = text.strip()
    if not text.isdigit() or not 1 <= int(text) <= WATCH_MAX_LIMIT_HOURS:
        raise ValueError("WATCH_MAX_HOURS must be a whole number from 1 to %d" % WATCH_MAX_LIMIT_HOURS)
    return int(text)


def load_config(env):
    if env.get("IDLE_MODE") == "dry-run":   # an old settings file: refuse rather than start stopping for real
        raise ValueError("Dry-run mode was removed. Delete the IDLE_MODE line from /etc/ec2-idle.conf, "
                         "or run install_guest.sh --uninstall --purge.")
    cfg = SimpleNamespace(
        warn=int(env.get("WARN_MINUTES", "30")) * 60,
        stop=int(env.get("STOP_MINUTES", "45")) * 60,
        retries=int(env.get("WARN_RETRIES", "3")),
        email_gap=int(env.get("WARN_EMAIL_GAP_MINUTES", "60")) * 60,
        topic=env.get("SNS_TOPIC_ARN", "").strip(),
        probe=env.get("WORK_PROBE", "").strip(),
        wake_url=env.get("WAKE_PAGE_URL", "").strip(),
        name=env.get("INSTANCE_NAME", "").strip(),
        monthly=parse_on_off("MONTHLY_EMAIL", env.get("MONTHLY_EMAIL", "on")),
        watch=parse_watch(env.get("WATCH", "")),
        watch_max=parse_watch_hours(env.get("WATCH_MAX_HOURS", str(WATCH_DEFAULT_HOURS))),
    )
    if not 0 < cfg.warn < cfg.stop or cfg.retries < 1 or cfg.email_gap < 0:
        raise ValueError("Check WARN_MINUTES, STOP_MINUTES, WARN_RETRIES and WARN_EMAIL_GAP_MINUTES")
    if cfg.topic and not cfg.topic.startswith("arn:"):
        raise ValueError("SNS_TOPIC_ARN must be an SNS topic ARN or empty")
    if cfg.wake_url and not cfg.wake_url.startswith("https://"):
        raise ValueError("WAKE_PAGE_URL must start with https:// or be empty")
    return cfg


def send_warning(cfg, ctx, state, emails, emails_path, state_path, context_error=None):
    """Send the warning email at most once per WARN_EMAIL_GAP_MINUTES."""
    last = emails.get("warned_at")
    if last is not None and time.time() - last < cfg.email_gap:
        print(f"Warning email skipped: one was sent {(time.time() - last) / 60:.0f} minutes ago "
              f"(limit: one per {cfg.email_gap // 60} minutes).")
        return
    if not cfg.topic:
        print("No SNS topic configured. Continuing without a warning email.")
        return
    try:
        if ctx is None:   # no credentials yet: count it like a failed email, so the stop is never held back
            raise RuntimeError(f"could not read this instance's details ({context_error})")
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


MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December")
HEALTH_RETRY_SECONDS = 24 * 3600   # a failed monthly note is tried again at most once a day


def month_key(timestamp):
    """The UTC month of a timestamp as 'YYYY-MM' (UTC, so daylight saving never matters)."""
    moment = time.gmtime(timestamp)
    return "%04d-%02d" % (moment.tm_year, moment.tm_mon)


def valid_month(value):
    return (isinstance(value, str) and len(value) == 7 and value[4] == "-"
            and value[:4].isdigit() and value[5:].isdigit() and 1 <= int(value[5:]) <= 12)


def previous_month(key):
    year, month = int(key[:4]), int(key[5:]) - 1
    return "%04d-%02d" % ((year, month) if month else (year - 1, 12))


def month_title(key):
    return "%s %s" % (MONTH_NAMES[int(key[5:]) - 1], key[:4])


def save(path, data):
    """Write a small state file. Returns False instead of raising when the disk is not writable."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record(path, data)
        return True
    except OSError:
        return False


def count_stop(health_path):
    """Add one to this month's count of stops (kept across reboots, for the monthly note)."""
    health = load(health_path)
    stops = health.get("stops")
    stops = {key: value for key, value in (stops if isinstance(stops, dict) else {}).items()
             if valid_month(key) and isinstance(value, int)}
    month = month_key(time.time())
    stops[month] = stops.get(month, 0) + 1
    health["stops"] = {key: stops[key] for key in sorted(stops)[-3:]}
    save(health_path, health)


def health_email_text(cfg, ctx, month, last_month_stops):
    who = label(cfg, ctx)
    subject = f"EC2 {who}: idle stop is still working ({month_title(month)})"
    lines = ["Hello,", "", f"Idle stop is still working on {who} in {ctx['region']}.", ""]
    if last_month_stops is None:
        lines.append("This is your first monthly note.")
    else:
        lines.append(f"Times it stopped the instance in {month_title(previous_month(month))}: {last_month_stops}")
    if cfg.wake_url:
        lines.append(f"Your wake page: {cfg.wake_url}")
    lines.append("To stop these notes, run on the instance: bash install_guest.sh --monthly-email off")
    return subject, "\n".join(lines) + "\n" + SIGNATURE


def health_email(cfg, health_path):
    """Send one short 'still working' email per calendar month. Never raises, never blocks a stop.

    The month is saved only after SNS accepts the email, so a failure is retried (at most once a day).
    The first run only records the month, because the setup email has just been sent.
    """
    if not cfg.topic or not cfg.monthly:
        return
    now = time.time()
    month = month_key(now)
    health = load(health_path)
    seen = health.get("month")
    if seen == month:
        return
    if not valid_month(seen):   # first run, or a damaged file: start from this month
        health["month"] = month
        save(health_path, health)
        return
    if seen > month:            # the clock went backwards: wait
        return
    failed = health.get("failed_at")
    if isinstance(failed, (int, float)) and 0 <= now - failed < HEALTH_RETRY_SECONDS:
        return
    health["failed_at"] = now   # claimed first, so a disk that can't be written never causes repeats
    if not save(health_path, health):
        return
    stops = health.get("stops")
    last = stops.get(previous_month(month), 0) if isinstance(stops, dict) else 0
    try:
        ctx = instance_context()
        publish(ctx, cfg.topic, *health_email_text(cfg, ctx, month, last if isinstance(last, int) else 0))
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"Monthly note failed ({error}). It will be tried again within a day.", file=sys.stderr)
        return
    health["month"] = month
    health.pop("failed_at", None)
    save(health_path, health)
    print("Monthly note accepted by SNS.")


def send_stopped(cfg, ctx, emails, emails_path):
    """Send one 'stopped' email per stop. Failures never block the stop. True when it counts a new stop."""
    if emails.get("stopped_sent") or not cfg.topic:
        return False
    import datetime
    now_text = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M UTC on %d %b %Y")
    try:
        publish(ctx, cfg.topic, *stopped_email(cfg, ctx, now_text))
        print("Stopped email accepted by SNS.")
    except (OSError, RuntimeError) as error:
        print(f"Stopped email failed ({error}). The stop was still requested.", file=sys.stderr)
    emails["stopped_sent"] = True
    record(emails_path, emails)
    return True


def watch(cfg, state_path, health_path=None):
    emails_path = state_path.with_name("emails.json")
    health_path = health_path or state_path.with_name("health.json")
    try:
        health_email(cfg, health_path)
    except Exception as error:   # the monthly note must never get in the way of the stop
        print(f"Monthly note skipped ({error}).", file=sys.stderr)

    cpu_path = state_path.with_name("watch.json")
    watched = (lambda: watch_busy(cfg, cpu_path)) if cfg.watch else None

    def activity():
        try:
            return busy_reason(cfg.probe, watched=watched)
        except (OSError, subprocess.SubprocessError, RuntimeError):
            record(state_path, {})
            raise

    busy = activity()
    now = time.monotonic()
    before = load(state_path)
    state = advance(before, now, busy)
    if busy:
        if before.get("stop_requested") is not None:   # a stop was requested but you are back: allow a new "stopped" email
            emails = load(emails_path)
            emails.pop("stopped_sent", None)
            record(emails_path, emails)
        record(state_path, state)
        print(f"Busy ({busy}). Idle clock reset.")
        return

    elapsed = now - state["since"]
    record(state_path, state)
    print(f"Idle for {elapsed / 60:.1f} minutes.", flush=True)

    ctx = None
    if elapsed >= cfg.warn and state.get("warned") is None:
        emails = load(emails_path)
        context_error = None
        if cfg.topic:
            try:
                ctx = instance_context()
            except (OSError, RuntimeError) as error:
                context_error = error
        send_warning(cfg, ctx, state, emails, emails_path, state_path, context_error)
        state["warned"] = time.monotonic()
        record(state_path, state)

    requested = state.get("stop_requested")
    if (elapsed < cfg.stop or state.get("warned") is None
            or now - state["warned"] < cfg.stop - cfg.warn
            or (requested is not None and now - requested < STOP_RETRY_SECONDS)):
        return
    if ctx is None:
        ctx = instance_context()
    busy = activity()
    if busy:
        record(state_path, {})
        print(f"Busy ({busy}) on final check. Idle clock reset.")
        return
    stop_instance(ctx)
    print("Requested EC2 StopInstances.")
    if send_stopped(cfg, ctx, load(emails_path), emails_path):
        try:
            count_stop(health_path)
        except Exception as error:   # only a count for the monthly note: never worth failing the tick
            print(f"Stop count skipped ({error}).", file=sys.stderr)
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

    print(f"Warn after {cfg.warn // 60} min, stop after {cfg.stop // 60} min.")
    try:
        sessions = remote_sessions()
        ok(f"Login session check works ({sessions} remote session(s) now)")
        if expect_session and not sessions:
            fail("Your current login was not seen as a session, so the timer could stop the instance "
                 "while you are connected. On Debian and Ubuntu run: sudo apt install libpam-systemd. "
                 "sshd also needs UsePAM yes. Then log in again and re-run this.")
    except (OSError, subprocess.SubprocessError) as error:
        fail(f"Login session check failed: {error}")
    try:
        ok(f"Session Manager check works (open: {'yes' if ssm_session() else 'no'})")
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        fail(f"Session Manager check failed: {error}")
    if cfg.watch:
        try:
            jobs = watch_scan(cfg.watch)
            ok("Long-job check works (watching: %s; ignoring builds and training older than %d hours)"
               % (", ".join(cfg.watch), cfg.watch_max))
            for job in jobs:
                if job["group"] == "agents":
                    note = "counts only while it uses CPU"
                elif job["age"] > cfg.watch_max * 3600:
                    note = "running %.0f hours, too old, ignored" % (job["age"] / 3600)
                else:
                    note = "counts as busy, running %.0f minutes" % (job["age"] / 60)
                print("      running now: %s (%s, %s)" % (job["name"], job["group"], note))
            if not jobs:
                print("      nothing watched is running right now")
        except (OSError, RuntimeError) as error:
            fail(f"Long-job check failed: {error}")
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
        fail(f"Instance metadata or role: {error}. Run this on the EC2 instance itself. Instance "
             "metadata must be enabled (EC2 console -> Actions -> Instance settings -> Modify "
             "instance metadata options).")
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
                        + (f"- You'll also get one short 'still working' note a month. "
                           f"Turn it off with install_guest.sh --monthly-email off.\n" if cfg.monthly else "")
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
    watch(cfg, Path(os.environ.get("RUNTIME_DIRECTORY", "/run/ec2-idle")) / "state.json",
          Path(os.environ.get("STATE_DIRECTORY", "/var/lib/ec2-idle")) / "health.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
