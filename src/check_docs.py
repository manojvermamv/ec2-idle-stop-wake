#!/usr/bin/env python3
"""Check that the docs still match the code. Run it from anywhere: python src/check_docs.py

It runs inside the unit tests and in CI, so documentation cannot drift quietly. It checks that:
  - every --option the docs mention exists in one of our scripts (or belongs to aws, curl, ssh, ...)
  - the settings table in SETUP.md lists exactly the stack's parameters
  - every stack output is documented, and every documented output exists
  - removed settings, options and old wording no longer appear
  - every link and anchor in the docs resolves, and code fences are balanced
Works on Python 3.6 and newer.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ("install_guest.sh", "ec2_up.py", "ec2_idle.py", "discover.sh", "temp_ssh.sh")
# Options that belong to other tools (aws, curl, ssh, systemctl ...), not to our scripts.
FOREIGN_FLAGS = {
    "--parameter-overrides", "--capabilities", "--stack-name", "--template-file", "--query", "--output",
    "--instance-ids", "--instance-id", "--allocation-id", "--allocation-ids", "--public-ip",
    "--association-id", "--filters", "--no-cli-pager", "--iam-instance-profile", "--since",
    "--no-pager",
}
REMOVED_FLAGS = ("--proxy", "--port %p", "--key-file", "--dry-run", "--include-plan")
REMOVED_WORDS = (
    "wake key", "Wake key", "WakeKeySha256", "WakeDryRun", "WakeMaxConcurrency", "ScheduleTimezone",
    "BackupAlarmAction", "BackupAlarmCpuPercent", "verify_guest", "IDLE_MODE", "Practice wake",
    "Mode stop", "key is in your password manager",
)


def read(path):
    return path.read_text(encoding="utf-8")


def slug(heading):
    heading = re.sub(r"[^\w\- ]", "", heading.strip().lower(), flags=re.UNICODE)
    return heading.replace(" ", "-")


def problems():
    found = []
    docs = {p.name: read(p) for p in sorted(ROOT.glob("*.md"))}
    code = {name: read(ROOT / "src" / name) for name in SCRIPTS}
    template = read(ROOT / "src" / "ec2-idle-stack.yaml")

    # 1. options
    ours = set()
    for text in code.values():
        ours |= set(re.findall(r"--[a-z][a-z-]+", text))
    for name, text in docs.items():
        for flag in sorted(set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", text))):
            if flag not in ours and flag not in FOREIGN_FLAGS:
                found.append("%s: the option %s is not in any of our scripts" % (name, flag))
        for gone in REMOVED_FLAGS:
            if gone in text:
                found.append("%s: still mentions the removed option %r" % (name, gone))

    # 2. settings table = stack parameters
    section = template.split("\nParameters:\n", 1)[1].split("\nRules:\n", 1)[0]
    parameters = set(re.findall(r"^  ([A-Za-z]+):\s*$", section, re.M))
    table = docs["SETUP.md"].split("What every setting means", 1)[1].split("</details>", 1)[0]
    documented = set(re.findall(r"^\| `([A-Za-z]+)`", table, re.M))
    if parameters != documented:
        found.append("SETUP.md settings table differs from the stack. Only in the stack: %s. Only in the docs: %s."
                     % (sorted(parameters - documented), sorted(documented - parameters)))

    # 3. outputs
    outputs = set(re.findall(r"^  ([A-Za-z]+):\s*$", template.split("\nOutputs:\n", 1)[1], re.M))
    names = "WakePageUrl|OneTapBookmark|TerminalWakeCommand|AlertTopicArn|ConnectInBrowser|TemporarySshCommand|GuestInstallCommand|AttachRoleCommand"
    mentioned = set()
    for text in docs.values():
        for bold, code_span in re.findall(r"\*\*(%s)\*\*|`(%s)`" % (names, names), text):
            mentioned.add(bold or code_span)
    if not mentioned <= outputs:
        found.append("outputs named in the docs but missing from the stack: %s" % sorted(mentioned - outputs))
    if not outputs <= mentioned:
        found.append("stack outputs the docs never mention: %s" % sorted(outputs - mentioned))

    # 4. removed wording
    for name, text in docs.items():
        for word in REMOVED_WORDS:
            for number, line in enumerate(text.splitlines(), 1):
                if word in line:
                    found.append("%s:%d: old wording %r: %s" % (name, number, word, line.strip()[:80]))

    # 5. links, anchors, fences
    anchors = {}
    for name, text in docs.items():
        bare = re.sub(r"```.*?```", "", text, flags=re.S)
        anchors[name] = {slug(m.group(1)) for m in re.finditer(r"^#{1,6} (.+)$", bare, re.M)}
    for name, text in docs.items():
        if text.count("```") % 2:
            found.append("%s: a code fence is not closed" % name)
        for target, fragment in re.findall(r"\]\(([^)#\s]*)#([^)\s]+)\)", text):
            page = target or name
            if page.endswith(".md") and fragment not in anchors.get(page, set()):
                found.append("%s: the link %s#%s goes nowhere" % (name, page, fragment))
        for target in re.findall(r"\]\(([^)#\s:]+)\)", text):
            if not (ROOT / target).exists():
                found.append("%s: the link %s points at a missing file" % (name, target))
    return found


def main():
    found = problems()
    for item in found:
        print(" - " + item.encode("ascii", "replace").decode("ascii"))
    print("Docs match the code." if not found else "%d problem(s) found." % len(found))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
