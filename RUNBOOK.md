# 📖 Runbook

[README](README.md) · [How it works](WORKFLOW.md) · [Setup](SETUP.md) · [Agent setup](AGENT_SETUP.md) · [Testing](TESTING.md)

> [!TIP]
> **In short**
> - **Start:** open your wake page and tap **Wake**.
> - **Work:** as usual.
> - **Stop:** close your connections. You get a warning email at 30 minutes, and the instance stops at 45.
>
> `<WAKE_URL>` is your wake page link (**WakePageUrl** in the stack's Outputs tab). `<WAKE_KEY>` is in your password manager.

---

## ⚡ Quick answers

| I want to… | Do this |
|---|---|
| Start working | Open the wake page and tap **Wake**, or [use a terminal](#-wake-from-a-terminal-or-code). |
| Check whether it's on | Wake page → **Check status**, or `curl -d key=<WAKE_KEY> <WAKE_URL>` |
| Keep it on for a long job | Add a [long-job check](SETUP.md#step-4-install-the-idle-timer-on-the-instance), or [pause auto-stop](#-change-settings). |
| Stop it right now | EC2 console → **Instance state → Stop instance**, or close everything and wait. |
| Wake it when the page fails | [Other ways to wake](#-if-waking-doesnt-work) |
| Change times, email or schedule | [Change settings](#-change-settings) |
| Fix a problem | [Fix a problem](#-fix-a-problem) |
| Remove everything | [Remove everything](#-remove-everything) |

---

## 💻 Wake from a terminal or code

One command wakes the instance, waits until it's ready (up to 2 minutes), and tells you how to connect.

| Where | Wake and wait | Just check status |
|---|---|---|
| Mac, Linux, Windows Command Prompt | `curl -d key=<WAKE_KEY> -d action=wake <WAKE_URL>` | `curl -d key=<WAKE_KEY> <WAKE_URL>` |
| Windows PowerShell | `curl.exe -d key=<WAKE_KEY> -d action=wake <WAKE_URL>` | `curl.exe -d key=<WAKE_KEY> <WAKE_URL>` |

**You should see:**

```text
state: running
ready: yes
public_ip: 3.25.10.20
ssh: ssh ubuntu@3.25.10.20
progress:
    0s  stopped
    2s  start requested, now pending
   24s  running
   26s  ready
```

When `ready: yes` appears, run the `ssh:` line to connect.
- **`ready: no`:** run the command again in a few seconds.
- **The instance was stopping:** the command waits for it to finish stopping, then starts it.

<details>
<summary>More options (for scripts and power users)</summary>

| Option | Effect |
|---|---|
| `-d wait=60` | How long to wait, 0 to 150 seconds. The default for a wake is 120. |
| `-d format=json` | Reply in JSON instead of text. |
| `curl -f …` | Fail with a non-zero exit code on errors, for scripts. |
| `curl <WAKE_URL>` | With nothing else, prints these instructions. |
| `Invoke-RestMethod -Method Post -Uri <WAKE_URL> -Body @{key='<WAKE_KEY>'; action='wake'}` | Native PowerShell. |

| Reply code | Meaning |
|---|---|
| `200` | Ready, or a status or dry-run reply |
| `202` | Still starting. Ask again. |
| `409` | Stopping, shutting down or deleted |
| `403` | Wrong wake key |
| `400` | Bad request |

The progress lines arrive all together at the end. To watch live, repeat the status command.

</details>

<details>
<summary>From code or an AI agent (JSON)</summary>

```python
import json, os, urllib.request
body = json.dumps({"action": "wake", "key": os.environ["EC2_WAKE_KEY"], "wait": 120}).encode()
request = urllib.request.Request("<WAKE_URL>", data=body, headers={"content-type": "application/json"})
print(json.load(urllib.request.urlopen(request, timeout=180)))
# {"state": "running", "ready": true, "publicIp": "3.25.10.20", "ssh": "ssh ubuntu@3.25.10.20", "progress": [...]}
```

```js
const res = await fetch("<WAKE_URL>", { method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify({ action: "wake", key: process.env.EC2_WAKE_KEY, wait: 120 }) });
console.log(res.status, await res.json());
```

</details>

> [!TIP]
> **Keep your key out of your command history.** Save `key=<WAKE_KEY>` in a file and use `curl -d @path/to/file -d action=wake <WAKE_URL>`.

---

## 🆘 If waking doesn't work

None of these need anything installed.

| Way | How |
|---|---|
| ☁️ **CloudShell** | AWS console → CloudShell icon → run `aws ec2 start-instances --instance-ids <INSTANCE_ID>` |
| 🖥️ **EC2 console** | Select your instance → **Instance state → Start instance** |
| 📱 **AWS phone app** | EC2 → your instance → **Start** |

---

## 📝 Change settings

| What | How | Takes effect |
|---|---|---|
| Warning or stop times, long-job check | On the instance: `sudoedit /etc/ec2-idle.conf` | Within 1 minute |
| Pause auto-stop | On the instance: `sudo systemctl disable --now ec2-idle.timer` | Immediately |
| Turn auto-stop back on | On the instance: `sudo systemctl enable --now ec2-idle.timer` | Immediately |
| Email, schedule, alarm, login name | CloudFormation → `ec2-idle` → **Update → Use existing template**, then edit the values | After the update |
| New wake key | Make a new key and hash ([Setup Step 1](SETUP.md#step-1-create-your-wake-key)), then update `WakeKeySha256` | Immediately. The old key stops working. |

---

## 🔧 Fix a problem

**Start here.** On the instance, these three commands show what the timer is doing:

```bash
sudo journalctl -u ec2-idle.service -b -n 30 --no-pager      # what it decided recently
systemctl list-timers ec2-idle.timer --no-pager               # is it scheduled?
sudo bash -c 'set -a; . /etc/ec2-idle.conf; exec python3 /usr/local/sbin/ec2_idle.py --check'
```

### 🔁 It never stops

| You see | Why | Fix |
|---|---|---|
| `Busy (remote login session)` | A connection is still open somewhere, often a forgotten editor window. | Close it. To find it, run `loginctl list-sessions`. To end it, run `sudo loginctl terminate-session <ID>`. |
| `Busy (workload)` | Your long-job check still finds a running job. | Run the check by hand, then `echo $?`. Make its pattern stricter. |
| Errors in the log | It's missing a permission or setting. | Run the `--check` command above. It names the problem. |
| An email from the backup alarm | The instance ran for hours without stopping. | Look at the log above. A long, quiet editing session can also cause this. |

### ⛔ It stopped while I was working

Only logged-in connections count as activity. Web apps you use without an SSH login, and jobs your long-job check doesn't cover, don't count. Add a [long-job check](SETUP.md#step-4-install-the-idle-timer-on-the-instance) for them.

### ✉️ No warning email

1. Confirm the subscription email from AWS, and check your spam folder.
2. To send a test, run the `--check` command with `--test-email` added.

If emails keep failing, the instance still stops after 3 tries, so it doesn't run up costs.

### 🌐 Waking problems

| You see | Fix |
|---|---|
| `error: Wrong wake key` | Copy the key again, or make a [new one](#-change-settings). |
| It shows `pending`, then back to `stopped` | Check the reason in the EC2 console. If your disk uses your own encryption key, set `EbsKmsKeyArn` in the stack. A capacity shortage usually clears on retry. |
| `409` for a long time | The instance is stuck stopping. Use **Force stop** in the EC2 console. |
| `ready: yes`, but SSH times out | Your security group must allow your IP on port 22. Without a fixed IP, use the new address shown. |
| SSH warns that the host key changed | The IP changed. Use the automatic SSH setup, which handles this ([Setup Step 5](SETUP.md#step-5-choose-how-you-connect)). |
| `AccessDenied` on things that used to work | Check your spend limit in **AWS Settings → Billing**. A paused project can't start instances. |

---

## 🧹 Remove everything

1. **On the instance**, from the copied folder: `bash install_guest.sh --uninstall`
2. **In CloudShell**, only if the stack created a new role, detach it first:
   ```bash
   aws ec2 describe-iam-instance-profile-associations --filters Name=instance-id,Values=<INSTANCE_ID> \
     --query 'IamInstanceProfileAssociations[0].AssociationId' --output text
   aws ec2 disassociate-iam-instance-profile --association-id <ASSOCIATION_ID>
   ```
3. **Delete the stack:** CloudFormation → `ec2-idle` → **Delete**.
4. **If you made a fixed IP for this,** release it, because it costs money until you do:
   - `aws ec2 disassociate-address --public-ip <IP>`
   - then `aws ec2 release-address --allocation-id <ALLOCATION_ID>`

**You should see:** the stack shows **DELETE_COMPLETE**.
