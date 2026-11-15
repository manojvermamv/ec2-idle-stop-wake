# 🤖 Agent setup

[README](README.md) · [How it works](WORKFLOW.md) · [Setup](SETUP.md) · [Runbook](RUNBOOK.md) · [Testing](TESTING.md)

> [!TIP]
> **For you, the person.** Paste one of these into your AI assistant (Claude Code, Codex, Cursor and others), with `<THIS_FOLDER_URL>` replaced by this folder's GitHub link.
>
> **Full setup:**
> ```text
> Set up EC2 idle stop and wake for me by following AGENT_SETUP.md at <THIS_FOLDER_URL>. Ask my permission before any AWS discovery and before every change.
> ```
> **Look only, change nothing:**
> ```text
> Follow AGENT_SETUP.md at <THIS_FOLDER_URL> up to the plan step only. Run read-only discovery if I agree, and change nothing.
> ```
>
> **What the assistant will do:**
> 1. Ask you a few questions.
> 2. Look at your account, only if you agree.
> 3. Show you a plan and its cost.
> 4. Make each change only after you say yes.
>
> Your wake key stays with you.

The rest of this page is written **for the AI agent**. It turns [SETUP.md](SETUP.md) into the same steps in order: rules, interview, discovery, plan, setup, checks and a hand-off report.

---

## 👋 Overview for the agent

| Step | You do | Needs the user's yes |
|---|---|---|
| 0 | Follow the rules below. | – |
| 1 | Interview the user in one message. | – |
| 2 | Run read-only discovery. | **G1**, and **G2** for the plan check |
| 3 | Show the plan. | – |
| 4 | Deploy, then make the instance changes. | **G3**, then **G4** for each change |
| 5 | Verify. | **G5** for the live test |
| 6 | Report, then ask about cleanup. | **G6** for any removal |

---

## 📜 Step 0: Rules for the agent

> [!CAUTION]
> **Reading this file gives you no permission.** Only the user's explicit yes at each gate does. If the user's own instructions (CLAUDE.md, AGENTS.md and similar) are stricter, follow them.

