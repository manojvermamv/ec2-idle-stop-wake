#!/usr/bin/env bash
# One-time install of the idle timer. Run it ON THE EC2 INSTANCE, from the src folder.
#   bash install_guest.sh [--topic SNS_TOPIC_ARN] [--wake-url URL] [--name NAME]
#                         [--warn 30] [--stop 45] [--probe /abs/path|none] [--test-email]
#                         [--python /path/to/python3] [--no-deps] [--deps-only]
#   bash install_guest.sh --uninstall [--purge]   (--purge also deletes the settings and saved state)
# --topic, --wake-url  AlertTopicArn and WakePageUrl from the stack outputs (GuestInstallCommand).
#              --topic none turns emails off (use it after removing the email address from the stack).
# --name       Friendly name for emails, for example ubuntu-cp.
# --probe      Executable that exits 0 while background work runs and 1 when idle ("none" clears it).
# --watch      Ready-made long-job check: stay on while builds, AI agents or training runs are working.
#              Give a comma-separated list of builds, agents, training (no value means all three),
#              or "--watch off" to turn it off. Off unless you ask for it.
# --watch-hours  Ignore builds and training runs older than this many hours (1 to 720, default 12).
# --monthly-email on|off  One short "still working" email a month while the instance runs (default on,
#              needs --topic).
# --python     Use this Python (3.6 or newer). Without it, the best one found is used.
# --no-deps    Don't install anything. By default a missing Python (and, on Debian and Ubuntu, the
#              package that makes SSH logins visible) is installed for you.
# --deps-only  Check, and install, the prerequisites. Print what was found and stop.
# Topic, wake URL, name, probe, watch, --warn and --stop are kept from the previous install when left out.
# A test email is sent only on the first install, when the topic changes, or with --test-email.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

topic="" wake_url="" name="" probe="" warn_given="" stop_given="" warn=30 stop=45 uninstall=0 purge=0 force_test=0
python_override="" no_deps=0 deps_only=0
set_topic=0 set_wake=0 set_name=0 set_probe=0 set_watch=0 set_watch_hours=0 set_monthly=0
watch="" watch_hours="" monthly=""
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit "${1:-0}"; }
while (($#)); do
    case $1 in
        --topic) topic=${2:?--topic needs a value}; set_topic=1; [[ $topic == none ]] && topic=""; shift 2 ;;
        --wake-url) wake_url=${2:?--wake-url needs a value}; set_wake=1; shift 2 ;;
        --name) name=${2:?--name needs a value}; set_name=1; shift 2 ;;
        --probe) probe=${2:?--probe needs a value}; set_probe=1; shift 2 ;;
        --watch)
            if [[ -n ${2:-} && ${2:-} != -* ]]; then watch=$2; shift 2; else watch=builds,agents,training; shift; fi
            set_watch=1 ;;
        --watch-hours) watch_hours=${2:?--watch-hours needs a value}; set_watch_hours=1; shift 2 ;;
        --monthly-email) monthly=${2:?--monthly-email needs on or off}; set_monthly=1; shift 2 ;;
        --warn) warn=${2:?--warn needs a value}; warn_given=1; shift 2 ;;
        --stop) stop=${2:?--stop needs a value}; stop_given=1; shift 2 ;;
        --python) python_override=${2:?--python needs a value}; shift 2 ;;
        --test-email) force_test=1; shift ;;
        --no-deps) no_deps=1; shift ;;
        --deps-only) deps_only=1; shift ;;
        --uninstall) uninstall=1; shift ;;
        --purge) purge=1; shift ;;
        -h | --help) usage ;;
        *) echo "Unknown option: $1" >&2; usage 2 ;;
    esac
done

fail() { echo "install_guest: $*" >&2; exit 1; }
[[ $(uname -s) == Linux ]] || fail "run this ON THE EC2 INSTANCE (SSH, EC2 Instance Connect or Session Manager), not on your computer. Windows and macOS instances aren't supported."

# Run a command as root: directly when we already are root, otherwise through sudo.
if ((EUID == 0)); then
    root() { "$@"; }
