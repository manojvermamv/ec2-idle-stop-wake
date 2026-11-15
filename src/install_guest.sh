#!/usr/bin/env bash
# One-time install of the idle watcher. Run it ON THE EC2 INSTANCE, from the src folder.
#   bash install_guest.sh [--topic SNS_TOPIC_ARN] [--wake-url URL] [--name NAME]
#                         [--warn 30] [--stop 45] [--probe /abs/path|none] [--test-email]
#   bash install_guest.sh --uninstall [--purge]   (--purge also deletes the settings and saved state)
# --topic, --wake-url  AlertTopicArn and WakePageUrl from the stack outputs (GuestInstallCommand).
# --name     Friendly name for emails, for example ubuntu-cp.
# --probe    Executable that exits 0 while background work runs and 1 when idle ("none" clears it).
# Topic, wake URL, name and probe are kept from the previous install when left out.
# A test email is sent only on the first install, when the topic changes, or with --test-email.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

topic="" wake_url="" name="" probe="" warn=30 stop=45 uninstall=0 purge=0 force_test=0
set_topic=0 set_wake=0 set_name=0 set_probe=0
usage() { sed -n '2,10p' "$0"; exit "${1:-0}"; }
while (($#)); do
    case $1 in
        --topic) topic=${2:?--topic needs a value}; set_topic=1; shift 2 ;;
        --wake-url) wake_url=${2:?--wake-url needs a value}; set_wake=1; shift 2 ;;
        --name) name=${2:?--name needs a value}; set_name=1; shift 2 ;;
        --probe) probe=${2:?--probe needs a value}; set_probe=1; shift 2 ;;
        --warn) warn=${2:?--warn needs a value}; shift 2 ;;
        --stop) stop=${2:?--stop needs a value}; shift 2 ;;
        --test-email) force_test=1; shift ;;
        --uninstall) uninstall=1; shift ;;
        --purge) purge=1; shift ;;
        -h | --help) usage ;;
        *) echo "Unknown option: $1" >&2; usage 2 ;;
    esac
done

fail() { echo "install_guest: $*" >&2; exit 1; }
[[ $(uname -s) == Linux ]] || fail "run this ON THE EC2 INSTANCE (SSH or EC2 Instance Connect), not on your computer."

if ((uninstall)); then
    sudo systemctl disable --now ec2-idle.timer 2>/dev/null || true
    sudo systemctl stop ec2-idle.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/ec2-idle.service /etc/systemd/system/ec2-idle.timer \
        /usr/local/sbin/ec2_idle.py
    sudo rm -rf /run/systemd/system/ec2-idle.service.d /run/ec2-idle-test.env
    sudo systemctl daemon-reload
    if ((purge)); then
        sudo rm -rf /etc/ec2-idle.conf /etc/ec2-idle.conf.bak /run/ec2-idle
        echo "Idle watcher removed completely, including its settings and saved state."
        echo "Optional: delete this downloaded folder with: rm -rf ~/ec2-idle-stop-wake"
    else
        echo "Idle watcher removed. Settings kept in /etc/ec2-idle.conf, so a later install reuses them."
        echo "For a completely clean removal, run again with: --uninstall --purge"
    fi
    exit 0
fi

for tool in systemctl loginctl python3 pgrep; do
    command -v "$tool" >/dev/null || fail "$tool is required"
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || fail "Python 3.8 or newer is required"

# Keep earlier values for anything not given this time.
previous() { { sudo sed -n "s/^$1=//p" /etc/ec2-idle.conf 2>/dev/null || true; } | tail -n 1; }
old_topic=$(previous SNS_TOPIC_ARN)
((set_topic)) || topic=$old_topic
((set_wake)) || wake_url=$(previous WAKE_PAGE_URL)
((set_name)) || name=$(previous INSTANCE_NAME)
((set_probe)) || probe=$(previous WORK_PROBE)
[[ $probe == none ]] && probe=""

[[ $warn =~ ^[0-9]+$ && $stop =~ ^[0-9]+$ ]] && ((0 < warn && warn < stop)) \
    || fail "--warn and --stop are minutes, and --warn must be less than --stop"
if [[ -n $probe ]]; then
    [[ $probe == /* && -x $probe ]] || fail "--probe must be an absolute path to an executable"
fi
if [[ -n $topic && ! $topic =~ ^arn:[a-z-]+:sns:[a-z0-9-]+:[0-9]{12}:[A-Za-z0-9_-]+$ ]]; then
    fail "--topic must be an SNS topic ARN"
fi
if [[ -n $wake_url && ! $wake_url =~ ^https://[A-Za-z0-9./-]+$ ]]; then
    fail "--wake-url must be the https:// WakePageUrl from the stack outputs"
fi
if [[ -n $name && ! $name =~ ^[A-Za-z0-9._\ -]{1,64}$ ]]; then
    fail "--name may use letters, numbers, spaces, dots, dashes and underscores (up to 64)"
fi

conf=$(mktemp)
trap 'rm -f -- "$conf"' EXIT
sed -e "s|^WARN_MINUTES=.*|WARN_MINUTES=$warn|" \
    -e "s|^STOP_MINUTES=.*|STOP_MINUTES=$stop|" -e "s|^SNS_TOPIC_ARN=.*|SNS_TOPIC_ARN=$topic|" \
    -e "s|^WAKE_PAGE_URL=.*|WAKE_PAGE_URL=$wake_url|" -e "s|^INSTANCE_NAME=.*|INSTANCE_NAME=$name|" \
    -e "s|^WORK_PROBE=.*|WORK_PROBE=$probe|" ec2-idle.conf > "$conf"

if sudo test -e /etc/ec2-idle.conf; then
    sudo cp -p /etc/ec2-idle.conf /etc/ec2-idle.conf.bak
    echo "Saved the previous settings to /etc/ec2-idle.conf.bak"
fi
sudo install -m 0640 -o root -g root "$conf" /etc/ec2-idle.conf
sudo install -m 0755 -o root -g root ec2_idle.py /usr/local/sbin/ec2_idle.py
sudo install -m 0644 -o root -g root ec2-idle.service ec2-idle.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/ec2-idle.service /etc/systemd/system/ec2-idle.timer
sudo systemctl daemon-reload

check_args=(--check)
[[ -n ${SSH_CONNECTION:-} ]] && check_args+=(--expect-session)
if [[ -n $topic ]] && { ((force_test)) || [[ $topic != "$old_topic" ]]; }; then
    check_args+=(--test-email)
fi
echo "Checking this host..."
if ! sudo bash -c 'set -a; . /etc/ec2-idle.conf; set +a; exec python3 -I -S /usr/local/sbin/ec2_idle.py "$@"' \
    _ "${check_args[@]}"; then
    echo "The timer was NOT enabled. Fix the problems above and run this script again." >&2
    exit 1
fi

sudo systemctl enable --now ec2-idle.timer
sudo systemctl restart ec2-idle.timer
echo
echo "Installed. Warning at $warn idle minutes, stop at $stop."
echo "Emails: one warning (at most one per hour), then one 'stopped' email${wake_url:+ with your wake page link}."
echo "Logs: sudo journalctl -u ec2-idle.service -b -n 30 --no-pager"
