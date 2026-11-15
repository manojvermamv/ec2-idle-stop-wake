# How it works

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
| Never waste money silently | A broken email can't stop the shutdown, failed stops are retried, and a backup alarm watches. |
| Usable from anywhere | Browser, phone, any terminal, code or AI agent, with no AWS login and nothing to install. |

---

## 🧩 The pieces

```mermaid
flowchart TB
    subgraph Cloud["AWS (one stack, your Region)"]
        URL["Wake page and API<br/>Lambda Function URL"]
        SCH["Work-hours start<br/>EventBridge Scheduler (optional)"]
        SNS["Warning email<br/>SNS"]
        CW["Backup alarm<br/>CloudWatch"]
    end
    subgraph Box["Your EC2 instance"]
        TIMER["Timer, every minute"] --> PY["ec2_idle.py"]
    end
    URL -->|start| Box
    SCH -->|start| Box
    PY -->|send warning| SNS
    PY -->|stop itself| Box
    CW -->|email you| SNS
```

| Piece | Lives in | Runs when | Allowed to |
|---|---|---|---|
| Idle timer (`ec2_idle.py`) | The instance | Every minute, starting 5 minutes after boot | Stop **its own** instance and send **its own** warning email |
| Wake page (`wake_ec2.py`) | AWS Lambda | You open it, or call it with the wake key | Look up instances, and start **this** instance |
| Work-hours start | AWS EventBridge Scheduler | On your timetable | Start **this** instance |
| Backup alarm | AWS CloudWatch | CPU stays low for hours while running | Email you (or, if you opt in, stop the instance) |
| SSH helper (`ec2_up.py`) | Your computer, optional | You run `ssh` | Call the wake page, then connect |

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
- **When unsure, it stays on.** Any error in a check restarts the idle clock instead of moving toward a stop.
- **Gaps restart the clock.** Two checks more than 2.5 minutes apart (after a pause, for example) start the count over.
- **You always get the full warning time.** The 15 minutes count from when the email was actually sent.
- **Email trouble can't block the stop.** After 3 failed emails in a row, it stops without one.
- **Every boot starts fresh.** The idle clock is kept in memory only.

### What counts as "in use"

| Keeps it on | Doesn't keep it on |
|---|---|
| A logged-in SSH connection, including VS Code and Cursor | Random internet traffic hitting the SSH port, which never logs in |
| EC2 Instance Connect and Session Manager sessions | An editor window you already closed |
| Anything your long-job check reports as busy | Web apps used without an SSH login |
| | Background jobs you didn't add to the long-job check |

---

## 🌐 How waking works

```mermaid
sequenceDiagram
    autonumber
    participant You as You (browser, terminal, code)
    participant L as Wake page (AWS Lambda)
    participant E as EC2 instance
    You->>L: wake + your wake key
    L->>L: check the key's fingerprint
    alt wrong key
        L-->>You: "Wrong wake key"
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
| A public link protected by a wake key | It works on any device with no AWS login. The worst a stranger with the key can do is start it, and it stops again by itself. | AWS-signed requests, which need AWS tools and logins that expire. |
| AWS stores only the key's fingerprint | Nothing secret is stored in AWS. | Storing the key itself. |
| The timer talks to AWS directly | Nothing extra to install on the instance. | Installing the AWS command-line tools. |
| Only logged-in sessions count | Internet scanners can't keep it awake. | Counting any network connection. |
| The backup alarm only emails by default | Quiet editing uses little CPU, so an automatic stop could cut you off. | Stopping automatically on low CPU. |
| The instance finds its own ID | Nothing to type wrong, and it can only ever stop itself. | Typing the ID into a settings file. |
| Terminal wakes wait before replying | One command gives you a final answer. | Live streaming, which this Lambda runtime can't do. |
| No "wake on SSH attempt" | AWS only notices such attempts minutes later, and not reliably. | Watching network logs. |

---

## 🚧 Limits

- **One instance per stack.** For another instance, create another stack with a different name.
- **Terminal progress arrives at the end** of the wait. Repeat the status command to follow along live.
- **The backup alarm only sees CPU,** not your connections. That's why it emails rather than stops.
- **The IP address changes** at each start unless you choose a fixed IP.
- **Linux only.** Windows instances aren't supported.
