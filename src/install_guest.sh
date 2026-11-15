#!/usr/bin/env bash
# One-time install of the idle watcher. Run it over SSH on the instance, from the src folder.
#   bash install_guest.sh [--topic SNS_TOPIC_ARN] [--warn 30] [--stop 45] [--probe /abs/path] [--dry-run]
#   bash install_guest.sh --uninstall
# --topic   AlertTopicArn from the stack outputs. Leave it out to stop without warning emails.
# --probe   Executable that exits 0 while background work runs and 1 when idle.
# --dry-run Log what would happen instead of stopping (for testing).
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

topic="" warn=30 stop=45 probe="" mode=stop uninstall=0
usage() { sed -n '2,8p' "$0"; exit "${1:-0}"; }
while (($#)); do
    case $1 in
        --topic) topic=${2:?--topic needs a value}; shift 2 ;;
        --warn) warn=${2:?--warn needs a value}; shift 2 ;;
        --stop) stop=${2:?--stop needs a value}; shift 2 ;;
        --probe) probe=${2:?--probe needs a value}; shift 2 ;;
        --dry-run) mode=dry-run; shift ;;
        --uninstall) uninstall=1; shift ;;
        -h | --help) usage ;;
        *) echo "Unknown option: $1" >&2; usage 2 ;;
    esac
done

if ((uninstall)); then
    sudo systemctl disable --now ec2-idle.timer 2>/dev/null || true
    sudo systemctl stop ec2-idle.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/ec2-idle.service /etc/systemd/system/ec2-idle.timer \
        /usr/local/sbin/ec2_idle.py
    sudo systemctl daemon-reload
    echo "Idle watcher removed. Settings kept in /etc/ec2-idle.conf (sudo rm it if unwanted)."
    exit 0
fi

fail() { echo "install_guest: $*" >&2; exit 1; }
for tool in systemctl loginctl python3 pgrep; do
    command -v "$tool" >/dev/null || fail "$tool is required"
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || fail "Python 3.8 or newer is required"
[[ $warn =~ ^[0-9]+$ && $stop =~ ^[0-9]+$ ]] && ((0 < warn && warn < stop)) \
    || fail "--warn and --stop are minutes, and --warn must be less than --stop"
if [[ -n $probe ]]; then
    [[ $probe == /* && -x $probe ]] || fail "--probe must be an absolute path to an executable"
fi
if [[ -n $topic && $topic != arn:*:sns:*:*:* ]]; then
    fail "--topic must be an SNS topic ARN"
fi

conf=$(mktemp)
trap 'rm -f -- "$conf"' EXIT
sed -e "s|^IDLE_MODE=.*|IDLE_MODE=$mode|" -e "s|^WARN_MINUTES=.*|WARN_MINUTES=$warn|" \
    -e "s|^STOP_MINUTES=.*|STOP_MINUTES=$stop|" -e "s|^SNS_TOPIC_ARN=.*|SNS_TOPIC_ARN=$topic|" \
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
[[ -n $topic ]] && check_args+=(--test-email)
echo "Checking this host..."
if ! sudo bash -c 'set -a; . /etc/ec2-idle.conf; set +a; exec python3 /usr/local/sbin/ec2_idle.py "$@"' \
    _ "${check_args[@]}"; then
    if [[ $mode == stop ]]; then
        echo "The timer was NOT enabled. Fix the problems above and run this script again." >&2
        exit 1
    fi
    echo "Continuing because --dry-run is set."
fi

sudo systemctl enable --now ec2-idle.timer
echo
echo "Installed. Mode $mode: warning at $warn idle minutes, stop at $stop."
echo "Logs: sudo journalctl -u ec2-idle.service -b -n 30 --no-pager"
