# 🧠 How it works

[README](README.md) · [Setup](SETUP.md) · [Agent setup](AGENT_SETUP.md) · [Runbook](RUNBOOK.md) · [Testing](TESTING.md)

> [!TIP]
> **In short**
> - Two small helpers do all the work:
>   - one **inside the instance**, which stops it when idle;
>   - one **in AWS**, which starts it when you ask.
> - Neither needs your computer.
> - You don't need this page to use the project. It explains the idea and the reasons behind each choice.

---

## 🎯 Goals

| Goal | How it's met |
|---|---|
| Set up once, then forget it | One AWS stack file plus one installer run on the instance. |
| Nothing runs on your computer | Waking runs in AWS. Stopping runs inside the instance. |
| Never cut off real work | Only real connections and declared long jobs count, and it checks once more right before stopping. |
| Never waste money silently | A broken email can't stop the shutdown, failed stops are retried, and an optional backup alarm can watch the timer. |
| Usable from anywhere | Browser, phone, any terminal, code or AI agent, with no AWS login and nothing to install. |

---

## 🧩 The pieces

```mermaid
flowchart TB
    subgraph Cloud["☁️ AWS (one stack, your Region)"]
        URL["🌐 Wake page and API<br/>Lambda Function URL"]
        SCH["⏰ Work-hours start<br/>EventBridge Scheduler (optional)"]
        SNS["✉️ Warning and stopped emails<br/>SNS"]
        CW["🧯 Backup alarm, optional<br/>CloudWatch"]
    end
    subgraph Box["🖥️ Your EC2 instance"]
        TIMER["⏱️ Timer, every minute"] --> PY["ec2_idle.py"]
    end
    URL -->|start| Box
    SCH -->|start| Box
    PY -->|send warning| SNS
    PY -->|stop itself| Box
    CW -->|email you| SNS
```

| Piece | Lives in | Runs when | Allowed to |
|---|---|---|---|
| Idle timer (`ec2_idle.py`) | The instance | Every minute, starting 5 minutes after boot | Stop **its own** instance and send **its own** emails (one warning, one "stopped", and one short monthly "still working" note) |
| Wake page (`wake_ec2.py`) | AWS Lambda | You open it, or call it with your PIN | Look up instances, start **this** instance, and keep its small wrong-PIN counter |
| Work-hours start | AWS EventBridge Scheduler | On your timetable | Start **this** instance |
| Backup alarm (optional, off by default) | AWS CloudWatch | CPU stays under 3% for the hours you chose while running | Email you |
| SSH helper (`ec2_up.py`) | Your computer, optional | You run `ssh` | Try the address first. If it doesn't answer, ask the wake page to start the instance, wait, then connect. |

---

## 💤 How it decides to stop

Once a minute, the timer asks: *is anyone or anything using this instance?*

```mermaid
stateDiagram-v2
    [*] --> Busy: connection or job found
    Busy --> Idle: nothing found, clock starts
    Idle --> Busy: activity returns, clock resets
    Idle --> Warned: 30 idle min, email sent
    Warned --> Busy: activity returns, warning cancelled
    Warned --> LastCheck: 45 idle min and 15 min since email
    LastCheck --> Busy: activity found at the last moment
    LastCheck --> Stopping: still idle, stop requested
    Stopping --> [*]: instance stops
    Stopping --> LastCheck: still running 10 min later, try again
```

