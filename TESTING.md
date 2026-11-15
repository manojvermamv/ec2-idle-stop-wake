# 🧪 Testing

[README](README.md) · [How it works](WORKFLOW.md) · [Setup](SETUP.md) · [Agent setup](AGENT_SETUP.md) · [Runbook](RUNBOOK.md)

> [!TIP]
> **In short**
> - **Most people need one test:** the 5-minute [live test](#-level-5-live-acceptance-cycle) after setup. The instance stops, you wake it, and your files are still there.
> - **The other levels** are for checking before going live, or after changing the code.

---

## 🪜 Test levels

Each level proves a bit more. The first four never stop your instance.

| Level | Proves | Where | Safe? |
|---|---|---|---|
| 🔵 1. Unit tests | The program logic works. | Any computer | Yes |
| 🔵 2. Template check | The AWS stack file is valid. | Any computer | Yes |
| 🔵 3. Account check | Your account and instance are suitable. | CloudShell | Yes, read-only |
| 🟡 4. Instance check | Connections, permissions and email work on the instance. | Instance | Sends one test email (first install only) |
| 🟢 5. Live test | Everything, for real. | Everywhere | **Stops the instance** |

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
| `WakeClientTests` | The optional SSH helper: fast path, wake and wait, fall back to the wake page, `--no-wake`, timeouts, wrong PIN or lock, command line, data relay |
| `LambdaTests` | Wake page vs instructions, text vs JSON, waiting and progress, PIN checks, the 5-tries lockout (lock, unlock, reset, expiry, fail closed) |
| `TemplateTests` | The stack file contains the current wake page code, and its form has exactly the 8 expected settings |

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

## 🟢 Level 5: Live acceptance cycle

> [!WARNING]
> This **really stops your instance**. Save your work first. If waking fails, use the [other ways to wake](RUNBOOK.md#-if-waking-doesnt-work).

| Step | Do this | Where | You should see |
|---|---|---|---|
| 1 | `bash install_guest.sh --warn 2 --stop 4`, **on the instance** (not your PC). The topic and wake link are kept from the first install. | Instance | `Installed. Warning at 2 idle minutes, stop at 4.` |
| 2 | Close **every** connection and editor window | Your devices | – |
| 3 | Wait about 3 minutes | – | One warning email arrives |
| 4 | Wait about 2 more minutes | EC2 console | **Stopped** |
| 5 | `curl -d pin=<PIN> -d action=wake <WAKE_URL>` | Any terminal | `ready: yes` and an `ssh:` line |
| 6 | Connect and look at your files | Instance | Everything is still there |
| 7 | `bash install_guest.sh` | Instance | Back to 30 and 45 minutes, with no extra test email |

**Emails to expect in a first test:** **three**: the setup test email (first install only), one warning, and one "stopped" email with your wake link. If you turned on the optional backup alarm, a fourth "ALARM" email can arrive about 20 minutes after the stack is created on an already-idle instance.

<details>
<summary>Cleaner way: short timings that disappear at the next boot (no step 7)</summary>

Instead of step 1, keep the normal 30/45 install and add a temporary override that lives only in memory. The next boot, which is the wake in step 5, goes back to 30/45 by itself.

**▶ Run in:** the instance

```bash
printf 'WARN_MINUTES=2\nSTOP_MINUTES=4\n' | sudo tee /run/ec2-idle-test.env >/dev/null
sudo mkdir -p /run/systemd/system/ec2-idle.service.d
printf '[Service]\nEnvironmentFile=/run/ec2-idle-test.env\n' | sudo tee /run/systemd/system/ec2-idle.service.d/test.conf >/dev/null
sudo systemctl daemon-reload
```

**You should see:** `systemctl show ec2-idle.service -p EnvironmentFiles` lists `/etc/ec2-idle.conf` first, then `/run/ec2-idle-test.env`. Then continue from step 2.

</details>

**Optional extra checks**
- [ ] **Schedule:** set the start time a few minutes ahead while the instance is stopped. It should start on time.
- [ ] **Long-job check:** start your real job, disconnect, and wait past the stop time. The instance should keep running, and the log should say `Busy (workload)`.
- [ ] **Phone:** wake from your phone. The page should show `stopped → pending → running → ready`.

---

## 🔁 After changing code

| You changed | Then run |
|---|---|
| `src/wake_ec2.py` | `python src/sync_template.py`, then levels 1 and 2, then update the stack |
| `src/ec2_idle.py` or the timer files | Level 1, then the installer again (level 4) |
| `src/ec2-idle-stack.yaml` | Level 2, then update the stack |
| `src/ec2_up.py` | Level 1, then connect with `ssh ec2-workspace` while the instance is stopped, and again while it's running |

## 📋 Results so far

| Check | Result |
|---|---|
| 51 unit tests on Windows, Python 3.13, with and without botocore | ✅ Passed |
| Request signing matches AWS's published example and botocore | ✅ Passed |
| Template check (`cfn-lint`) | ✅ Clean |
| `discover.sh` against simulated AWS answers (your setup and a worst case) | ✅ Passed |
| `ec2_up.py` end to end against a fake wake page and fake SSH server: wake, wait and connect; fast path with no wake page call; wrong PIN message | ✅ Passed |
| Services allowed on the AWS Free plan | ✅ Confirmed from AWS docs |
| Live run on a real account (Ubuntu 24.04, m7i-flex.large, Sydney), deployed by a fresh AI agent from this repository through an AWS MCP server | ✅ Passed |
| Level 4: instance check, test email delivered | ✅ Passed |
| Level 5: warning at 2 idle minutes, stop requested at about 4.3, **Stopped** about 35 s later | ✅ Passed |
| Level 5: wake through the API, `ready: yes` in 4 s, files intact | ✅ Passed |
| Fresh redeploy by a new AI agent from this repository (complete removal, new stack, new timer, real warn and stop cycle with the reboot-proof short timings, 4 emails sent and delivered, none failed, instance left stopped. The backup alarm was on by default then, which is why it was 4 and not 3) | ✅ Passed |
