# 🧭 Setup guide

[README](README.md) · [How it works](WORKFLOW.md) · [Agent setup](AGENT_SETUP.md) · [Runbook](RUNBOOK.md) · [Testing](TESTING.md)

> [!TIP]
> **In short**
> - **What you'll do:** create a password (the wake key), create the AWS pieces from one file, then install a small timer on your instance.
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

**The six steps:**

| Step | What | Time |
|---|---|---|
| 0 | Collect your settings | 2 min |
| 1 | Create your wake key | 1 min |
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
bash discover.sh --instance <INSTANCE_ID>  # checks one and suggests settings
```

**You should see:**
- A **Suggested stack parameters** list. Copy it into the table below.
- A **Findings** list. Lines starting with `UNSUPPORTED` or `FIX` must be sorted out first.

### Option B: fill it in yourself ✍️

| Setting | Where to find it | Your value |
|---|---|---|
| Region | AWS Settings → View all projects → Overview → Additional Info → Region | `<REGION>` |
| Instance ID | EC2 console → Instances. It must show **Root device type: EBS**. | `<INSTANCE_ID>` |
| Existing role | EC2 → your instance → **Security** tab → IAM Role. Write "none" if empty. | `<ROLE_NAME_OR_EMPTY>` |
| Login name | `ubuntu` on Ubuntu, `ec2-user` on Amazon Linux, `admin` on Debian | `<SSH_USER>` |
| Disk encryption key | EC2 → Volumes → KMS key. Fill this in only if it isn't `aws/ebs`. | `<KMS_KEY_ARN_OR_EMPTY>` |
| Email for warnings | Optional | `<EMAIL_OR_EMPTY>` |
| IP address | **Fixed** (about US$3.65/month) or **changing** (free, and the wake page shows it) | fixed / changing |
| Daily start time | Optional, for example `cron(0 9 ? * MON-FRI *)` for 9:00 on weekdays, plus a time zone such as `Asia/Kolkata` | `<CRON_OR_EMPTY>` `<TIME_ZONE>` |
| Long jobs | Work that must keep running after you disconnect, such as AI agents, builds or training | `<JOBS_OR_NONE>` |

> [!NOTE]
> Everything used here works on the **AWS Free plan**. Create everything in your project's Region.

---

## Step 1: Create your wake key

The wake key is the password for your wake page. AWS only keeps a scrambled fingerprint of it, called the **hash**.

**▶ Run in:** CloudShell (or a Mac or Linux terminal) · 🔒 **Changes nothing**

```bash
key=$(openssl rand -hex 20); echo "Wake key: $key"; echo "Hash:     $(printf '%s' "$key" | sha256sum | cut -d' ' -f1)"
```

<details>
<summary>On Windows instead (PowerShell)</summary>

```powershell
$b = [byte[]]::new(20); [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); $key = -join ($b | % { $_.ToString('x2') })
"Wake key: $key"; "Hash:     " + (-join ([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($key)) | % { $_.ToString('x2') }))
```

</details>

**You should see:** two lines of random letters and numbers.

> [!IMPORTANT]
> Save the **wake key** in your password manager. You'll need the **hash** in Step 2. Never put either one in Git.

---

## Step 2: Create the AWS pieces

This creates everything in one go from the file `src/ec2-idle-stack.yaml`: the wake page, permissions, email and optional schedule. AWS calls this bundle a **stack**.

**▶ Run in:** AWS console · ✏️ **Changes AWS**

1. Open **CloudFormation → Create stack → With new resources**.
2. Choose **Upload a template file** and pick `ec2-idle-stack.yaml`. Download it first from [src/ec2-idle-stack.yaml](https://github.com/manojvermamv/ec2-idle-stop-wake/blob/main/src/ec2-idle-stack.yaml) (**Download raw file**).
3. Name the stack `ec2-idle`.
4. Fill in your values from Step 0. Only **InstanceId** and **WakeKeySha256** are required.
5. Tick the box that acknowledges IAM resources, then choose **Submit**.
6. Wait for the status **CREATE_COMPLETE**, which takes about 2 minutes.

<details>
<summary>Prefer one command? (CloudShell)</summary>

Run it from the `src` folder you downloaded in Step 0. Leave out any optional line you don't need.

```bash
aws cloudformation deploy --stack-name ec2-idle --template-file ec2-idle-stack.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides \
  InstanceId=<INSTANCE_ID> WakeKeySha256=<WAKE_KEY_HASH> SshUser=<SSH_USER> \
  InstanceRoleName=<ROLE_NAME_OR_EMPTY> \
  NotificationEmail=<EMAIL_OR_EMPTY> \
  WorkHoursStart='<CRON_OR_EMPTY>' ScheduleTimezone=<TIME_ZONE> \
  EbsKmsKeyArn=<KMS_KEY_ARN_OR_EMPTY>