**Safety rules**
- ❓ **When unsure, it stays on.** Any error in a check restarts the idle clock instead of moving toward a stop.
- ⏱️ **Long gaps restart the clock.** Checks delayed by a busy host still count. Two checks more than 5 minutes apart (after a pause, for example) start the count over.
- ✉️ **You always get the full warning time.** The 15 minutes count from when the email was actually sent.
- 🧯 **Email trouble can't block the stop.** After 3 failed emails in a row, it stops without one. The "stopped" email is sent after the stop request, so it can never delay it.
- 📬 **Quiet inbox.** At most one warning email per hour, even if you connect and disconnect several times, and one "stopped" email per stop.
- 🩺 **A monthly "still working" note.** The first check of each new month (UTC) sends one short email, so silence never hides a broken timer. The month is saved only after AWS accepts the email, a failed send is retried at most once a day, and it never touches the stop. Turn it off with `--monthly-email off`. It uses a few emails a year, far below the free allowance.
- 🔁 **Every boot starts fresh.** The idle clock and the email limit are kept in memory only, so a restart resets both. The one exception is the monthly note's month and stop count, which are kept on disk (`/var/lib/ec2-idle`), because the instance is stopped and started all the time. The note can only be sent while the instance is running.

### What counts as "in use"

| Keeps it on ✅ | Doesn't keep it on ❌ |
|---|---|
| A logged-in SSH connection, including VS Code and Cursor | Random internet traffic hitting the SSH port, which never logs in |
| EC2 Instance Connect and Session Manager sessions | An editor window you already closed |
| Builds, busy AI agents and training runs, if you turned on `--watch` | Web apps used without an SSH login |
| Anything your own long-job check (probe) reports as busy | Background jobs that neither check covers |
| | An AI agent left open and idle, or a build or training run older than 12 hours |

The ready-made check (`--watch`) reads the process list from `/proc`, so it starts no other program and needs nothing installed. It only lists commands that finish (`npm run build` yes, `npm start` no) and skips zombie and stopped processes. A newly started agent counts once, and after that only while its processor time grows between two checks, and the timer never counts itself or its installer. It is best effort, so use your own probe for anything it doesn't recognise.

---

## 🪶 Light on the host

The timer is built to be invisible, even on the smallest instance or one that's busy with heavy work.

| What | How |
|---|---|
| **Almost no work most minutes** | A normal check starts no other programs. It reads login sessions through systemd's own library and Session Manager sessions from `/proc`, then exits. (The ready-made long-job check, if you turn it on, reads `/proc` once a minute. Your own probe, if you add one, runs once a minute.) |
| **Network code only when needed** | The AWS signing and HTTPS code loads only when an email or a stop is due. |
| **Fast start** | Python runs with `-I -S` (isolated, without site packages). |
| **Low priority** | It runs at `Nice=10` with `CPUWeight=20` and low IO priority, so your workloads always come first. |
| **Capped memory** | A soft limit of 48 MB and a hard limit of 96 MB. |
| **Copes with a busy host** | Runs delayed by up to 5 minutes still count toward the idle time. Only a longer pause restarts the clock. |
| **Fails safe** | If a check can't finish, for example under memory pressure, the instance simply stays on. It never stops by mistake. |

---

## 🌍 Runs on many systems

The timer is meant to run on almost any Linux instance, so it is built from as little as possible.

| Choice | Why it helps |
|---|---|
| **Python standard library only** | Nothing to install from a package index, and no compiled parts. The timer works on x86_64 and on Graviton (arm64) alike. |
| **Python 3.6 or newer** | RHEL, Rocky and Alma 8, SUSE 15 and Amazon Linux 2 ship 3.6 or 3.7. Current releases ship 3.12 to 3.14. The tests run on every version from 3.6 to 3.14. |
| **The installer finds or installs Python** | It knows apt, dnf, yum, zypper, pacman and apk, so a minimal image still works. |
| **Logged-in sessions come from systemd** | The same idea `autosuspend`, a popular suspend-when-idle daemon, uses. It makes VS Code and Cursor sessions count, and scanners not. |
| **State lives in `/run/ec2-idle`** | It's created by the service itself, because the usual systemd setting for it only exists from version 235. Older systems such as Amazon Linux 2 would lose it between runs. |
| **Older systemd is tolerated** | Resource limits the system doesn't know are ignored with a warning, not an error. |
| **systemd is required** | Alpine, other non-systemd systems and containers aren't supported. Windows and Mac instances aren't either. |