elif command -v sudo >/dev/null 2>&1; then
    root() { sudo "$@"; }
else
    fail "this needs root. Run it as root, or install sudo first."
fi

# The timer runs as root, so root must be the only one able to change what it runs. A path passes only if
# the file it leads to is owned by root, and every folder above that file AND above the path as given is
# writable only by root. (A link kept in a folder you can write could otherwise be pointed somewhere else later.)
root_only() {   # root_only <path>: it and every folder above it belong to root, with no group or other write bit
    local path=$1 owner mode
    while :; do
        read -r owner mode < <(stat -L -c '%u %a' -- "$path" 2>/dev/null) || return 1   # -L: /bin and /usr/sbin are links to folders root controls
        [[ $owner == 0 ]] || return 1
        (( (8#$mode & 8#022) == 0 )) || return 1
        [[ $path == / ]] && return 0
        path=$(dirname -- "$path")
    done
}
trusted() {
    local given=$1 real
    [[ $given == /* ]] || given=$PWD/$given
    real=$(readlink -f -- "$given" 2>/dev/null) || return 1
    root_only "$real" && root_only "$(dirname -- "$given")"
}

if ((uninstall)); then
    root systemctl stop ec2-idle.timer ec2-idle.service 2>/dev/null || true
    root systemctl disable ec2-idle.timer 2>/dev/null || true
    root rm -f /etc/systemd/system/ec2-idle.service /etc/systemd/system/ec2-idle.timer \
        /usr/local/sbin/ec2_idle.py
    root rm -rf /run/systemd/system/ec2-idle.service.d /run/ec2-idle-test.env
    root systemctl daemon-reload
    if ((purge)); then
        root rm -rf /etc/ec2-idle.conf /etc/ec2-idle.conf.bak /run/ec2-idle /var/lib/ec2-idle
        echo "Idle timer removed completely, including its settings and saved state."
        echo "Optional: delete this downloaded folder with: rm -rf ~/ec2-idle-stop-wake"
    else
        echo "Idle timer removed. Settings kept in /etc/ec2-idle.conf, so a later install reuses them."
        echo "For a completely clean removal, run again with: --uninstall --purge"
    fi
    exit 0
fi

# ---------------------------------------------------------------- prerequisites
distro=$( . /etc/os-release 2>/dev/null && printf '%s' "${PRETTY_NAME:-${ID:-Linux}}" ) || distro="Linux"

package_manager() {
    local manager
    for manager in apt-get dnf yum zypper pacman apk; do
        if command -v "$manager" >/dev/null 2>&1; then echo "$manager"; return 0; fi
    done
    return 1
}

python_package() {   # the package that provides python3 under each package manager
    case $1 in pacman) echo python ;; *) echo python3 ;; esac
}

install_packages() { # install_packages <manager> <package>...
    local manager=$1; shift
    case $manager in
        apt-get) root env DEBIAN_FRONTEND=noninteractive apt-get update -qq \
                 && root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
        dnf | yum) root "$manager" install -y -q "$@" ;;
        zypper) root zypper --non-interactive install "$@" ;;
        pacman) root pacman -Syu --noconfirm --needed "$@" ;;   # Arch does not support partial upgrades (-Sy alone)
        apk) root apk add --no-cache "$@" ;;
        *) return 1 ;;
    esac
}

find_python() {      # the first interpreter that is Python 3.6 or newer
    local candidate path
    for candidate in "$python_override" python3 python3.14 python3.13 python3.12 python3.11 \
        python3.10 python3.9 python3.8 python3.7 python3.6; do
        [[ -n $candidate ]] || continue
        path=$(command -v "$candidate" 2>/dev/null) || continue
        trusted "$path" || continue   # the timer runs it as root
        if "$path" -c 'import sys; sys.exit(sys.version_info < (3, 6))' 2>/dev/null; then
            echo "$path"; return 0
        fi
    done
    return 1
}

untrusted_python() {   # a usable Python that was skipped only because root does not control it
    local candidate path
    for candidate in "$python_override" python3 python3.14 python3.13 python3.12 python3.11 \
        python3.10 python3.9 python3.8 python3.7 python3.6; do
        [[ -n $candidate ]] || continue
        path=$(command -v "$candidate" 2>/dev/null) || continue
        if "$path" -c 'import sys; sys.exit(sys.version_info < (3, 6))' 2>/dev/null && ! trusted "$path"; then
            echo "$path"; return 0
        fi
    done
    return 1
}

manager=$(package_manager || true)
python=$(find_python || true)
if [[ -z $python ]]; then
    skipped=$(untrusted_python || true)
    if [[ -n $skipped ]]; then
        echo "Found Python at $skipped, but it is not used: the timer runs as root, so its Python must be owned by root" \
            "and sit in folders only root can change (this often means pyenv, conda or a Python in your home folder)."
    fi
    ((no_deps)) && fail "Python 3.6 or newer is required. Install it, or pass --python /path/to/python3."
    [[ -n $manager ]] || fail "Python 3.6 or newer is required, and no supported package manager (apt, dnf, yum, zypper, pacman, apk) was found. Install Python yourself, then run this again."
    echo "Python 3.6 or newer was not found. Installing it with $manager (pass --no-deps to skip)..."
    install_packages "$manager" "$(python_package "$manager")" \
        || fail "could not install Python with $manager. Install Python 3.6 or newer yourself, then run this again."
    python=$(find_python || true)
    [[ -n $python ]] || fail "Python was installed but no Python 3.6 or newer owned by root was found. Pass --python /usr/bin/python3, or the path of a system Python."
fi
python_version=$("$python" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')

systemd_running=no
[[ -d /run/systemd/system ]] && systemd_running=yes
systemd_version=$(systemctl --version 2>/dev/null | awk 'NR == 1 { print $2 }' || true)

# Debian and Ubuntu register SSH logins as sessions only with libpam-systemd. Without it the
# timer cannot see you and would stop the instance while you are connected.
if [[ $manager == apt-get && $systemd_running == yes ]] && ! ((no_deps)) \
    && ! dpkg -s libpam-systemd >/dev/null 2>&1; then
    echo "Installing libpam-systemd, so SSH logins are visible to the timer..."
    install_packages apt-get libpam-systemd \
        || echo "Could not install libpam-systemd. Continuing; the check below says if SSH logins aren't seen."
fi

printf 'Detected: %s | package manager: %s | python: %s (%s) | systemd: %s (running: %s)\n' \
    "$distro" "${manager:-none}" "$python" "$python_version" "${systemd_version:-none}" "$systemd_running"
((deps_only)) && exit 0

command -v systemctl >/dev/null 2>&1 && [[ $systemd_running == yes ]] \
    || fail "systemd is not running here. The timer needs systemd, which every mainstream Linux on EC2 has (Amazon Linux, Ubuntu, Debian, RHEL, Rocky, Alma, SUSE, Fedora). Alpine and containers don't."

# ---------------------------------------------------------------- settings
# Keep earlier values for anything not given this time.
previous() { { root sed -n "s/^$1=//p" /etc/ec2-idle.conf 2>/dev/null || true; } | tail -n 1 | sed 's/^"\(.*\)"$/\1/'; }
if root grep -Eq '^IDLE_MODE=.*dry-run' /etc/ec2-idle.conf 2>/dev/null; then
    fail "/etc/ec2-idle.conf still has IDLE_MODE=dry-run, which this version no longer supports. Reinstalling would start real stops. Delete that line (sudoedit /etc/ec2-idle.conf), then run this again."
fi
old_topic=$(previous SNS_TOPIC_ARN)
if [[ -z $warn_given ]]; then warn=$(previous WARN_MINUTES); warn=${warn:-30}; fi
if [[ -z $stop_given ]]; then stop=$(previous STOP_MINUTES); stop=${stop:-45}; fi
((set_topic)) || topic=$old_topic
((set_wake)) || wake_url=$(previous WAKE_PAGE_URL)
((set_name)) || name=$(previous INSTANCE_NAME)
((set_probe)) || probe=$(previous WORK_PROBE)
[[ $probe == none ]] && probe=""
((set_watch)) || watch=$(previous WATCH)
((set_watch_hours)) || watch_hours=$(previous WATCH_MAX_HOURS)
watch_hours=${watch_hours:-12}
((set_monthly)) || monthly=$(previous MONTHLY_EMAIL)
monthly=${monthly:-on}
[[ $watch == off ]] && watch=""

# WATCH is empty or a list of known group names, so nothing else can reach the root-owned settings file.
valid_watch() { [[ -z $1 || $1 =~ ^(builds|agents|training)(,(builds|agents|training))*$ ]]; }

[[ $warn =~ ^[0-9]+$ && $stop =~ ^[0-9]+$ ]] && ((0 < warn && warn < stop)) \
    || fail "--warn and --stop are minutes, and --warn must be less than --stop"
if [[ -n $probe ]]; then
    [[ $probe =~ ^/[A-Za-z0-9._/+@-]+$ && -x $probe ]] \
        || fail "--probe must be the absolute path of an executable (letters, numbers and . _ / + @ - only, no spaces)"
    trusted "$probe" || fail "--probe $probe must be owned by root, and neither it nor its folders may be writable by anyone else, because the timer runs it as root."
fi
valid_watch "$watch" \
    || fail "--watch takes builds, agents and/or training, separated by commas (for example --watch builds,agents), or off. Leave the value out to watch all three."
[[ $watch_hours =~ ^[0-9]{1,3}$ ]] && ((10#$watch_hours >= 1 && 10#$watch_hours <= 720)) \
    || fail "--watch-hours must be a whole number of hours from 1 to 720"
watch_hours=$((10#$watch_hours))
[[ $monthly == on || $monthly == off ]] || fail "--monthly-email must be on or off"
if [[ -n $topic && ! $topic =~ ^arn:[a-z-]+:sns:[a-z0-9-]+:[0-9]{12}:[A-Za-z0-9_-]+$ ]]; then
    fail "--topic must be an SNS topic ARN"
fi
if [[ -n $wake_url && ! $wake_url =~ ^https://[A-Za-z0-9./-]+$ ]]; then
    fail "--wake-url must be the https:// WakePageUrl from the stack outputs"
fi
if [[ -n $name && ! $name =~ ^[A-Za-z0-9._\ -]{1,64}$ ]]; then
    fail "--name may use letters, numbers, spaces, dots, dashes and underscores (up to 64)"
fi
if [[ -n $python_override ]] && ! trusted "$(command -v "$python_override" 2>/dev/null || echo /nonexistent)"; then
    fail "--python $python_override must be owned by root, and neither it nor its folders may be writable by anyone else, because the timer runs it as root."
fi

conf=$(mktemp)
unit=$(mktemp)
trap 'rm -f -- "$conf" "$unit"' EXIT
sed -e "s|^WARN_MINUTES=.*|WARN_MINUTES=$warn|" \
    -e "s|^STOP_MINUTES=.*|STOP_MINUTES=$stop|" -e "s|^SNS_TOPIC_ARN=.*|SNS_TOPIC_ARN=$topic|" \
    -e "s|^WAKE_PAGE_URL=.*|WAKE_PAGE_URL=$wake_url|" -e "s|^INSTANCE_NAME=.*|INSTANCE_NAME=\"$name\"|" \
    -e "s|^MONTHLY_EMAIL=.*|MONTHLY_EMAIL=$monthly|" -e "s|^WATCH=.*|WATCH=$watch|" -e "s|^WATCH_MAX_HOURS=.*|WATCH_MAX_HOURS=$watch_hours|" \
    -e "s|^WORK_PROBE=.*|WORK_PROBE=$probe|" ec2-idle.conf > "$conf"
sed "s|^ExecStart=/usr/bin/python3 |ExecStart=$python |" ec2-idle.service > "$unit"
grep -q "^ExecStart=$python -I -S " "$unit" || fail "could not set the Python path ($python) in the service file"

# ---------------------------------------------------------------- check first, install second
# The new settings are tried out from this folder while the old files (and a timer that may already be
# running) are left alone. Nothing is replaced unless the check passes.
check_args=(--check)
[[ -n ${SSH_CONNECTION:-} ]] && check_args+=(--expect-session)
if [[ -n $topic ]] && { ((force_test)) || [[ $topic != "$old_topic" ]]; }; then
    check_args+=(--test-email)
fi
echo "Checking this host..."
if ! root bash -c 'set -a; . "$1"; set +a; shift; exec "$@"' _ "$conf" \
    "$python" -I -S "$PWD/ec2_idle.py" "${check_args[@]}"; then
    cat >&2 <<'HELP'

The timer was NOT installed or changed. Fix the problem named above, then run this script again.
Common fixes:
  - No IAM role: attach the stack's role (Setup Step 3), wait a minute, run this again.
  - Instance metadata off: EC2 console -> Actions -> Instance settings -> Modify instance
    metadata options -> Enabled.
  - Your SSH login isn't seen: on Debian and Ubuntu install libpam-systemd and make sure
    sshd has UsePAM yes, then log in again.
HELP
    exit 1
fi

if root test -e /etc/ec2-idle.conf; then
    root cp -p /etc/ec2-idle.conf /etc/ec2-idle.conf.bak
    echo "Saved the previous settings to /etc/ec2-idle.conf.bak"
fi

# Keep the files being replaced, so a failure part-way through puts everything back as it was.
installed_files=(/etc/ec2-idle.conf /usr/local/sbin/ec2_idle.py /etc/systemd/system/ec2-idle.service
                 /etc/systemd/system/ec2-idle.timer)
keep=$(mktemp -d)
installing=0
roll_back() {
    local file
    for file in "${installed_files[@]}"; do
        if [[ -e $keep/$(basename "$file") ]]; then root cp -p "$keep/$(basename "$file")" "$file"
        else root rm -f -- "$file"; fi
    done
    root systemctl daemon-reload || true
}
on_exit() {
    local status=$?
    if ((installing && status != 0)); then
        echo "install_guest: the install did not finish, so the previous files are being put back." >&2
        roll_back
    fi
    rm -f -- "$conf" "$unit"
    root rm -rf -- "$keep"
}
trap on_exit EXIT
for file in "${installed_files[@]}"; do
    if root test -e "$file"; then root cp -p "$file" "$keep/$(basename "$file")"; fi
done

# ---------------------------------------------------------------- install
installing=1
root install -m 0640 -o root -g root "$conf" /etc/ec2-idle.conf
root install -m 0755 -o root -g root ec2_idle.py /usr/local/sbin/ec2_idle.py
root install -m 0644 -o root -g root "$unit" /etc/systemd/system/ec2-idle.service
root install -m 0644 -o root -g root ec2-idle.timer /etc/systemd/system/ec2-idle.timer
if command -v restorecon >/dev/null 2>&1; then   # SELinux (RHEL, Rocky, Alma, Fedora, Amazon Linux)
    root restorecon -F /usr/local/sbin/ec2_idle.py /etc/ec2-idle.conf \
        /etc/systemd/system/ec2-idle.service /etc/systemd/system/ec2-idle.timer 2>/dev/null || true
fi
root systemd-analyze verify /etc/systemd/system/ec2-idle.service /etc/systemd/system/ec2-idle.timer \
    || echo "(The warnings above are normal on older systemd, which ignores settings it doesn't know.)"
root systemctl daemon-reload

# `enable --now` needs systemd 220, so enable and start separately for older systems.
root systemctl enable ec2-idle.timer
root systemctl restart ec2-idle.timer
installing=0
echo
echo "Installed. Warning at $warn idle minutes, stop at $stop."
echo "Emails: one warning (at most one per hour), then one 'stopped' email${wake_url:+ with your wake page link}."
if [[ -n $topic && $monthly == on ]]; then
    echo "Monthly note: one short 'still working' email a month while the instance runs (turn off with --monthly-email off)."
fi
if [[ -n $watch ]]; then
    echo "Long-job check: staying on while these run: ${watch//,/, } (ignoring builds and training older than $watch_hours hours)."
fi
echo "Logs: sudo journalctl -u ec2-idle.service -b -n 30 --no-pager"
