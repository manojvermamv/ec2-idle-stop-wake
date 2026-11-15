# 🧭 Setup guide

[README](README.md) · [How it works](WORKFLOW.md) · [Agent setup](AGENT_SETUP.md) · [Runbook](RUNBOOK.md) · [Testing](TESTING.md)

> [!TIP]
> **In short**
> - **What you'll do:** choose a short PIN, create the AWS pieces from one file, then install a small timer on your instance.
> - **Time:** about 15 minutes, once.
> - **You need:** the AWS console and your usual way of connecting to the instance. Nothing gets installed on your computer.
>
> Prefer an AI assistant? Use the [one-line prompt](README.md#-let-an-ai-agent-do-it) instead.

---

## 🧰 Before you start

**How to read the steps:**
- **▶ Run in:** says where to type the commands:
  - **CloudShell** is the terminal inside the AWS console. Open it from the icon in the top bar.
  - **Instance** means your EC2 instance, connected over SSH.
  - **Your computer** means a terminal on your own machine.
- 🔒 **Changes nothing** / ✏️ **Changes AWS** tells you whether a step changes anything.
- **You should see** shows what success looks like.
- `<LIKE_THIS>` marks a value to replace. You collect these values in Step 0.

> [!NOTE]
> **New AWS account? CloudShell may not open yet.** While AWS verifies a new account (up to two days), CloudShell can show *"Unable to create the environment. Your account verification is in progress"*. You can still finish setup without it:
> - **Step 0:** use Option B, or an AI assistant with an AWS MCP server.
> - **Step 1:** nothing to do, because you just choose a PIN.
> - **Step 2:** use the console upload.
> - **Step 3:** use the console steps.

**The seven steps (0 to 6):**

| Step | What | Time |
|---|---|---|
| 0 | Collect your settings | 2 min |
| 1 | Choose your PIN | 10 sec |
| 2 | Create the AWS pieces | 4 min |
| 3 | Give the instance its permission (and an optional fixed IP) | 1 min |
| 4 | Install the idle timer on the instance | 3 min |
| 5 | Choose how you connect | 2 min |
| 6 | Test it once | 5 min |

---

## Step 0: Plan your setup

Collect the values the next steps need. You can let a script find most of them, or fill them in yourself.

### Option A: let the check script find them 🔍

**▶ Run in:** CloudShell · 🔒 **Changes nothing**

1. In CloudShell, download this project (CloudShell already has `git`).
2. Run:

```bash
git clone https://github.com/manojvermamv/ec2-idle-stop-wake.git && cd ec2-idle-stop-wake/src
bash discover.sh                           # lists your instances
bash discover.sh --instance <INSTANCE_ID>  # checks one
```

It checks the 5 things that can block setup: an EBS root disk, not Windows or Spot, instance metadata on, an existing role, and your Elastic IP and SSH route.

**You should see:**
- A **Suggested stack settings** line. Copy it into the table below.
- A **Findings** list:
  - `UNSUPPORTED` or `FIX` lines must be sorted out first.
  - `KEEP` means keep what you have, for example an Elastic IP that's already attached.
  - `NOTE` and `RECOMMEND` are worth a look but don't block anything.
  - `CHECK` means look before continuing. For example, "no SSH route from outside" means you'll need one of the ways in Step 4 to reach the instance.

### Option B: fill it in yourself ✍️

| Setting | Where to find it | Your value |
|---|---|---|
| Region | AWS Settings → View all projects → Overview → Additional Info → Region | `<REGION>` |
| Instance ID | EC2 console → Instances. It must show **Root device type: EBS**. | `<INSTANCE_ID>` |
| Existing role | EC2 → your instance → **Security** tab → IAM Role. Write "none" if empty. | `<ROLE_NAME_OR_EMPTY>` |
| Login name | `ubuntu` on Ubuntu, `ec2-user` on Amazon Linux, `admin` on Debian | `<SSH_USER>` |
| Disk encryption key | EC2 → Volumes → KMS key. Fill this in only if it isn't `aws/ebs`. | `<KMS_KEY_ARN_OR_EMPTY>` |
| Email for warnings | Optional | `<EMAIL_OR_EMPTY>` |
| IP address | **Already has an Elastic IP?** Keep it and skip Step 3b. Otherwise choose **fixed** (about US$3.65/month) or **changing** (free, and the wake page shows it). | keep / fixed / changing |
| Daily start time | Optional, for example `cron(0 9 ? * MON-FRI *)\|Asia/Kolkata` for 9:00 on weekdays in India. The `\|` and time zone are optional (UTC without them). | `<SCHEDULE_OR_EMPTY>` |
| Long jobs | Work that must keep running after you disconnect, such as AI agents, builds or training | `<JOBS_OR_NONE>` |

> [!NOTE]
> Everything used here works on the **AWS Free plan**. Create everything in your project's Region.

---

## Step 1: Choose your PIN

Your PIN is the password for your wake page. Pick **4 to 64 letters and numbers**, for example `4821`. You'll type it into the stack form in Step 2, so there's nothing to generate.

- **A short PIN is fine.** After 5 wrong tries the page locks for 15 minutes, so it can't be guessed, and the PIN can only *start* the instance.
- **Longer is stronger.** A mix such as `blue7tiger` is easy to remember and much harder to guess.
- **Save it in your password manager,** and keep it out of Git.

---

## Step 2: Create the AWS pieces

This creates everything in one go from the file `src/ec2-idle-stack.yaml`: the wake page, permissions, email and optional schedule. AWS calls this bundle a **stack**.

**▶ Run in:** AWS console · ✏️ **Changes AWS**

1. Open **CloudFormation → Create stack → With new resources**.
2. Choose **Upload a template file** and pick `ec2-idle-stack.yaml`. Download it first from [src/ec2-idle-stack.yaml](https://github.com/manojvermamv/ec2-idle-stop-wake/blob/main/src/ec2-idle-stack.yaml) (**Download raw file**).
3. Name the stack `ec2-idle`.
4. Fill in your values from Step 0. The form has two groups: **Basic** (instance, PIN and email) and **Advanced** (everything optional). Only **InstanceId** and **WakePin** (your PIN) are required.
5. Tick the box that acknowledges IAM resources, then choose **Submit**.
6. Wait for the status **CREATE_COMPLETE**, which takes about 2 minutes.

<details>
<summary>Prefer one command? (CloudShell)</summary>

Run it from the `src` folder you downloaded in Step 0. Leave out any optional line you don't need.

```bash
aws cloudformation deploy --stack-name ec2-idle --template-file ec2-idle-stack.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides \
  InstanceId=<INSTANCE_ID> WakePin=<PIN> SshUser=<SSH_USER> \
  InstanceRoleName=<ROLE_NAME_OR_EMPTY> \
  NotificationEmail=<EMAIL_OR_EMPTY> \
  WorkHoursStart='<SCHEDULE_OR_EMPTY>' \
  EbsKmsKeyArn=<KMS_KEY_ARN_OR_EMPTY>
aws cloudformation describe-stacks --stack-name ec2-idle --query 'Stacks[0].Outputs' --output table
```

</details>

<details>
<summary>What every setting means</summary>

| Setting | Default | Meaning |
|---|---|---|
| `InstanceId` | required | Your instance. |
| `WakePin` | required | Your PIN from Step 1 (4 to 64 letters and numbers). It's hidden in the stack form and details. |
| `InstanceRoleName` | empty | Your existing role name. If empty, a new role is created. |
| `SshUser` | `ubuntu` | Login name shown in the connect command. |
| `EbsKmsKeyArn` | empty | Only needed if your disk uses your own encryption key. |
| `NotificationEmail` | empty | Where the warning and stopped emails go. |
| `WorkHoursStart` | empty | Optional daily start: a cron expression, then optionally `\|` and a time zone, for example `cron(0 9 ? * MON-FRI *)\|Asia/Kolkata`. Without a time zone it runs in UTC, and it follows daylight saving when you give a zone. [Cron format](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html#cron-based) |
| `BackupAlarmHours` | `0` (off) | Optional safety net. Set it to `4` to get an email if the instance runs 4 hours under 3% CPU without stopping, which could mean the idle timer broke. It needs `NotificationEmail`. A long quiet session, such as an open editor, can also trigger it. |

</details>

**When it's done**, open the stack's **Outputs** tab and keep it open:

| Output | What it's for |
|---|---|
| **WakePageUrl** | Your wake page. Bookmark it on your computer and phone. |
| **OneTapBookmark** | The same link ending in `#YOUR_PIN`. Replace `YOUR_PIN` with your PIN and bookmark that for **one-tap waking**. The part after `#` is never sent to AWS. |
| **TerminalWakeCommand** | The one-line wake for any terminal. |
| **AlertTopicArn** | Only if you entered an email. It's already inside **GuestInstallCommand**. |
| **GuestInstallCommand** | You'll paste this in Step 4. |
| **AttachRoleCommand** | Shown only if a new role was created. You'll use it in Step 3. |

> [!IMPORTANT]
> If you entered an email, AWS sends a **Subscription Confirmation** email. Click **Confirm subscription** now, **before Step 4**, or the installer's test email and later warnings won't arrive.

> [!NOTE]
> **If you turned on the optional backup alarm** (`BackupAlarmHours` above 0), an "ALARM" email within about 20 minutes of this step is normal when the instance was already sitting idle. The alarm looks back over the last few hours and sees no timer stopping the instance yet. It clears by itself once the timer is installed and the instance stops. With the alarm off, which is the default, you won't get it.

---

## Step 3: Give the instance its permission

**▶ Run in:** CloudShell or the AWS console · ✏️ **Changes AWS**

**3a.** Do this only if the **AttachRoleCommand** output exists.
- **CloudShell:** copy the command from the Outputs tab and run it. **You should see:** `"State": "associating"`.
- **Console:** EC2 → your instance → **Actions → Security → Modify IAM role**. Choose the profile named in **AttachRoleCommand** (it starts with `ec2-idle-GuestInstanceProfile-`), then choose **Update IAM role**.

Wait about **1 minute** before Step 4, so the instance can start using the role.

**3b. Optional fixed IP.** Do this only if you chose a new fixed address. Skip it if the instance already has an Elastic IP.

```bash
alloc=$(aws ec2 allocate-address --query AllocationId --output text)
aws ec2 associate-address --instance-id <INSTANCE_ID> --allocation-id "$alloc"
aws ec2 describe-addresses --allocation-ids "$alloc" --query 'Addresses[0].PublicIp' --output text
```

**You should see:** your new fixed IP. Your current connection drops, so reconnect using the new address.

---

## Step 4: Install the idle timer on the instance

**▶ Run in:** the instance · ✏️ **Changes the instance**

> [!CAUTION]
> **Run these commands ON THE EC2 INSTANCE, not on your own computer.** In Windows PowerShell, `bash install_guest.sh` either fails with *"Windows Subsystem for Linux has no installed distributions"* or stops with *"run this ON THE EC2 INSTANCE"*. Either way, nothing gets installed on the instance.

**First, open a terminal on the instance.** Use whichever of these works for you:

| Way | Needs |
|---|---|
| **SSH** from your computer | An inbound port 22 rule for your IP in the instance's security group |
| **EC2 Instance Connect** (EC2 console → instance → **Connect**) | An inbound port 22 rule for the AWS prefix list `com.amazonaws.<REGION>.ec2-instance-connect` |
| **EC2 Instance Connect Endpoint** | An endpoint in the instance's VPC. It has no endpoint fee and needs no public SSH. |
| **Session Manager** | The `AmazonSSMManagedInstanceCore` policy on the instance role |

> [!TIP]
> If none of these is set up, add a **temporary** inbound rule: port 22 from **My IP** only. Run the install, then delete the rule. The idle timer doesn't need it afterwards.

1. On the instance, download this project:

   ```bash
   git clone https://github.com/manojvermamv/ec2-idle-stop-wake.git
   cd ec2-idle-stop-wake/src
   ```

   No `git`? Install it with `sudo apt install -y git` (Ubuntu/Debian) or `sudo dnf install -y git` (Amazon Linux).

2. Paste the **GuestInstallCommand** from Step 2. It looks like this:

   ```bash
   bash install_guest.sh --topic <ALERT_TOPIC_ARN> --wake-url <WAKE_URL>
   ```

   Optionally add `--name my-dev-box` to use a friendly name in emails. Otherwise the instance ID is used.

**You should see:**

```text
Checking this host...
Warn after 30 min, stop after 45 min.
OK    Login session check works (1 remote session(s) now)
OK    Session Manager check works (open: no)
OK    Instance my-dev-box (i-0abc…) in ap-southeast-2 with role ec2-idle-GuestRole-…
OK    Role can stop this instance (DryRunOperation)
OK    Test email accepted by SNS. Confirm the subscription email if you have not.
All checks passed.

Installed. Warning at 30 idle minutes, stop at 45.
Emails: one warning (at most one per hour), then one 'stopped' email with your wake page link.
Logs: sudo journalctl -u ec2-idle.service -b -n 30 --no-pager
```

The test email line appears only on the first install or when the email topic changes. The instance shows as its ID, such as `i-0abc…`, unless you add `--name`.

> [!WARNING]
> If any line says **FAIL**, nothing was switched on. The line tells you what to fix. Fix it, then run the same command again.
> - **Role** errors right after Step 3: wait a minute, then try again.

**Double-check it's installed:** `systemctl list-timers ec2-idle.timer --no-pager` shows one timer. If it says **0 timers listed**, the install didn't happen on this instance.

<details>
<summary>Keep long jobs running after you disconnect</summary>

By default, the instance stops 45 minutes after your last connection closes. If an AI agent, build or training job must keep running, give the timer a small "is my job running?" check (a **probe**). It answers 0 for busy and 1 for idle.

1. On the instance, save this as `/usr/local/sbin/ec2-work-probe`, with your own process names:

   ```sh
   #!/bin/sh
   # Busy while a training script or an AI agent runs as user ubuntu.
   pgrep -u ubuntu -f -- '^(/usr/bin/)?python3 /home/ubuntu/jobs/train\.py( |$)' >/dev/null && exit 0
   pgrep -u ubuntu -x claude >/dev/null && exit 0
   exit 1
   ```

2. Make it safe and runnable:

   ```bash
   sudo chown root: /usr/local/sbin/ec2-work-probe && sudo chmod 755 /usr/local/sbin/ec2-work-probe
   ```

3. Run the installer again with `--probe /usr/local/sbin/ec2-work-probe`.

**You should see:** `OK    Workload probe works`.

</details>

<details>
<summary>Other installer options</summary>

| Option | Effect |
|---|---|
| `--warn 20 --stop 30` | Change the timings. The defaults are 30 and 45 minutes. |
| `--name my-dev-box` | A friendly name for the emails. |
| `--probe /path` | The long-job check from above. `--probe none` removes it. |
| `--test-email` | Send the setup test email again. |
| `--uninstall` | Remove the timer but keep its settings. |
| `--uninstall --purge` | Remove the timer and also delete its settings and saved state. |

Anything you leave out (email topic, wake link, name, long-job check) is kept from the last install.

</details>

---

## Step 5: Choose how you connect

| Way | Setup | Every day |
|---|---|---|
| 🌐 **Wake page** | Bookmark **WakePageUrl**. | Tap **Wake**, then copy the connect command it shows. If SSH from outside is blocked, connect with the console's **Connect** button instead. |
| ⌨️ **Terminal** | None. Works anywhere `curl` exists, including Windows 10+. | Run the **TerminalWakeCommand**, then the `ssh:` line it prints. |
| 📌 **Fixed IP** | Add a normal SSH entry with your fixed IP. | Wake first, then connect as usual. |
| 🔁 **Automatic** (optional) | Add `ec2_up.py` to your SSH config. Needs Python, but no AWS tools. | Run your normal `ssh` command. It **waits while the instance wakes, then connects**, so there's nothing to retry. |

<details>
<summary>Automatic SSH setup (Mac, Linux, Windows)</summary>

This needs your computer to be able to reach the instance's SSH port (see the ways in Step 4). If it can't, use the console's **Connect** button instead.

1. Save your PIN on one line in `~/.ssh/ec2-wake.pin`. On Windows that's `%USERPROFILE%\.ssh\ec2-wake.pin`.
2. Add this to `~/.ssh/config`. On Windows the file is `%USERPROFILE%\.ssh\config`.

```sshconfig
Host ec2-workspace
    HostName <FIXED_IP_OR_ec2-workspace>
    HostKeyAlias ec2-workspace
    User <SSH_USER>
    IdentityFile ~/.ssh/<YOUR_PRIVATE_KEY>
    ConnectTimeout 360
    ServerAliveInterval 30
    ProxyCommand python /absolute/path/to/ec2-idle-stop-wake/src/ec2_up.py <WAKE_URL> %h %p --pin-file ~/.ssh/ec2-wake.pin
```

3. Connect with `ssh ec2-workspace`. VS Code and Cursor can use the same name.

**How it behaves:**
- **Already running:** with a **fixed IP**, put it in `HostName`. The helper tries it for up to 3 seconds and connects straight away without calling the wake page.
- **Stopped:** it asks the wake page to start the instance and wait (about one request), then connects to the IP the page reports. Your original `ssh` request carries on by itself.
- **Changing IP:** keep a made-up name such as `ec2-workspace` in `HostName`. It can't be reached, so the helper skips straight to the wake page for the current IP.
- **Wrong PIN or locked page:** `ssh` stops with the message, for example `ec2-up: Too many wrong PIN tries. Try again in 15 minutes.`
- **Keep `ConnectTimeout` above the wake time.** 360 is fine, so ssh doesn't give up first.

**Options** (add them to the `ProxyCommand` line):
- `--no-wake` connects only if the instance is already running.
- `--private-ip` is for connecting over a VPN.
- `--timeout 600` waits longer than the default 300 seconds.

**To try it by hand** (not in the config), add `--check`: it wakes the instance and prints the address, without relaying SSH.

**Good to know**
- **First connection:** SSH asks you to trust the server. To compare its fingerprint, run `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the instance.
- **On Windows:**
  - Use forward slashes in paths (`C:/Users/you/...`).
  - If `python` opens the Microsoft Store, use the full path from `py -0p`.
  - In Windows PowerShell 5.1, type `curl.exe` instead of `curl`.

</details>

---

## Step 6: Test it once

Follow the short live test in [TESTING.md](TESTING.md#-level-5-live-acceptance-cycle). It takes about 5 minutes: the instance stops, you wake it, and you check your files.

---

## ✅ Done checklist

- [ ] PIN saved in your password manager
- [ ] Stack `ec2-idle` shows **CREATE_COMPLETE**
- [ ] Email subscription confirmed, if you added an email
- [ ] Role attached in Step 3a, if one was created
- [ ] Installer printed **All checks passed** and **Installed**, run **on the instance**
- [ ] `systemctl list-timers ec2-idle.timer` on the instance shows the timer
- [ ] Wake page bookmarked on your computer and phone
- [ ] The 5-minute live test passed

That's it. Everyday use is in the **[Runbook](RUNBOOK.md)**.
