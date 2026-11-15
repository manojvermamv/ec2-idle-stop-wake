"""Wake page and wake API for one EC2 instance, served by a Lambda Function URL.

Browsers get a web page. Terminals and code POST to the same URL:

    curl -d key=YOUR_WAKE_KEY -d action=wake URL     (waits, then prints readable progress)
    curl -d key=YOUR_WAKE_KEY URL                    (status only)

Form posts and "accept: text/plain" get "name: value" text. JSON posts and
"accept: application/json" get JSON. The key is checked against WAKE_KEY_SHA256,
so the function configuration never holds the key itself.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse

import boto3
from botocore.exceptions import ClientError

ec2 = boto3.client("ec2")

POLL_SECONDS = 4
MAX_WAIT_SECONDS = 150
TEXT_WAKE_WAIT = 120
ENDED = {"shutting-down", "terminated"}

HEADERS = {
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
}

HELP = """EC2 wake API

Wake and wait until ready (prints progress, IP and SSH command):
  curl -d key=YOUR_WAKE_KEY -d action=wake {url}

Status only:
  curl -d key=YOUR_WAKE_KEY {url}

Options: action=status|wake, wait=SECONDS (0-150, text wake default 120),
format=text|json. Windows PowerShell: use curl.exe, or
  Invoke-RestMethod -Method Post -Uri {url} -Body @{{key='YOUR_WAKE_KEY'; action='wake'}}
