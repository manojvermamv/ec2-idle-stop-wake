#!/usr/bin/env bash
# TEMPORARY SSH access, only for the install. It adds ONE inbound port 22 rule for ONE address
# (your current public IP, as a /32) to the instance's security group, and takes it out again.
#   bash temp_ssh.sh open   <INSTANCE_ID> [--minutes 30] [--ip 203.0.113.7] [--port 22]
#   bash temp_ssh.sh status <INSTANCE_ID>
#   bash temp_ssh.sh close  <INSTANCE_ID>
#   Options for all three: [--group sg-0abc...] [--region REGION]
# The rule is tagged Purpose=ec2-idle-temp and its description says when it should end
# ("ec2-idle-temp until 2026-11-16T12:30:00Z"). AWS cannot remove a rule by itself, so run
# `close` when you are done. `open`, `status` and `close` also remove any rule of this kind
# that has already passed its end time.
# The rule applies to EVERY instance that uses the security group. Use --group to pick another.
# Run it on YOUR OWN computer (AWS CLI v2 signed in), because it uses the public address it is run from.
# In CloudShell that is CloudShell's address, not yours: add --ip <YOUR_ADDRESS> there.
# `close` removes every rule of ours in the group, or only the one matching --ip and --port if you give them.
set -uo pipefail

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit "${1:-0}"; }
fail() { echo "temp_ssh: $*" >&2; exit 1; }

command_name=${1:-}
[[ $command_name == -h || $command_name == --help || -z $command_name ]] && usage
instance=${2:-}
shift 2 2>/dev/null || true
minutes=30 ip="" port=22 ip_given="" port_given="" group="" region=${AWS_REGION:-${AWS_DEFAULT_REGION:-}}
while (($#)); do
    case $1 in
        --minutes) minutes=${2:?--minutes needs a value}; shift 2 ;;
        --ip) ip=${2:?--ip needs a value}; ip_given=1; shift 2 ;;
        --port) port=${2:?--port needs a value}; port_given=1; shift 2 ;;
        --group) group=${2:?--group needs a value}; shift 2 ;;
        --region) region=${2:?--region needs a value}; shift 2 ;;
        -h | --help) usage ;;
        *) echo "Unknown option: $1" >&2; usage 2 ;;
    esac
done

[[ $command_name =~ ^(open|status|close)$ ]] || fail "the first word must be open, status or close"
[[ $instance =~ ^i-[0-9a-f]+$ ]] || fail "give the instance ID, for example i-0abc1234def567890"
[[ $minutes =~ ^[0-9]+$ ]] && ((minutes >= 1 && minutes <= 1440)) || fail "--minutes must be 1 to 1440"
[[ $port =~ ^[0-9]+$ ]] && ((port >= 1 && port <= 65535)) || fail "--port must be 1 to 65535"
command -v aws >/dev/null 2>&1 || fail "AWS CLI v2 is required. AWS CloudShell has it."
[[ -n $region ]] || region=$(aws configure get region 2>/dev/null)
[[ -n $region ]] || fail "set a Region: --region <REGION>"

ro() { aws --region "$region" --no-cli-pager "$@"; }

if [[ -z $group ]]; then
    group=$(ro ec2 describe-instances --instance-ids "$instance" \
        --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text) \
        || fail "cannot read $instance in $region. Are you signed in, and is this the right Region?"
    [[ $group == sg-* ]] || fail "$instance has no security group"
fi
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# One line per rule of ours: id, address, port, end time (ISO times sort like text).
# Stops with an error if AWS cannot list the rules, so "cannot read" is never mistaken for "no rules".
our_rules() {
    local listed id cidr from description
    listed=$(ro ec2 describe-security-group-rules \
        --filters "Name=group-id,Values=$group" "Name=tag:Purpose,Values=ec2-idle-temp" \
        --query 'SecurityGroupRules[?!IsEgress].[SecurityGroupRuleId,CidrIpv4,FromPort,Description]' \
        --output text 2>&1) || fail "could not list the security group rules: $listed"
    while IFS=$'\t' read -r id cidr from description; do
        [[ -n $id ]] && printf '%s\t%s\t%s\t%s\n' "$id" "$cidr" "$from" "${description##* until }"
    done <<< "$listed"
    return 0
}