aws cloudformation describe-stacks --stack-name ec2-idle --query 'Stacks[0].Outputs' --output table
```

</details>

<details>
<summary>What every setting means</summary>

| Setting | Default | Meaning |
|---|---|---|
| `InstanceId` | required | Your instance. |
| `WakeKeySha256` | required | The hash from Step 1. |
| `InstanceRoleName` | empty | Your existing role name. If empty, a new role is created. |
| `SshUser` | `ubuntu` | Login name shown in the connect command. |
| `EbsKmsKeyArn` | empty | Only needed if your disk uses your own encryption key. |
| `NotificationEmail` | empty | Where warning emails go. |
| `WorkHoursStart`, `ScheduleTimezone` | empty, `UTC` | Optional daily start. [Schedule format](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html#cron-based) |
| `BackupAlarmHours` | `4` | Email you if the instance runs this many hours at low CPU without stopping. `0` turns it off. |
| `BackupAlarmCpuPercent` | `3` | The CPU level that counts as "low". |
| `BackupAlarmAction` | `notify` | `stop` also stops the instance, but it could interrupt a quiet editing session. |
| `WakeDryRun` | `false` | `true` makes **Wake** only check permission, which is useful for testing. |
| `WakeMaxConcurrency` | `0` | Optional limit on simultaneous wake requests. Keep `0` on new accounts. |

</details>

**When it's done**, open the stack's **Outputs** tab and keep it open:

| Output | What it's for |
|---|---|
| **WakePageUrl** | Your wake page. Bookmark it on your computer and phone. |
| **TerminalWakeCommand** | The one-line wake for any terminal. |
| **GuestInstallCommand** | You'll paste this in Step 4. |
| **AttachRoleCommand** | Shown only if a new role was created. You'll use it in Step 3. |

> [!NOTE]
> If you entered an email, AWS sends a **Subscription Confirmation** email. Click the link in it, or warnings won't arrive.

---

## Step 3: Give the instance its permission

**▶ Run in:** CloudShell · ✏️ **Changes AWS**

**3a.** Do this only if the **AttachRoleCommand** output exists. Copy it from the Outputs tab and run it.

**You should see:** `"State": "associating"`.

**3b. Optional fixed IP.** Do this only if you chose a fixed address.

```bash
alloc=$(aws ec2 allocate-address --query AllocationId --output text)
aws ec2 associate-address --instance-id <INSTANCE_ID> --allocation-id "$alloc"
aws ec2 describe-addresses --allocation-ids "$alloc" --query 'Addresses[0].PublicIp' --output text
```

**You should see:** your new fixed IP. Your current connection drops, so reconnect using the new address.

---

## Step 4: Install the idle timer on the instance

**▶ Run in:** the instance · ✏️ **Changes the instance**

1. Connect to the instance and download this project:

   ```bash
   ssh <SSH_HOST>
   git clone https://github.com/manojvermamv/ec2-idle-stop-wake.git
   cd ec2-idle-stop-wake/src
   ```

   No `git`? Install it with `sudo apt install -y git` (Ubuntu/Debian) or `sudo dnf install -y git` (Amazon Linux).

2. Paste the **GuestInstallCommand** from Step 2. It looks like this:

   ```bash
   bash install_guest.sh --topic <ALERT_TOPIC_ARN>
   ```

**You should see:**

```text
OK    Login session check works (1 remote session(s) now)
OK    Role can stop this instance (DryRunOperation)
OK    Test email accepted by SNS. …
All checks passed.
Installed. Mode stop: warning at 30 idle minutes, stop at 45.
```

> [!WARNING]
> If any line says **FAIL**, nothing was switched on. The line tells you what to fix. Fix it, then run the same command again.

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
| `--dry-run` | Only log what would happen, never stop. |
| `--uninstall` | Remove the timer. |

</details>

---

## Step 5: Choose how you connect

| Way | Setup | Every day |
|---|---|---|
| 🌐 **Wake page** | Bookmark **WakePageUrl**. | Tap **Wake**, then copy the connect command it shows. |
| ⌨️ **Terminal** | None. Works anywhere `curl` exists, including Windows 10+. | Run the **TerminalWakeCommand**, then the `ssh:` line it prints. |
| 📌 **Fixed IP** | Add a normal SSH entry with your fixed IP. | Wake first, then connect as usual. |
| 🔁 **Automatic** (optional) | Add `ec2_up.py` to your SSH config. Needs Python, but no AWS tools. | Just connect. It wakes the instance for you. |

<details>
<summary>Automatic SSH setup (Mac, Linux, Windows)</summary>

1. Save your wake key on one line in `~/.ssh/ec2-wake.key`. On Windows that's `%USERPROFILE%\.ssh\ec2-wake.key`.
2. Add this to `~/.ssh/config`. On Windows the file is `%USERPROFILE%\.ssh\config`.

```sshconfig
Host ec2-workspace
    HostName ec2-workspace
    HostKeyAlias ec2-workspace
    User <SSH_USER>
    IdentityFile ~/.ssh/<YOUR_PRIVATE_KEY>
    ConnectTimeout 360
    ServerAliveInterval 30
    ProxyCommand python /absolute/path/to/ec2-idle-stop-wake/src/ec2_up.py <WAKE_URL> --key-file ~/.ssh/ec2-wake.key --proxy --port %p
```

3. Connect with `ssh ec2-workspace`. VS Code and Cursor can use the same name.

- **Options:**
  - `--no-wake` connects only if the instance is already running.
  - `--private-ip` is for connecting over a VPN.
- **First connection:** SSH asks you to trust the server. To compare its fingerprint, run `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the instance.
- **On Windows:**
  - Use forward slashes in paths (`C:/Users/you/...`).
  - If `python` opens the Microsoft Store, use the full path from `py -0p`.
  - In Windows PowerShell 5.1, type `curl.exe` instead of `curl`.

</details>

---

## Step 6: Test it once

Follow the short live test in [TESTING.md](TESTING.md#-level-6-live-acceptance-cycle). It takes about 5 minutes: the instance stops, you wake it, and you check your files.

---

## ✅ Done checklist

- [ ] Wake key saved in your password manager
- [ ] Stack `ec2-idle` shows **CREATE_COMPLETE**
- [ ] Email subscription confirmed, if you added an email
- [ ] Role attached in Step 3a, if one was created
- [ ] Installer printed **All checks passed** and **Installed**
- [ ] Wake page bookmarked on your computer and phone
- [ ] The 5-minute live test passed

That's it. Everyday use is in the **[Runbook](RUNBOOK.md)**.
