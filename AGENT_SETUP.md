# 🤖 Agent setup

[README](README.md) · [How it works](WORKFLOW.md) · [Setup](SETUP.md) · [Runbook](RUNBOOK.md) · [Testing](TESTING.md)

> [!TIP]
> **For you, the person.** Paste one of these into your AI assistant (Claude Code, Codex, Cursor and others).
>
> **Full setup:**
> ```text
> Set up EC2 idle stop and wake for me by following AGENT_SETUP.md at https://github.com/manojvermamv/ec2-idle-stop-wake. Ask my permission before any AWS discovery and before every change.
> ```
> **Look only, change nothing:**
> ```text
> Follow AGENT_SETUP.md at https://github.com/manojvermamv/ec2-idle-stop-wake up to the plan step only. Run read-only discovery if I agree, and change nothing.
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
   - **New accounts:** CloudShell may refuse to start while AWS verifies the account ("account verification is in progress", up to two days). Then use the MCP server, or have the user create the wake key in PowerShell and deploy through the console upload.
6. **Stop on surprises.** Report and ask on any of these:
   - an `UNSUPPORTED` finding;
   - a stack that already exists;
   - an access error on something that used to work. In that case, ask about their spend limit in AWS Settings → Billing.
7. **Be honest.** Show real output for every check, and say clearly what you skipped.

### Gates

| Gate | What it allows | Ask |
|---|---|---|
| 📂 G0 | Read this repository and run the unit tests | No need to ask |
| 🔒 **G1** | Read-only look at **one** profile and Region (`discover.sh`) | "May I look at your AWS account, read-only, in profile `<P>` and Region `<R>`? It changes nothing." |
| 🔒 **G2** | Check the Free or Paid plan (a billing read) | "May I also check whether you're on the Free or Paid plan?" |
| ✏️ **G3** | Create or update the CloudFormation stack | "Here's the plan. May I create stack `ec2-idle`?" |
| ✏️ **G4** | Attach the role, set up a fixed IP, open temporary SSH access, install the timer | Ask **each** one separately. Any temporary SSH rule must be removed again when you're done. |
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
| 4 | Fixed IP (about US$3.65/month) or changing IP (free)? **Skip this if discovery shows an Elastic IP is already attached.** Keep it, and never allocate a second one. | Changing, or keep the existing one |
| 5 | Start automatically on work days? If yes, which days, what time and which time zone? | Off |
| 6 | Anything that must keep running after you disconnect (agents, builds, training)? | None |
| 7 | Backup alarm: email only, or also stop? | Email only |
| 8 | How will you connect: wake page, terminal, or automatic SSH? **How do you get a shell on the instance today?** (SSH, EC2 Instance Connect, an endpoint, Session Manager.) The installer must run there. | Wake page and terminal |
| 9 | Idle times? | Warn at 30, stop at 45 |

---

## 🔍 Step 2: Discovery (needs G1)

**▶ Run in:** CloudShell, or your AWS access · 🔒 **Changes nothing**

```bash
git clone https://github.com/manojvermamv/ec2-idle-stop-wake.git && cd ec2-idle-stop-wake/src
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
| SSH exposure | `ec2:DescribeSecurityGroupRules` for the instance's groups. For port 22 (or protocol `-1`), look at **all** sources: CIDRs, prefix lists and referenced groups. Only CIDRs and prefix lists can reach the instance from outside. |
| Instance Connect route | `ec2:DescribeManagedPrefixLists` (name `com.amazonaws.<REGION>.ec2-instance-connect`) and `ec2:DescribeInstanceConnectEndpoints` |
| Windows | `Platform` = `windows` in `ec2:DescribeInstances` means `UNSUPPORTED` |
| Disk encryption | `ec2:DescribeVolumes` plus `kms:DescribeKey`. A `CUSTOMER` key means set `EbsKmsKeyArn`. |
| Lambda limit | `lambda:GetAccountSettings`. A limit of 100 or less means `WakeMaxConcurrency=0`. Above 100, `3` is a good optional cap. |
| Existing stack | `cloudformation:DescribeStacks` |
| Plan, G2 only | `freetier:GetAccountPlanState` |

</details>

**What to do with the findings**

| Finding | Do |
|---|---|
| `UNSUPPORTED` | Stop. Explain that this instance can't be stopped and started. |
| `CHECK` | Ask the user to confirm. For "no SSH route from outside", ask how they reach the instance. Offer a temporary port 22 rule for their IP (/32) as a separate G4 action, and remove it afterwards. |
| `KEEP` | Keep it as is, for example an Elastic IP that's already attached. Skip SETUP Step 3b. |
| `NOTE` | Show it in the plan, for example "your existing role `<ROLE>` will get the stack's policy added". |
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
          warning + stopped emails, subscription [if email]
          work-hours start <CRON> <TIME_ZONE>   [if schedule]
          backup alarm (<email | stop>)
          new instance role                     [if none exists]
