#!/usr/bin/env bash
# Optional test. Run with bash verify_guest.sh from an SSH session on the instance.
# It never stops the instance: it uses dry-run mode and a temporary systemd timer.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

scratch=$(mktemp -d)
unit="ec2-idle-test-${scratch##*.}"
cleanup() {
    sudo -n systemctl stop "$unit.timer" "$unit.service" 2>/dev/null || true
    sudo -n systemctl reset-failed "$unit.service" 2>/dev/null || true
    sudo -n rm -f -- "/run/$unit/state.json" "/run/$unit/state.tmp" || true
    sudo -n rmdir -- "/run/$unit" 2>/dev/null || true
    rm -f -- "$scratch/active/state.json" "$scratch/idle/state.json" \
        "$scratch/probe" "$scratch/job.active"
    rmdir -- "$scratch/active" "$scratch/idle" "$scratch" 2>/dev/null || true
}
trap cleanup EXIT
chmod 755 "$scratch"
mkdir -- "$scratch/active" "$scratch/idle"

printf 'Host %s\n' "$(hostname)"
systemd --version | head -n 1
systemd-analyze verify ec2-idle.service ec2-idle.timer
python3 -B -m unittest discover -s . -v

observe() {
    env IDLE_MODE=dry-run RUNTIME_DIRECTORY="$1" WORK_PROBE="${2:-}" \
        EC2_IDLE_IGNORE_SESSIONS="${3:-0}" WARN_MINUTES=1 STOP_MINUTES=2 python3 ec2_idle.py
}

if [[ -n "${SSH_CONNECTION:-}" ]]; then
    output=$(observe "$scratch/active")
    printf '%s\n' "$output"
    grep -Fq 'Busy (remote login session).' <<< "$output"
fi

printf '#!/bin/sh\n[ -e "%s/job.active" ]\n' "$scratch" > "$scratch/probe"
chmod 755 "$scratch/probe"
touch "$scratch/job.active"
output=$(observe "$scratch/idle" "$scratch/probe" 1)
printf '%s\n' "$output"
grep -Fq 'Busy (workload).' <<< "$output"
rm -- "$scratch/job.active"
output=$(observe "$scratch/idle" "$scratch/probe" 1)
printf '%s\n' "$output"
grep -Fq 'Idle for 0.0 minutes (dry-run).' <<< "$output"

# EC2_IDLE_IGNORE_SESSIONS=1 lets the dry run finish while this SSH session stays open.
sudo -n systemd-run --unit="$unit" --on-active=1s --on-unit-inactive=30s \
    --timer-property=AccuracySec=1s --property=Type=oneshot \
    --property="RuntimeDirectory=$unit" --property=RuntimeDirectoryPreserve=yes \
    --setenv=IDLE_MODE=dry-run --setenv=WARN_MINUTES=1 --setenv=STOP_MINUTES=2 \
    --setenv=EC2_IDLE_IGNORE_SESSIONS=1 --setenv="WORK_PROBE=$scratch/probe" \
    /usr/bin/python3 -I -S "$PWD/ec2_idle.py"
sudo -n systemctl list-timers "$unit.timer" --no-pager

for attempt in {1..17}; do
    log=$(sudo -n journalctl -u "$unit.service" --no-pager -o cat)
    if grep -Fq 'Would call EC2 StopInstances' <<< "$log"; then
        printf '%s\n' "$log"
        grep -Fq 'Would publish warning.' <<< "$log"
        echo "Dry-run cycle passed."
        exit 0
    fi
    sleep 10
done
printf '%s\n' "$log"
printf 'The timer did not complete the dry-run window.\n' >&2
exit 1
