#!/usr/bin/env bash
# Runs INSIDE a distro container (see ci.yml). The installer finds or installs the prerequisites the way
# it would on a real instance, then the tests run on that distro's own Python.
set -euo pipefail
log=/tmp/ci-run.log
exec > >(tee "$log") 2>&1
# On failure, put the end of the output on the run page as an annotation, so the cause can be read without logging in.
report_failure() {
    local status=$?
    if [ "$status" -ne 0 ]; then
        sleep 2   # let the tee behind the log file finish writing
        # the failing test names first, then the end of the log
        problems=$(grep -E '^(FAIL|ERROR):|Error|Traceback' "$log" | head -n 12 || true)
        echo "::error title=${CI_IMAGE:-image} ($(uname -m)) failed::$(printf '%s\n' "$problems" "-----" "$(tail -n 25 "$log")" | sed -e 's/%/%25/g' -e ':a;N;$!ba;s/\n/%0A/g')"
    fi
}
trap report_failure EXIT
cd /repo/src
output=$(bash install_guest.sh --deps-only)
echo "$output"
# Shown on the run page, so the real versions are on record: "Detected: <distro> | package manager | python | systemd".
echo "::notice title=${CI_IMAGE:-image} ($(uname -m))::$(echo "$output" | tail -n 1)"
python3 -c 'import sys; print("python3 is", sys.version.split()[0]); assert sys.version_info >= (3, 6)'
python3 -I -S -c 'import sys; sys.path.insert(0, "."); import ec2_idle; print("ec2_idle imports the way the service runs it (-I -S)")'
# The installer only lets root run files that nobody else can change: the distro's own Python passes,
# and a world-writable file is refused.
eval "$(sed -n '/^root_only() {/,/^}/p;/^trusted() {/,/^}/p' /repo/src/install_guest.sh)"
trusted "$(command -v python3)" || { echo "trusted() refused the distro's own python3"; exit 1; }
scratch=$(mktemp -d)
touch "$scratch/tool"
chmod 777 "$scratch/tool"
if trusted "$scratch/tool"; then echo "trusted() accepted a world-writable file"; exit 1; fi
echo "trusted() accepts the distro Python and refuses a world-writable file"
cd /repo
PYTHONIOENCODING=utf-8 python3 -B -m unittest discover -s src