remove_rules() {   # remove_rules <rule id>...  (stops with an error if AWS refuses)
    (($#)) || return 0
    local result
    result=$(ro ec2 revoke-security-group-ingress --group-id "$group" --security-group-rule-ids "$@" 2>&1) \
        || fail "AWS did not remove the rule, so it may still be open: $result"
}

remove_expired() {
    local ids=() id cidr from until rules
    rules=$(our_rules) || exit 1
    while IFS=$'\t' read -r id cidr from until; do
        if [[ -n $id && $until < $now ]]; then ids+=("$id"); echo "Removed an expired rule: $cidr port $from (ended $until)"; fi
    done <<< "$rules"
    ((${#ids[@]})) && remove_rules "${ids[@]}"
    return 0
}

case $command_name in
    status)
        remove_expired
        echo "Security group $group ($region), checked $now:"
        found=0
        rules=$(our_rules) || exit 1
        while IFS=$'\t' read -r id cidr from until; do
            [[ -n $id ]] || continue
            found=1; echo "  open: $cidr port $from until $until  ($id)"
        done <<< "$rules"
        ((found)) || echo "  no temporary rule is open."
        ;;
    close)
        remove_expired
        ids=() ports=""
        rules=$(our_rules) || exit 1
        while IFS=$'\t' read -r id cidr from until; do
            [[ -n $id ]] || continue
            [[ -z $ip_given || $cidr == "$ip/32" ]] || continue
            [[ -z $port_given || $from == "$port" ]] || continue
            ids+=("$id"); ports="$ports $from"; echo "Removing: $cidr port $from  ($id)"
        done <<< "$rules"
        if ((${#ids[@]})); then
            remove_rules "${ids[@]}"
            echo "Done. The temporary rule is removed (port${ports} closed to the outside again)."
        else echo "No matching temporary rule was open on $group."; fi
        ;;
    open)
        remove_expired
        if [[ -z $ip ]]; then
            ip=$(curl -fsS -m 10 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]') \
                || fail "could not find your public IP. Pass it with --ip <ADDRESS>."
        fi
        [[ $ip =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || fail "$ip is not an IPv4 address. Pass yours with --ip"
        [[ $ip != 0.0.0.0 ]] || fail "refusing to open the port to everyone"
        end=$(date -u -d "+$minutes minutes" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
            || date -u -v+"${minutes}"M +%Y-%m-%dT%H:%M:%SZ)
        rules=$(our_rules) || exit 1
        while IFS=$'\t' read -r id cidr from until; do
            if [[ $cidr == "$ip/32" && $from == "$port" ]]; then
                echo "Already open for $ip on port $port until $until. Run close first to change the time."; exit 0
            fi
        done <<< "$rules"
        permissions=$(printf '[{"IpProtocol":"tcp","FromPort":%s,"ToPort":%s,"IpRanges":[{"CidrIp":"%s/32","Description":"ec2-idle-temp until %s"}]}]' \
            "$port" "$port" "$ip" "$end")
        rule=$(ro ec2 authorize-security-group-ingress --group-id "$group" --ip-permissions "$permissions" \
            --tag-specifications 'ResourceType=security-group-rule,Tags=[{Key=Purpose,Value=ec2-idle-temp}]' \
            --query 'SecurityGroupRules[0].SecurityGroupRuleId' --output text) \
            || fail "could not add the rule. If a rule for $ip/32 on port $port already exists, nothing is needed."
        echo "Opened port $port on $group for $ip only, until $end ($minutes minutes). Rule: $rule"
        echo "It applies to every instance that uses $group."
        echo "Connect now, run the installer ON THE INSTANCE, then run: bash temp_ssh.sh close $instance"
        ;;
esac