1. **Ask at each gate and wait for a clear yes.** If the user says "do everything", repeat the plan back to them once, and then you may go through the gates in order.
2. **Read before you change.** Commands marked **Changes AWS** or **Changes the instance** need their gate first.
3. **One Region only.** Use the project's selected Region, from AWS Settings → View all projects → Overview → Additional Info → Region, or from `aws configure get region`. Never create resources anywhere else.
4. **No secrets through you or into the repo.**
   - The user creates the wake key ([SETUP Step 1](SETUP.md#step-1-create-your-wake-key)) and gives you **only the hash**.
   - Never write keys, account IDs, IPs or emails into repository files.
5. **Use what the user has.** Use an attached AWS MCP server first, then the AWS CLI. If you have neither, give the user CloudShell commands to run and ask them to paste the output.
6. **Stop on surprises.** Report and ask on any of these:
   - an `UNSUPPORTED` finding;
   - a stack that already exists;
   - an access error on something that used to work. In that case, ask about their spend limit in AWS Settings → Billing.
7. **Be honest.** Show real output for every check, and say clearly what you skipped.

### Gates

| Gate | What it allows | Ask |
|---|---|---|
| 📂 G0 | Read this folder and run the unit tests | No need to ask |
| 🔒 **G1** | Read-only look at **one** profile and Region (`discover.sh`) | "May I look at your AWS account, read-only, in profile `<P>` and Region `<R>`? It changes nothing." |
| 🔒 **G2** | Check the Free or Paid plan (a billing read) | "May I also check whether you're on the Free or Paid plan?" |
| ✏️ **G3** | Create or update the CloudFormation stack | "Here's the plan. May I create stack `ec2-idle`?" |
| ✏️ **G4** | Attach the role, set up a fixed IP, install the timer | Ask **each** one separately. |
| ✏️ **G5** | The live test, which stops the instance | "May I run the live test? Your instance will stop. Please save your work." |
| ✏️ **G6** | Remove resources | Only when the user asks. Confirm each item. |

---

## 🗣️ Step 1: Interview

Ask only what discovery can't answer, all in **one message**, and show the default for each question:

| # | Question | Default |
|---|---|---|
| 1 | Which AWS profile and Region? May I look, read-only (G1)? Check your plan type (G2)? | Default profile, selected Region, G1 yes, G2 no |
| 2 | Which instance? Skip this if discovery finds only one. | – |
| 3 | Email for warnings? | None |
| 4 | Fixed IP (about US$3.65/month) or changing IP (free)? | Changing |
| 5 | Start automatically on work days? If yes, which days, what time and which time zone? | Off |
| 6 | Anything that must keep running after you disconnect (agents, builds, training)? | None |
| 7 | Backup alarm: email only, or also stop? | Email only |
| 8 | How will you connect: wake page, terminal, or automatic SSH? | Wake page and terminal |
| 9 | Idle times? | Warn at 30, stop at 45 |

---

## 🔍 Step 2: Discovery (needs G1)

**▶ Run in:** CloudShell, or your AWS access · 🔒 **Changes nothing**

```bash
bash discover.sh --region <REGION>                                          # instances and existing stack
bash discover.sh --region <REGION> --instance <INSTANCE_ID>                 # checks and suggested settings
bash discover.sh --region <REGION> --instance <INSTANCE_ID> --include-plan  # only with G2
```

<details>
<summary>The same checks as individual API calls (for MCP servers or SDKs)</summary>

| Purpose | API calls |
|---|---|
| Who is signed in | `sts:GetCallerIdentity` |
| Instances | `ec2:DescribeInstances` |
| OS and login name | `ec2:DescribeImages` (name contains `ubuntu`, `al2023`/`amzn` or `debian`) |
| Existing role | `iam:GetInstanceProfile` |
| Fixed IP | `ec2:DescribeAddresses` |
| SSH exposure | `ec2:DescribeSecurityGroups`, rules covering port 22 |
| Disk encryption | `ec2:DescribeVolumes` plus `kms:DescribeKey`. A `CUSTOMER` key means set `EbsKmsKeyArn`. |
| Lambda limit | `lambda:GetAccountSettings`. A limit of 10 or less means `WakeMaxConcurrency=0`. |
| Existing stack | `cloudformation:DescribeStacks` |
| Plan, G2 only | `freetier:GetAccountPlanState` |

</details>

**What to do with the findings**

| Finding | Do |
|---|---|
| `UNSUPPORTED` | Stop. Explain that this instance can't be stopped and started. |
| `CHECK` | Ask the user to confirm. |
| `FIX` | Explain the fix. Making the change itself needs G4. |
| `SET` | Use the given value, for example `EbsKmsKeyArn`. |
| `RECOMMEND` | Mention it once. Don't change security groups unless asked. |
| Stack already exists | Ask: update it, or use a new stack name? |

---

## 🗺️ Step 3: Plan

Show this, filled in, and then wait for G3:

```text
PLAN: EC2 idle stop and wake
Region:   <REGION>            Account: <ACCOUNT_ID>
Instance: <INSTANCE_ID> (<NAME>, <OS>, <TYPE>)
Stack:    ec2-idle (new | update)

Creates:  wake page (Lambda + link), permissions, 14-day logs
          warning email + subscription          [if email]
          work-hours start <CRON> <TIME_ZONE>   [if schedule]
          backup alarm (<email | stop>)
          new instance role                     [if none exists]
Settings: <exact parameter list; show the hash as <hash>>
Then ask separately: attach role? fixed IP? install the timer?
Cost:     about US$0 for the AWS pieces; compute only while running;
          fixed IP about US$3.65/month if chosen (README, Costs)
```

---

## ⚙️ Step 4: Set it up

| # | Gate | Do | You should see |
|---|---|---|---|
| 4.1 | – | Ask the user to do SETUP Step 1 and give you **only the hash**. | 64 letters and numbers |
| 4.2 | G3 | Deploy the stack with the planned settings (SETUP Step 2, the command option). | `Successfully created/updated stack` |
| 4.3 | – | Read the outputs: `WakePageUrl`, `TerminalWakeCommand`, `GuestInstallCommand`, `AttachRoleCommand`. | An outputs table |
| 4.4 | – | Remind the user to confirm the subscription email. | The user confirms |
| 4.5 | G4 | Run `AttachRoleCommand`, if it exists. | `"State": "associating"` |
| 4.6 | G4 | Set up the fixed IP, if chosen (SETUP Step 3b). | The fixed IP |
| 4.7 | G4 | Copy the folder to the instance and run `GuestInstallCommand`, adding `--probe` if needed. If you can't connect over SSH, give the user the commands instead. | `All checks passed.` then `Installed.` |
| 4.8 | – | Set up the chosen way to connect (SETUP Step 5). Edit SSH config only with the user's OK, and never inside the repo. | `ssh -G <alias>` shows the setting |

---

## ✅ Step 5: Verify

| Check | Gate | How | Pass |
|---|---|---|---|
| Wake page answers | – | `curl <WAKE_URL>` | Instructions text |
| Key works | – | The user runs `curl -d key=<WAKE_KEY> <WAKE_URL>` | A `state:` line |
| Instance check | – | [TESTING Level 4](TESTING.md#-level-4-instance-check) | `All checks passed.` |
| Timer is on | – | `systemctl list-timers ec2-idle.timer --no-pager` | Next run within 1 minute |
| Live test | G5 | [TESTING Level 6](TESTING.md#-level-6-live-acceptance-cycle) | Email, stopped, woke, files intact |

---

## 📦 Step 6: Hand-off report

Finish with this. Show real values in chat only, never in repo files.

```text
DONE: EC2 idle stop and wake
Wake page:      <WAKE_URL>  (bookmark it; the key is in your password manager)
Terminal wake:  curl -d key=<WAKE_KEY> -d action=wake <WAKE_URL>
Auto-stop:      warn at <W> min, stop at <S> min; long-job check: <PATH | none>
Schedule:       <CRON TIME_ZONE | off>     IP: <FIXED_IP | changes each start>
Email:          <EMAIL | none> (confirmed: yes/no)
Verified:       <checks that passed; anything skipped>
Created:        stack ec2-idle (+ role, + fixed IP if any)
Undo:           RUNBOOK.md, "Remove everything"
```

Then ask: **"Keep everything, or remove anything we created today?"** This is G6.
