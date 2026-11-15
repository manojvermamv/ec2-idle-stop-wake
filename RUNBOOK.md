# 📖 Runbook

[README](README.md) · [How it works](WORKFLOW.md) · [Setup](SETUP.md) · [Agent setup](AGENT_SETUP.md) · [Testing](TESTING.md)

> [!TIP]
> **In short**
> - **Start:** open your wake page and tap **Wake**.
> - **Work:** as usual.
> - **Stop:** close your connections. You get one warning email at 30 minutes. At 45 it stops, and you get one "stopped" email with your wake link.
>
> `<WAKE_URL>` is your wake page link (**WakePageUrl** in the stack's Outputs tab). `<PIN>` is the PIN you chose, in your password manager.

---

## ⚡ Quick answers

| I want to… | Do this |
|---|---|
| Start working | Open the wake page and tap **Wake**, or [use a terminal](#-wake-from-a-terminal-or-code). |
| Check whether it's on | Wake page → **Check status**, or `curl -d pin=<PIN> <WAKE_URL>` |
| Keep it on for a long job | Add a [long-job check](SETUP.md#step-4-install-the-idle-timer-on-the-instance), or [pause auto-stop](#-change-settings). |
| Stop it right now | EC2 console → **Instance state → Stop instance**, or close everything and wait. |
| Wake it when the page fails | [Other ways to wake](#-if-waking-doesnt-work) |
| Get a shell when SSH isn't open | Use the **Session Manager** tab (needs `EnableSessionManager`), or open a [temporary SSH rule](SETUP.md#step-4-install-the-idle-timer-on-the-instance) with `bash temp_ssh.sh open <INSTANCE_ID>` and close it afterwards. |
| Change times, email or schedule | [Change settings](#-change-settings) |
| Fix a problem | [Fix a problem](#-fix-a-problem) |
| Remove everything | [Remove everything](#-remove-everything) |

---

## 💻 Wake from a terminal or code

One command wakes the instance, waits until it's ready (up to 2 minutes), and tells you how to connect.

| Where | Wake and wait | Just check status |
|---|---|---|
| Mac, Linux, Windows Command Prompt | `curl -d pin=<PIN> -d action=wake <WAKE_URL>` | `curl -d pin=<PIN> <WAKE_URL>` |
| Windows PowerShell | `curl.exe -d pin=<PIN> -d action=wake <WAKE_URL>` | `curl.exe -d pin=<PIN> <WAKE_URL>` |

**You should see:**

```text
state: running
ready: yes
public_ip: 3.25.10.20
private_ip: 172.31.5.9
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
| `Invoke-RestMethod -Method Post -Uri <WAKE_URL> -Body @{pin='<PIN>'; action='wake'}` | Native PowerShell. |

| Reply code | Meaning |
|---|---|
| `200` | Ready, or a status reply |
| `202` | Still starting. Ask again. |
| `409` | Stopping, shutting down or deleted |
| `403` | Wrong PIN |
| `429` | Too many wrong PINs. The page is locked for 15 minutes. |
| `503` | The wrong-tries counter is unavailable. Start the instance from the EC2 console. |
| `400` | Bad request |

The progress lines arrive all together at the end. To watch live, repeat the status command.

</details>

<details>
<summary>From code or an AI agent (JSON)</summary>

```python
import json, os, urllib.request
body = json.dumps({"action": "wake", "pin": os.environ["EC2_WAKE_PIN"], "wait": 120}).encode()
request = urllib.request.Request("<WAKE_URL>", data=body, headers={"content-type": "application/json"})
print(json.load(urllib.request.urlopen(request, timeout=180)))
# {"state": "running", "ready": true, "publicIp": "3.25.10.20", "ssh": "ssh ubuntu@3.25.10.20", "progress": [...]}
```

```js
const res = await fetch("<WAKE_URL>", { method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify({ action: "wake", pin: process.env.EC2_WAKE_PIN, wait: 120 }) });
console.log(res.status, await res.json());
```

</details>

> [!TIP]
> **Skip typing the PIN.** Bookmark the one-tap link (`<WAKE_URL>#<PIN>`) in a browser, or keep `pin=<PIN>` in a file and use `curl -d @path/to/file -d action=wake <WAKE_URL>`. In code, read the PIN from an environment variable such as `EC2_WAKE_PIN`.

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
| Warning or stop times, long-job check, name in emails (`INSTANCE_NAME`), wake link in emails (`WAKE_PAGE_URL`), warning email limit (`WARN_EMAIL_GAP_MINUTES`, default 60) | On the instance: `sudoedit /etc/ec2-idle.conf` | Within 1 minute |
| Pause auto-stop | On the instance: `sudo systemctl disable --now ec2-idle.timer` | Immediately |
| Turn auto-stop back on | On the instance: `sudo systemctl enable --now ec2-idle.timer` | Immediately |
| Email, schedule, alarm, Session Manager, login name | CloudFormation → `ec2-idle` → **Update → Use existing template**, then edit the values | After the update |
| Idle times on a running instance | On the instance: run the installer again with `--warn` and `--stop`. (The stack's `WarnMinutes` and `StopMinutes` only fill in the install command.) | Within 1 minute |
| New PIN | CloudFormation → `ec2-idle` → **Update → Use existing template**, then change `WakePin` | After the update. The old PIN stops working. |

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
| `0 timers listed`, or `Unit ec2-idle.service could not be found` | **The timer isn't installed on this instance.** The most common cause is running the installer on your own computer instead of the instance. | Open a terminal **on the instance** (SSH, EC2 Instance Connect or Session Manager) and run the installer there ([Setup Step 4](SETUP.md#step-4-install-the-idle-timer-on-the-instance)). |
| `Busy (remote login session)` | A connection is still open somewhere, often a forgotten editor window. | Close it. To find it, run `loginctl list-sessions`. To end it, run `sudo loginctl terminate-session <ID>`. |
| `Busy (workload)` | Your long-job check still finds a running job. | Run the check by hand, then `echo $?`. Make its pattern stricter. |
| Errors in the log | It's missing a permission or setting. | Run the `--check` command above. It names the problem. |
| An "ALARM" email from the optional backup alarm | The instance ran quietly for hours without stopping. There are three possible reasons: an editor or session is still open, the timer isn't installed or working, or the stack was just created on an already-idle instance before the timer was installed. | Run the three commands above. If the timer is fine and nothing is open, it was a one-off from setup and clears once the instance stops. |

### 🧰 The installer says…

| You see | What it means | Do |
|---|---|---|
| `Python 3.6 or newer was not found. Installing it with …` | Normal. It installs Python and carries on. | Nothing. Add `--no-deps` to stop it installing. |
| `could not install Python with …` | The package manager failed. | Install Python 3.6 or newer yourself, then run again with `--python /path/to/python3`. |
| `systemd is not running here` | The timer needs systemd. Amazon Linux, Ubuntu, Debian, RHEL, Rocky, Alma, SUSE and Fedora have it. Alpine and containers don't. | Use a supported system. |
| `No IAM role is attached to this instance` | The stack's role isn't on the instance yet. | Attach it ([Setup Step 3](SETUP.md#step-3-give-the-instance-its-permission)), wait a minute, run again. |
| `Your current login was not seen as a session` | Without that, the timer could stop the instance while you're connected. | Debian and Ubuntu: `sudo apt install libpam-systemd`, make sure `sshd` has `UsePAM yes`, log in again. |
| `this needs root` | There's no `sudo` and you aren't root. | Run it as root, or install `sudo`. |

### ⛔ It stopped while I was working

Only logged-in connections count as activity. Web apps you use without an SSH login, and jobs your long-job check doesn't cover, don't count. Add a [long-job check](SETUP.md#step-4-install-the-idle-timer-on-the-instance) for them.

### ✉️ No warning or stopped email

1. Confirm the subscription email from AWS, and check your spam folder.
2. To send a test, run the `--check` command with `--test-email` added.
3. **Expected quiet:** after one warning, no second one is sent for 60 minutes, even if you reconnect and leave again. The log then says `Warning email skipped`. The limit resets when the instance restarts.
4. **No link in the stopped email?** Run the installer again with `--wake-url <WAKE_URL>` (included in the stack's **GuestInstallCommand**).

If emails keep failing, the instance still stops after 3 tries, so it doesn't run up costs.

### 🌐 Waking problems

| You see | Fix |
|---|---|
| `error: Wrong PIN` | Check your password manager. After 5 wrong tries the page locks for 15 minutes. |
| `error: Too many wrong PIN tries` | Wait the time it says (up to 15 minutes), then use the right PIN. The lock ends by itself. It's shared by everyone, so someone else's wrong guesses can lock you out too. If you can't wait, start the instance from the EC2 console. If you forgot the PIN, [set a new one](#-change-settings). |
| It shows `pending`, then back to `stopped` | Check the reason in the EC2 console. If your disk uses your own encryption key, set `EbsKmsKeyArn` in the stack. A capacity shortage usually clears on retry. |
| `409` for a long time | The instance is stuck stopping. Use **Force stop** in the EC2 console. |
| `ready: yes`, but SSH times out | Your security group must allow your IP on port 22. Without a fixed IP, use the new address shown. |
| SSH warns that the host key changed | The IP changed. Use the automatic SSH setup, which handles this ([Setup Step 5](SETUP.md#step-5-choose-how-you-connect)). |
| `AccessDenied` on things that used to work | Check your spend limit in **AWS Settings → Billing**. A paused project can't start instances. |

---

## 🧹 Remove everything

Do the steps in this order, so nothing is left behind.

1. **On the instance** (it must be running), in `~/ec2-idle-stop-wake/src`:
   ```bash
   bash install_guest.sh --uninstall --purge
   cd ~ && rm -rf ~/ec2-idle-stop-wake
   ```
   `--purge` also deletes the settings file and saved state. Without it, the settings stay and a later install reuses them.
2. **Detach the role the stack created.** In CloudShell or the console (EC2 → your instance → **Actions → Security → Modify IAM role** → remove the role). Skip this if the stack used your own existing role.
   ```bash
   aws ec2 describe-iam-instance-profile-associations --filters Name=instance-id,Values=<INSTANCE_ID> \
     --query 'IamInstanceProfileAssociations[0].AssociationId' --output text
   aws ec2 disassociate-iam-instance-profile --association-id <ASSOCIATION_ID>
   ```
3. **Delete the stack:** CloudFormation → `ec2-idle` → **Delete**. This removes the wake page, email topic, alarm and the role it created.
4. **If you opened a temporary SSH rule,** close it: `bash temp_ssh.sh close <INSTANCE_ID>`.
5. **If you made a fixed IP for this,** release it, because it costs money until you do:
   - `aws ec2 disassociate-address --public-ip <IP>`
   - then `aws ec2 release-address --allocation-id <ALLOCATION_ID>`

**You should see:** the stack shows **DELETE_COMPLETE**, and on the instance `systemctl list-timers ec2-idle.timer` lists nothing.

> [!NOTE]
> **Setting it up again?** A fresh stack gets a **new wake page link** (your PIN can stay the same) and a **new email subscription** to confirm. Update your bookmark, and click the new "Subscription Confirmation" email before running the installer.
