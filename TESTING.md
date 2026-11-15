# 🧪 Testing

[README](README.md) · [How it works](WORKFLOW.md) · [Setup](SETUP.md) · [Agent setup](AGENT_SETUP.md) · [Runbook](RUNBOOK.md)

> [!TIP]
> **In short**
> - **Most people need one test:** the 5-minute [live test](#-level-6-live-acceptance-cycle) after setup. The instance stops, you wake it, and your files are still there.
> - **The other levels** are for checking before going live, or after changing the code.

---

## 🪜 Test levels

Each level proves a bit more. The first five never stop your instance.

| Level | Proves | Where | Safe? |
|---|---|---|---|
| 🔵 1. Unit tests | The program logic works. | Any computer | Yes |
| 🔵 2. Template check | The AWS stack file is valid. | Any computer | Yes |
| 🔵 3. Account check | Your account and instance are suitable. | CloudShell | Yes, read-only |
| 🟡 4. Instance check | Connections, permissions and email work on the instance. | Instance | Sends one test email |
| 🟡 5. Practice run | The timer would warn and stop on time, but doesn't. | Instance | Yes |
| 🟡 5b. Practice wake | The wake page is allowed to start the instance. | Browser or terminal | Yes |
| 🟢 6. Live test | Everything, for real. | Everywhere | **Stops the instance** |

---

## 🔵 Level 1: Unit tests

**▶ Run in:** any computer with Python 3.8 or newer. No AWS access is needed.

```bash
python -B -m unittest discover -s src -v
```

**You should see:** `OK` at the end.

<details>
<summary>What the tests cover</summary>

| Group | Covers |
|---|---|
| `IdleTests` | Warns once and stops once, activity resets the clock, gaps, last-moment check, email failures, stop retry, settings checks |
| `ActivityTests` | Counting logged-in sessions, Session Manager, long-job check results |
| `SigningTests` | AWS request signing, against AWS's published example (and against the official AWS library, botocore, when it's installed) |
| `CheckTests` | `--check` passes and fails correctly |
| `WakeClientTests` | The optional SSH helper: waiting, `--no-wake`, wrong key, data relay |
| `LambdaTests` | Wake page vs instructions, text vs JSON, waiting and progress, keys, dry run |
| `TemplateTests` | The stack file contains the current wake page code |

</details>

## 🔵 Level 2: Template check

**▶ Run in:** any computer

```bash
pip install cfn-lint && cfn-lint src/ec2-idle-stack.yaml
```

**You should see:** no output.

## 🔵 Level 3: Account check

**▶ Run in:** CloudShell · 🔒 **Changes nothing**

```bash
cd ec2-idle-stop-wake/src && bash discover.sh --instance <INSTANCE_ID>
```

**You should see:** no `UNSUPPORTED` or `FIX` lines under **Findings**.

## 🟡 Level 4: Instance check

The installer already runs this. To run it again, **▶ run in:** the instance

```bash
sudo bash -c 'set -a; . /etc/ec2-idle.conf; exec python3 /usr/local/sbin/ec2_idle.py --check --expect-session --test-email'
```

**You should see:** every line starting with `OK`, then `All checks passed.`, and a test email arrives.

## 🟡 Level 5: Practice run

**▶ Run in:** the instance, in `~/ec2-idle-stop-wake/src`. It uses a temporary timer with 1- and 2-minute times and a fake job, and **never stops** the instance.

```bash
sudo -v && bash verify_guest.sh
```

**You should see**, after about 3 minutes: `Would publish warning.`, then `Would call EC2 StopInstances.`, then `Dry-run cycle passed.`

## 🟡 Level 5b: Practice wake

1. Update the stack and set `WakeDryRun` to `true`.
2. Run:

   ```bash
   curl -d key=<WAKE_KEY> -d action=wake <WAKE_URL>
   ```

3. Set `WakeDryRun` back to `false`.

**You should see:**
- If the instance is stopped: `message: Dry run. Start permission verified.`
- If it's running: `ready: yes`.

## 🟢 Level 6: Live acceptance cycle

> [!WARNING]
> This **really stops your instance**. Save your work first. If waking fails, use the [other ways to wake](RUNBOOK.md#-if-waking-doesnt-work).

| Step | Do this | Where | You should see |
|---|---|---|---|
| 1 | `bash install_guest.sh --topic <ALERT_TOPIC_ARN> --warn 2 --stop 4`, **on the instance** (not your PC) | Instance | `Installed. Mode stop: warning at 2 …` |
| 2 | Close **every** connection and editor window | Your devices | – |
| 3 | Wait about 3 minutes | – | The warning email arrives |
| 4 | Wait about 2 more minutes | EC2 console | **Stopped** |
| 5 | `curl -d key=<WAKE_KEY> -d action=wake <WAKE_URL>` | Any terminal | `ready: yes` and an `ssh:` line |
| 6 | Connect and look at your files | Instance | Everything is still there |
| 7 | `bash install_guest.sh --topic <ALERT_TOPIC_ARN>` | Instance | Back to 30 and 45 minutes |

**Optional extra checks**
- [ ] **Schedule:** set the start time a few minutes ahead while the instance is stopped. It should start on time.
- [ ] **Long-job check:** start your real job, disconnect, and wait past the stop time. The instance should keep running, and the log should say `Busy (workload)`.
- [ ] **Phone:** wake from your phone. The page should show `stopped → pending → running → ready`.

---

## 🔁 After changing code

| You changed | Then run |
|---|---|
| `src/wake_ec2.py` | `python src/sync_template.py`, then levels 1 and 2, then update the stack |
| `src/ec2_idle.py` or the timer files | Level 1, then the installer again (levels 4 and 5) |
| `src/ec2-idle-stack.yaml` | Level 2, then update the stack |
| `src/ec2_up.py` | Level 1, then connect with `ssh ec2-workspace` |

## 📋 Results so far

| Check | Result |
|---|---|
| 35 unit tests on Windows, Python 3.13, with and without botocore | ✅ Passed |
| Request signing matches AWS's published example and botocore | ✅ Passed |
| Template check (`cfn-lint`) | ✅ Clean |
| `discover.sh` against simulated AWS answers | ✅ Passed |
| Services allowed on the AWS Free plan | ✅ Confirmed from AWS docs |
| Live run on a real account (Ubuntu 24.04, m7i-flex.large, Sydney), deployed by a fresh AI agent from this repository through an AWS MCP server | ✅ Passed |
| Level 4: instance check, test email delivered | ✅ Passed |
| Level 6: warning at 2 idle minutes, stop requested at about 4.3, **Stopped** about 35 s later | ✅ Passed |
| Level 6: wake through the API, `ready: yes` in 4 s, files intact | ✅ Passed |
| Level 5 practice run (`verify_guest.sh`) on a real instance | ⏳ Not run yet |