Modifies: existing role <ROLE> gets the stack policy added  [if a role exists]
Settings: <exact parameter list; show the hash as <hash>>
Then ask separately: attach role? fixed IP (or: keep existing EIP <IP>)?
          temporary SSH access (if no route)? install the timer?
Cost:     about US$0 for the AWS pieces; compute only while running;
          new fixed IP about US$3.65/month if chosen (README, Costs)
Expect:   an "ALARM" email within about 20 minutes of deploy if the instance is already idle.
          This is normal until the timer is installed. A first test cycle then gives
          up to 4 emails: alarm, setup test, one warning, one "stopped".
```

---

## ⚙️ Step 4: Set it up

| # | Gate | Do | You should see |
|---|---|---|---|
| 4.1 | – | Ask the user to do SETUP Step 1 and give you **only the hash**. | 64 letters and numbers |
| 4.2 | G3 | Deploy the stack with the planned settings: with the CLI, use SETUP Step 2's command option; with an MCP server or SDK, see the recipe below. | `CREATE_COMPLETE` |
| 4.3 | – | Read the outputs: `WakePageUrl`, `TerminalWakeCommand`, `AlertTopicArn`, `GuestInstallCommand`, `AttachRoleCommand`. | An outputs table |
| 4.4 | – | Ask the user to confirm the subscription email **now, before 4.7**, and wait for their "done". Mention that an "ALARM" email may also arrive (see the plan). | The user confirms |
| 4.5 | G4 | Attach the role, if `AttachRoleCommand` exists: `ec2:AssociateIamInstanceProfile`. Then **wait about 60 seconds** for it to take effect. | `"State": "associating"` |
| 4.6 | G4 | Set up a new fixed IP, if chosen (SETUP Step 3b). Skip if one is already attached. | The fixed IP |
| 4.7 | G4 | **On the instance**, never on the user's computer: `git clone https://github.com/manojvermamv/ec2-idle-stop-wake.git`, then `cd ec2-idle-stop-wake/src`, then `GuestInstallCommand` (it already includes `--topic` and `--wake-url`), adding `--probe` and `--name` if wanted. If you have no shell on the instance, give the user these exact commands for their terminal (SSH, Instance Connect or Session Manager). | `All checks passed.` then `Installed.` |
| 4.7b | – | Confirm on the instance: `systemctl list-timers ec2-idle.timer --no-pager`. "0 timers listed" means the install didn't happen there. If the user reports success, still ask for this output. | One timer listed |
| 4.7c | G4 | If you opened temporary SSH access, remove that rule now (`ec2:RevokeSecurityGroupIngress` by rule ID). | The rule is gone |
| 4.8 | – | Set up the chosen way to connect (SETUP Step 5). Edit SSH config only with the user's OK, and never inside the repo. | `ssh -G <alias>` shows the setting |

<details>
<summary><b>Deploy recipe for MCP servers and SDKs</b> (no <code>aws cloudformation deploy</code>)</summary>

1. **Template.** Use the exact committed bytes of `src/ec2-idle-stack.yaml`, with LF line endings. The repository's `.gitattributes` keeps it LF even on Windows. It's about 31 KB, under the 51,200-byte limit for `TemplateBody`, so no S3 bucket is needed. If your tool can't read local files, embed the text in the call **as a raw string** (in Python, `r'''…'''`), because the template contains backslashes that a normal string would change. Check the length (about 31,000 bytes, no `\r` characters) before sending.
2. **Create.** `cloudformation:CreateStack` with:
   - `StackName=ec2-idle`
   - `TemplateBody=<template>`
   - `Capabilities=["CAPABILITY_IAM"]`. The IAM resources are unnamed and there's no transform, so this is enough.
   - `OnFailure=ROLLBACK`
   - `Parameters=[{ParameterKey, ParameterValue}, …]`. Pass empty optional values as `""`.
   - For an existing stack, use `UpdateStack` with the same arguments.
3. **Wait.** Poll `cloudformation:DescribeStacks` every 15 to 30 seconds until `CREATE_COMPLETE`, which takes about 2 to 3 minutes. Some MCP servers return before a long call finishes. If a call seems to hang, poll its task (for example `get_tasks`) or re-query the stack instead of sending it again. On `ROLLBACK_*`, read `DescribeStackEvents` for `ResourceStatusReason`, report it, and don't retry by yourself.
4. **Outputs.** Read them from `DescribeStacks` → `Stacks[0].Outputs`.
5. **Attach the role.** `ec2:AssociateIamInstanceProfile` with `InstanceId` and `IamInstanceProfile={"Name": "<profile from AttachRoleCommand>"}`. Check first with `DescribeIamInstanceProfileAssociations` that nothing is attached already.
6. **Check the wake page.** `lambda:GetFunctionUrlConfig` should show `AuthType NONE`. An HTTPS `GET` of `WakePageUrl` without a browser `accept` header returns the instructions text.

</details>

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