Exit-code friendly: add curl -f to fail on a wrong key or a blocked wake.
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wake EC2</title>
<style>
:root { color-scheme: light dark; --bg: #f6f7f9; --card: #fff; --text: #16191f; --muted: #5f6b7a;
  --accent: #0972d3; --ok: #037f0c; --warn: #8d6605; --bad: #d91515; --line: #d1d5db; }
@media (prefers-color-scheme: dark) { :root { --bg: #0f1419; --card: #1a2029; --text: #e9ebed;
  --muted: #9ba7b6; --accent: #539fe5; --ok: #29ad32; --warn: #e0ca57; --bad: #ff5d64; --line: #414d5c; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 440px; margin: 48px auto; padding: 0 16px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 24px; }
h1 { font-size: 22px; margin: 0 0 4px; }
p { margin: 0 0 16px; color: var(--muted); }
label { display: block; font-weight: 600; margin-bottom: 6px; }
input[type=password] { width: 100%; padding: 10px 12px; font: inherit; color: inherit;
  background: transparent; border: 1px solid var(--line); border-radius: 8px; }
.row { display: flex; gap: 8px; margin-top: 12px; }
button { flex: 1; padding: 10px; font: inherit; font-weight: 600; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--accent); background: var(--accent); color: #fff; }
button.secondary { background: transparent; color: var(--accent); }
button:disabled { opacity: .6; cursor: wait; }
.remember { display: flex; align-items: center; gap: 6px; margin-top: 10px; font-weight: 400;
  color: var(--muted); }
#out { margin-top: 20px; display: none; }
.state { font-size: 20px; font-weight: 700; }
.running { color: var(--ok); } .pending, .stopping { color: var(--warn); }
.stopped, .error { color: var(--bad); }
ol { margin: 8px 0 0; padding-left: 20px; color: var(--muted); font-size: 14px; }
code { display: block; margin-top: 8px; padding: 10px; border-radius: 8px; border: 1px solid var(--line);
  overflow-wrap: anywhere; font-size: 14px; }
</style>
</head>
<body>
<main>
<div class="card">
<h1>Wake EC2</h1>
<p>Start your workspace from any browser. It stops again by itself when idle.</p>
<form id="form">
<label for="key">Wake key</label>
<input id="key" type="password" autocomplete="current-password" required>
<label class="remember"><input id="remember" type="checkbox"> Remember on this device</label>
<div class="row">
<button id="wake" type="submit">Wake</button>
<button id="status" type="button" class="secondary">Check status</button>
</div>
</form>
<div id="out" aria-live="polite">
<div id="state" class="state"></div>
<div id="detail"></div>
<ol id="log"></ol>
<code id="ssh" hidden></code>
</div>
</div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let timer = null, began = 0, lastState = "";
try { const saved = localStorage.getItem("wakeKey"); if (saved) { $("key").value = saved; $("remember").checked = true; } } catch (e) {}

function note(text) {
  const item = document.createElement("li");
  item.textContent = Math.round((Date.now() - began) / 1000) + "s " + text;
  $("log").appendChild(item);
}

function show(data, failed) {
  $("out").style.display = "block";
  const state = failed ? "error" : data.state;
  $("state").textContent = failed ? (data.error || "Request failed") : state;
  $("state").className = "state " + state;
  const ip = data.publicIp || data.privateIp;
  $("detail").textContent = data.message || (ip ? "IP address " + ip : "");
  if (!failed && state !== lastState) { note(state); lastState = state; }
  $("ssh").hidden = !(data.ready && data.ssh);
  if (data.ready && data.ssh) $("ssh").textContent = data.ssh;
}

async function call(action) {
  const key = $("key").value.trim();
  if (!key) { $("key").focus(); return null; }
  try {
    if ($("remember").checked) localStorage.setItem("wakeKey", key); else localStorage.removeItem("wakeKey");
  } catch (e) {}
  const response = await fetch(location.pathname, {
    method: "POST", headers: { "content-type": "application/json", "accept": "application/json" },
    body: JSON.stringify({ action, key, wait: 0 }),
  });
  let data = {};
  try { data = await response.json(); } catch (e) { data = { error: "Unexpected response" }; }
  const failed = response.status === 403 || response.status === 400 || response.status >= 500;
  show(data, failed);
  return failed ? null : data;
}

async function run(action, attempt = 0) {
  clearTimeout(timer);
  if (attempt === 0) { began = Date.now(); lastState = ""; $("log").textContent = ""; }
  $("wake").disabled = $("status").disabled = true;
  try {
    const data = await call(action);
    if (data && action === "wake" && !data.ready && !data.dryRun && attempt < 60
        && data.state !== "terminated" && data.state !== "shutting-down") {
      timer = setTimeout(() => run("wake", attempt + 1), 5000);
    } else if (data && data.ready && action === "wake") {
      note("ready");
    }
  } catch (e) {
    show({ error: "Network error. Try again." }, true);
  } finally {
    $("wake").disabled = $("status").disabled = false;
  }
}

$("form").addEventListener("submit", (event) => { event.preventDefault(); run("wake"); });
$("status").addEventListener("click", () => run("status"));
</script>
</body>
</html>
"""


# Requests and replies -----------------------------------------------------

def lowered(headers):
    return {name.lower(): value for name, value in (headers or {}).items()}


def read_request(event):
    """Return a dict of request fields from a JSON or form body."""
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    headers = lowered(event.get("headers"))
    kind = headers.get("content-type", "")
    if "json" in kind or body.lstrip().startswith("{"):
        fields = json.loads(body) if body.strip() else {}
        if not isinstance(fields, dict):
            raise ValueError("JSON object expected")
        fields.setdefault("_body", "json")
    else:
        fields = {name: values[-1] for name, values in urllib.parse.parse_qs(body).items()}
        fields["_body"] = "form"
    if not fields.get("key") and headers.get("x-wake-key"):
        fields["key"] = headers["x-wake-key"]
    return fields, headers


def wants_text(fields, headers):
    chosen = str(fields.get("format", "")).lower()
    if chosen in {"text", "json"}:
        return chosen == "text"
    accept = headers.get("accept", "")
    if "application/json" in accept:
        return False
    if "text/plain" in accept:
        return True
    return fields.get("_body") == "form"


def as_text(data):
    if "error" in data:
        return f"error: {data['error']}\n"
    lines = [f"state: {data['state']}", f"ready: {'yes' if data.get('ready') else 'no'}"]
    for label, name in (("message", "message"), ("public_ip", "publicIp"),
                        ("private_ip", "privateIp"), ("ssh", "ssh")):
        if data.get(name):
            lines.append(f"{label}: {data[name]}")
    if data.get("progress"):
        lines.append("progress:")
        lines += [f"  {step['seconds']:>3}s  {step['event']}" for step in data["progress"]]
    return "\n".join(lines) + "\n"


def reply(status, data, text=False):
    if text:
        return {"statusCode": status, "body": as_text(data),
                "headers": {**HEADERS, "content-type": "text/plain; charset=utf-8"}}
    return {"statusCode": status, "body": json.dumps(data),
            "headers": {**HEADERS, "content-type": "application/json"}}


def page():
    csp = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
           "connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
    return {"statusCode": 200, "body": PAGE, "headers": {
        **HEADERS, "content-type": "text/html; charset=utf-8", "content-security-policy": csp}}


def help_text(headers):
    url = f"https://{headers.get('host', 'YOUR-WAKE-URL')}/"
    return {"statusCode": 200, "body": HELP.format(url=url),
            "headers": {**HEADERS, "content-type": "text/plain; charset=utf-8"}}


def key_matches(key):
    expected = os.environ.get("WAKE_KEY_SHA256", "").strip().lower()
    if len(expected) != 64 or not key:
        return False
    actual = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    return hmac.compare_digest(actual, expected)


def wait_seconds(fields, text, action, context):
    raw = fields.get("wait")
    if raw in (None, ""):
        seconds = TEXT_WAKE_WAIT if text and action == "wake" else 0
    elif str(raw).lower() in {"yes", "true"}:
        seconds = MAX_WAIT_SECONDS
    else:
        seconds = int(float(raw))
    seconds = max(0, min(seconds, MAX_WAIT_SECONDS))
    if context is not None:
        seconds = min(seconds, max(0, context.get_remaining_time_in_millis() // 1000 - 10))
    return seconds


# EC2 --------------------------------------------------------------------

def describe(instance_id):
    return ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]


def summary(instance, state):
    public, private = instance.get("PublicIpAddress"), instance.get("PrivateIpAddress")
    address = public or private
    ready = state == "running" and bool(address)
    data = {"state": state, "ready": ready, "publicIp": public, "privateIp": private,
            "sshUser": os.environ.get("SSH_USER", "ubuntu")}
    if ready:
        data["ssh"] = f"ssh {data['sshUser']}@{address}"
    return data


def wake(instance_id, budget, dry_run):
    """Start the instance if needed and wait up to budget seconds until it is ready."""
    began = time.monotonic()
    progress, last, started = [], None, False

    def note(event):
        progress.append({"seconds": int(time.monotonic() - began), "event": event})

    while True:
        instance = describe(instance_id)
        state = instance["State"]["Name"]
        if state != last:
            note(state)
            last = state
        if state == "stopped" and not started:
            if dry_run:
                try:
                    ec2.start_instances(InstanceIds=[instance_id], DryRun=True)
                except ClientError as error:
                    if error.response["Error"]["Code"] != "DryRunOperation":
                        raise
                    note("dry run: start permission verified")
                    return {**summary(instance, state), "dryRun": True, "progress": progress,
                            "message": "Dry run. Start permission verified."}
                raise RuntimeError("EC2 did not return the expected DryRunOperation")
            try:
                result = ec2.start_instances(InstanceIds=[instance_id])
                started = True
                state = last = result["StartingInstances"][0]["CurrentState"]["Name"]
                note(f"start requested, now {state}")
            except ClientError as error:
                if error.response["Error"]["Code"] != "IncorrectInstanceState":
                    raise
                note("state changed during start, retrying")
        data = summary(instance, state)
        if data["ready"] or state in ENDED:
            break
        if time.monotonic() - began + POLL_SECONDS > budget:
            break
        time.sleep(POLL_SECONDS)
    if data["ready"]:
        note("ready")
    elif state == "stopping":
        data["message"] = "The instance is still stopping. It will start once stopped; retry."
    elif state not in ENDED:
        data["message"] = "Starting. This usually takes under a minute."
    return {**data, "progress": progress}


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")
    headers = lowered(event.get("headers"))
    if method == "GET":
        return page() if "text/html" in headers.get("accept", "") else help_text(headers)
    if method != "POST":
        return reply(405, {"error": "Use GET for the page or POST for the API"})
    try:
        fields, headers = read_request(event)
    except (ValueError, UnicodeDecodeError):
        return reply(400, {"error": "Send a JSON or form body"})
    text = wants_text(fields, headers)
    if not key_matches(fields.get("key")):
        print("Rejected request: wrong wake key")
        return reply(403, {"error": "Wrong wake key"}, text)
    action = str(fields.get("action", "status")).lower()
    if action not in {"status", "wake"}:
        return reply(400, {"error": "action must be status or wake"}, text)
    try:
        budget = wait_seconds(fields, text, action, context)
    except (ValueError, OverflowError):
        return reply(400, {"error": "wait must be a number of seconds"}, text)

    instance_id = os.environ["INSTANCE_ID"]
    if action == "status":
        instance = describe(instance_id)
        data = summary(instance, instance["State"]["Name"])
        print(f"status: {data['state']}")
        return reply(200, data, text)

    data = wake(instance_id, budget, os.environ.get("DRY_RUN", "false").lower() == "true")
    print(f"wake: {data['state']} ready={data['ready']} after {data['progress'][-1]['seconds']}s")
    if data["ready"] or data.get("dryRun"):
        status = 200
    elif data["state"] in ENDED or data["state"] == "stopping":
        status = 409
    else:
        status = 202
    return reply(status, data, text)
