# 📖 Runbook

[README](README.md) · [How it works](WORKFLOW.md) · [Setup](SETUP.md) · [Agent setup](AGENT_SETUP.md) · [Testing](TESTING.md)

> [!TIP]
> **In short**
> - **Start:** open your wake page and tap **Wake**.
> - **Work:** as usual.
> - **Stop:** close your connections. You get one warning email at 30 minutes. At 45 it stops, and you get one "stopped" email with your wake link. (An instance nobody uses stops about 50 minutes after it starts.)
>
> `<WAKE_URL>` is your wake page link (**WakePageUrl** in the stack's Outputs tab). `<PIN>` is the PIN you chose, in your password manager.

---

## ⚡ Quick answers

| I want to… | Do this |
|---|---|
| Start working | Open the wake page and tap **Wake**, or [use a terminal](#-wake-from-a-terminal-or-code). |
| Check whether it's on | Wake page → **Check status**, or `curl -d pin=<PIN> <WAKE_URL>` |
| Keep it on for a long job | Turn on the [ready-made long-job check](SETUP.md#step-4-install-the-idle-timer-on-the-instance) (`bash install_guest.sh --watch`), or [pause auto-stop](#-change-settings). |
| Stop it right now | EC2 console → **Instance state → Stop instance**, or close everything and wait. |
| Fix a lock or a forgotten PIN | [Locked out or forgot the PIN](#-locked-out-or-forgot-the-pin) |
| Wake it when the page fails | [Other ways to wake](#-if-waking-doesnt-work) |
| Get a shell when SSH isn't open | Use the **Session Manager** tab (needs `EnableSessionManager`), or open a [temporary SSH rule](SETUP.md#step-4-install-the-idle-timer-on-the-instance) with `bash temp_ssh.sh open <INSTANCE_ID>` and close it afterwards. |
| Change times, email or schedule | [Change settings](#-change-settings) |
| Get an email if my AWS bill goes over what I expect | [Set a spend alert](SETUP.md#-optional-set-a-spend-alert) |
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
| `405` | Wrong HTTP method. Use GET for the page or POST for the API. |
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
| Warning or stop times, long-job check (`WATCH`, `WATCH_MAX_HOURS`, `WORK_PROBE`), name in emails (`INSTANCE_NAME`), wake link in emails (`WAKE_PAGE_URL`), warning email limit (`WARN_EMAIL_GAP_MINUTES`, default 60), failed warnings before it stops anyway (`WARN_RETRIES`, default 3) | On the instance: `sudoedit /etc/ec2-idle.conf` | Within 1 minute |
| Monthly "still working" email (`MONTHLY_EMAIL`, `on` or `off`) | On the instance: `bash install_guest.sh --monthly-email off` (or `on`) | Within 1 minute |
| Pause auto-stop | On the instance: `sudo systemctl disable --now ec2-idle.timer` | Immediately |
| Turn auto-stop back on | On the instance: `sudo systemctl enable --now ec2-idle.timer` | Immediately |
| Schedule, alarm, Session Manager, login name | CloudFormation → `ec2-idle` → **Update → Use existing template**, then edit the values | After the update |
| Email address | The same stack update, **then** run the stack's new **GuestInstallCommand** on the instance (without an email it includes `--topic none`), because the instance keeps its own copy of the email topic | After you run the command |
| Idle times on a running instance | On the instance: run the installer again with `--warn` and `--stop`. (The stack's `WarnMinutes` and `StopMinutes` only fill in the install command.) | Within 1 minute |
| New PIN (or you forgot it) | CloudFormation → `ec2-idle` → **Update → Use existing template**, then change `WakePin`. More in [locked out or forgot the PIN](#-locked-out-or-forgot-the-pin). | After the update. The old PIN stops working. |

---

## 🔧 Fix a problem

**Start here.** On the instance, these three commands show what the timer is doing:

```bash
sudo journalctl -u ec2-idle.service -b -n 30 --no-pager      # what it decided recently
systemctl list-timers ec2-idle.timer --no-pager               # is it scheduled?
sudo bash -c 'set -a; . /etc/ec2-idle.conf; exec python3 /usr/local/sbin/ec2_idle.py --check'
```

If the last command says `python3: not found`, use the Python named on the `ExecStart` line of `systemctl cat ec2-idle.service` instead of `python3`.

### 🔁 It never stops

| You see | Why | Fix |
|---|---|---|
| `0 timers listed`, or `Unit ec2-idle.service could not be found` | **The timer isn't installed on this instance.** The most common cause is running the installer on your own computer instead of the instance. | Open a terminal **on the instance** (SSH, EC2 Instance Connect or Session Manager) and run the installer there ([Setup Step 4](SETUP.md#step-4-install-the-idle-timer-on-the-instance)). |
| `Busy (remote login session)` | A connection is still open somewhere, often a forgotten editor window. | Close it. To find it, run `loginctl list-sessions`. To end it, run `sudo loginctl terminate-session <ID>`. |
| `Busy (watched job: npm build)` (or `make`, `docker build`, `claude`, `python training script` and so on) | The ready-made long-job check (`--watch`) sees that job running. The text after the colon says which one. | If the job is really finished or stuck, end it. Run the `--check` command above to list what is running. A job older than 12 hours is ignored by itself. To turn the check off, run the installer with `--watch off`. |
| `Busy (workload)` | Your own long-job check (`--probe`) still finds a running job. | Run the check by hand, then `echo $?`. Make its pattern stricter. |
| Errors in the log | It's missing a permission or setting. | Run the `--check` command above. It names the problem. |
| An "ALARM" email from the optional backup alarm | The instance ran quietly for hours without stopping. There are three possible reasons: an editor or session is still open, the timer isn't installed or working, or the stack was just created on an already-idle instance before the timer was installed. | Run the three commands above. If the timer is fine and nothing is open, it was a one-off from setup and clears once the instance stops. |

### 🧰 The installer says…

| You see | What it means | Do |
|---|---|---|
| `Python 3.6 or newer was not found. Installing it with …` | Normal. It installs Python and carries on. | Nothing. Add `--no-deps` to stop it installing. |
| `could not install Python with …` | The package manager failed. | Install Python 3.6 or newer yourself, then run again with `--python /path/to/python3`. |
| `systemd is not running here` | The timer needs systemd. Amazon Linux, Ubuntu, Debian, RHEL, Rocky, Alma, SUSE and Fedora have it. Alpine and containers don't. | Use a supported system. |
| `No IAM role is attached to this instance` | The stack's role isn't on the instance yet. | Attach it ([Setup Step 3](SETUP.md#step-3-give-the-instance-its-permission)), wait a minute, run again. |
| `Your current login was not seen as a session` | Without that, the timer could stop the instance while you're connected. This also appears if you are in a Session Manager shell, which the timer counts separately. | Debian and Ubuntu: the installer normally adds `libpam-systemd` by itself (unless you used `--no-deps`). Otherwise run `sudo apt install libpam-systemd`, make sure `sshd` has `UsePAM yes`, log in again over SSH. |
| `this needs root` | There's no `sudo` and you aren't root. | Run it as root, or install `sudo`. |
| `Found Python at …, but it is not used` | The timer runs as root, so its Python must belong to root and sit in folders only root can change. A pyenv, conda or home-folder Python doesn't qualify. | Nothing: the installer installs the system Python. Or pass `--python /usr/bin/python3`. |

### ⛔ It stopped while I was working

Only logged-in connections count as activity. Web apps you use without an SSH login, and jobs your long-job check doesn't cover, don't count. Turn on the ready-made check (`bash install_guest.sh --watch`) or add your own [long-job check](SETUP.md#step-4-install-the-idle-timer-on-the-instance) for them. The ready-made one is best effort: an agent waiting quietly for a slow reply can look idle, and a job started more than 12 hours ago is ignored.

### ✉️ No warning or stopped email

1. Confirm the subscription email from AWS, and check your spam folder.
2. To send a test, run the `--check` command with `--test-email` added.
3. **Expected quiet:** after one warning, no second one is sent for 60 minutes, even if you reconnect and leave again. The log then says `Warning email skipped`. The limit resets when the instance restarts.
4. **No link in the stopped email?** Run the installer again with `--wake-url <WAKE_URL>` (included in the stack's **GuestInstallCommand**).

If emails keep failing, the instance still stops after 3 tries, so it doesn't run up costs.

**The monthly "still working" note**
- **When it comes:** the first time the instance is running in a new month (UTC), and only if you set up emails. Installing never sends one straight away, because you just got the setup email. The first note arrives next month.
- **No note this month?** The timer can only send it while the instance is running, so a month with no running time has no note. If the instance did run and nothing arrived, check spam, then look at the log: `sudo journalctl -u ec2-idle.service -b | grep -i "monthly note"`. A failed send is tried again within a day.
- **Too many emails?** Run `bash install_guest.sh --monthly-email off`.
- **Test it:** the saved month lives in `/var/lib/ec2-idle/health.json`. Set `"month"` there to last month (for example `{"month": "2026-10"}`), and the next check sends the note.

### 🌐 Waking problems

| You see | Fix |
|---|---|
| `error: Wrong PIN. 4 tries left before a 15-minute lock.` | Check your password manager. After 5 wrong tries the page locks for 15 minutes. |
| `error: Too many wrong PIN tries` | Wait the time it says (up to 15 minutes), then use the right PIN. The lock ends by itself. It's shared by everyone, so someone else's wrong guesses can lock you out too. If you can't wait, [clear the lock](#-locked-out-or-forgot-the-pin) or start the instance from the EC2 console. If you forgot the PIN, [set a new one](#-locked-out-or-forgot-the-pin). |
| It shows `pending`, then back to `stopped` | Check the reason in the EC2 console. If your disk uses your own encryption key, set `EbsKmsKeyArn` in the stack. A capacity shortage usually clears on retry. |
| `409` for a long time | The instance is stuck stopping. Use **Force stop** in the EC2 console. |
| `ready: yes`, but SSH times out | Your security group must allow your IP on port 22. Without a fixed IP, use the new address shown. |
| SSH warns that the host key changed | The IP changed. Use the automatic SSH setup, which handles this ([Setup Step 5](SETUP.md#step-5-choose-how-you-connect)). |
| `AccessDenied` on things that used to work | Check your spend limit in **AWS Settings → Billing**. A paused project can't start instances, and only a project owner can change the limit. To get an email *before* you reach it, [set a spend alert](SETUP.md#-optional-set-a-spend-alert). |

### 🔓 Locked out or forgot the PIN

Don't worry, you can always get back in. The way back is your **AWS account access**, not a second secret, so nobody else can use it. The PIN itself is never shown or sent to you, and the lock stays at 5 wrong tries for 15 minutes.

**Locked (you see "Too many wrong PIN tries")**
- **Easiest:** wait up to 15 minutes. It unlocks by itself.
- **Faster:** clear the lock. You need to be signed in to your AWS account as the owner.
  - **CloudShell:** open the stack's **Outputs** tab, copy **ClearLockCommand**, and run it. It prints nothing when it works. Then use your PIN again. (It is written for CloudShell and other Mac or Linux terminals. In Windows PowerShell the quotes differ, so use CloudShell.)
  - **Console instead:** DynamoDB → **Tables** → the table named in **LockTableName** → **Explore table items** → tick the item whose `id` is `wake` → **Actions → Delete items**.
- **Still in a hurry:** start the instance from the EC2 console (**Instance state → Start instance**). That never needs the PIN.

**Forgot the PIN**
1. Open CloudFormation → `ec2-idle` → **Update → Use existing template**.
2. On the parameters page, type a new **WakePin** (4 to 64 letters and numbers). Finish the update. The old PIN stops working.
3. If the page is still locked, clear the lock as above.
4. Update your bookmark and password manager.

**Good to know**
- **Never paste your PIN into a chat or an AI assistant.** If you have to share it, change it afterwards.
- **No AWS access?** Ask the project owner, or just wait 15 minutes if you only need the lock to end.
- **Why not an emailed reset link?** A link anyone could get into their inbox would weaken the lock. Your AWS sign-in is already protected, so the recovery uses that.

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