`autosuspend` needs Python 3.11 or newer plus `psutil`, `dbus-python` and `PyGObject`, and it ships as distribution packages. That suits a desktop or home server. An EC2 instance can be any age and any distribution, so this project ships one installer and no dependencies instead.

---

## 🌐 How waking works

```mermaid
sequenceDiagram
    autonumber
    participant You as 🧑 You (browser, terminal, code)
    participant L as 🌐 Wake page (AWS Lambda)
    participant E as 🖥️ EC2 instance
    You->>L: wake + your PIN
    L->>L: locked after wrong tries? PIN right?
    alt locked or wrong PIN
        L-->>You: "Wrong PIN" or "try again later"
    else instance is stopping
        L->>E: wait until stopped, then start
    else instance is stopped
        L->>E: start
    end
    loop every 4 seconds, up to the wait time
        L->>E: is it ready?
    end
    L-->>You: ready, with IP, connect command and progress
```

| You use | You get | It waits |
|---|---|---|
| A browser | The wake page, which shows each step as it happens | The page keeps checking by itself |
| A terminal (`curl -d …`) | Easy-to-read text | Up to 2 minutes |
| Code (JSON) | JSON | Only if you ask, up to 150 seconds |
| `curl <WAKE_URL>` with nothing else | Instructions | – |
| The schedule | Starts the instance directly | – |

---

## 💡 Why it's built this way

| Choice | Why | Not chosen |
|---|---|---|
| A public link protected by a short PIN | It works on any device with no AWS login. The worst a stranger with the PIN can do is start it, and it stops again by itself. | AWS-signed requests, which need AWS tools and logins that expire. |
| 5 wrong tries lock the page for 15 minutes | About 480 guesses a day at most, so a short PIN is slow to guess (a longer mix is safer). A tiny table counts the tries, and if it can't, the page stays closed. | A long random key, which is a pain to type and move around. |
| Recovery uses your AWS sign-in, not a second secret | If the PIN is forgotten or the page is locked, you clear the lock (the stack's **ClearLockCommand** output deletes the counter) or set a new PIN by updating the stack. Both need AWS account access, which already guards everything else. The PIN is never read back, and the lock stays at 5 tries for 15 minutes. | An emailed reset link or a recovery code. Those add a way in for anyone who gets the email or loses the code. |
| The PIN is stored as a hidden stack setting | Hashing a short PIN adds no real protection, and setup stays one simple step. | A hash you had to compute by hand. |
| The timer talks to AWS directly | Nothing extra to install on the instance. | Installing the AWS command-line tools. |
| Only logged-in sessions count | Internet scanners can't keep it awake. | Counting any network connection. |
| The backup alarm only emails | Quiet editing uses little CPU, so an automatic stop could cut you off. | Stopping automatically on low CPU. |
| The instance finds its own ID | Nothing to type wrong, and it can only ever stop itself. | Typing the ID into a settings file. |
| Terminal wakes wait before replying | One command gives you a final answer. | Live streaming, which this Lambda runtime can't do. |
| Spend alerts are a guide, not part of the stack | A budget belongs to your whole account, so a stack-owned one would duplicate and vanish with the stack. Some accounts limit Budgets, and a resource that can't be created would break the stack. Docs can't ([how to set one](SETUP.md#-optional-set-a-spend-alert)). | A budget resource in the stack. |
| No "wake on SSH attempt" | AWS only notices such attempts minutes later, and not reliably. | Watching network logs. |

---

## 🚧 Limits

- 1️⃣ **One instance per stack.** For another instance, create another stack with a different name.
- ⏳ **Terminal progress arrives at the end** of the wait. Repeat the status command to follow along live.
- 🧯 **The backup alarm only sees CPU,** not your connections. That's why it emails rather than stops.
- 🔒 **The wrong-PIN lock is shared by everyone,** including you. Someone guessing wrong can lock the page for 15 minutes. You can still start the instance from the EC2 console.
- 🌍 **The IP address changes** at each start unless you choose a fixed IP.
- 🐧 **Linux only.** Windows instances aren't supported.
