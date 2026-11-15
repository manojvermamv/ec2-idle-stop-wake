#!/usr/bin/env python3
"""Copy wake_ec2.py into the ZipFile block of ec2-idle-stack.yaml (run after editing it).

The template embeds the Lambda code so it can be deployed from a single file.
test_workflow.py fails if the two copies differ.
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent
INDENT = " " * 10


def main():
    template = HERE / "ec2-idle-stack.yaml"
    lines = template.read_text(encoding="utf-8").split("\n")
    start = next(i for i, line in enumerate(lines) if line.strip() == "ZipFile: |") + 1
    end = start
    while end < len(lines) and (not lines[end] or lines[end].startswith(INDENT)):
        end += 1
    while end > start and not lines[end - 1]:
        end -= 1
    code = (HERE / "wake_ec2.py").read_text(encoding="utf-8").rstrip("\n")
    if "\t" in code:
        raise SystemExit("wake_ec2.py must not contain tab characters")
    block = [INDENT + line if line else "" for line in code.split("\n")]
    with open(template, "w", encoding="utf-8", newline="\n") as handle:   # write_text(newline=) needs Python 3.10
        handle.write("\n".join(lines[:start] + block + lines[end:]))
    print(f"Updated {template.name} with {len(block)} lines of wake_ec2.py")


if __name__ == "__main__":
    main()
