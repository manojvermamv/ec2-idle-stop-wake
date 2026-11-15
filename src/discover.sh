#!/usr/bin/env bash
# READ-ONLY. Checks the 5 things that can block setup. It never creates, changes or deletes anything.
#   1. Can it stop and start? (EBS root disk, no stop protection, not in an Auto Scaling group,
#      no local disks that lose their data)   2. Not Windows, Mac or Spot
#   3. Instance metadata on   4. Existing role   5. Elastic IP and SSH route
# Run in AWS CloudShell, or any shell with AWS CLI v2 credentials.
#   bash discover.sh                       list your instances
#   bash discover.sh --instance i-0abc...  check one instance      [--region REGION]
set -uo pipefail

instance=""
region=${AWS_REGION:-${AWS_DEFAULT_REGION:-}}
while (($#)); do
    case $1 in
        --instance) instance=${2:?--instance needs an ID}; shift 2 ;;
        --region) region=${2:?--region needs a value}; shift 2 ;;
        -h | --help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done
command -v aws >/dev/null || { echo "AWS CLI v2 is required. AWS CloudShell has it." >&2; exit 2; }
[[ -n $region ]] || region=$(aws configure get region 2>/dev/null)
[[ -n $region ]] || { echo "Set a Region: bash discover.sh --region <REGION>" >&2; exit 2; }

ro() { aws --region "$region" --no-cli-pager "$@"; }
notes=()
note() { notes+=("$1"); }
# chk <what> <command...>: run a read. If AWS refuses (for example a missing permission), say so,
# instead of treating "could not read it" as "nothing there".
unreadable=$(mktemp)
trap 'rm -f -- "$unreadable"' EXIT
chk() {
    local what=$1 out
    shift
    if out=$("$@" 2>&1); then printf '%s' "$out"; else echo "$what" >> "$unreadable"; return 1; fi
}

echo "Region: $region  (must be your project's selected Region)"
if [[ -z $instance ]]; then
    ro ec2 describe-instances \
        --filters Name=instance-state-name,Values=pending,running,stopping,stopped \
        --query 'Reservations[].Instances[].{Instance:InstanceId,Name:(Tags[?Key==`Name`].Value)[0],State:State.Name,Type:InstanceType}' \
        --output table || { echo "Not signed in. Open AWS CloudShell, or run: aws login" >&2; exit 1; }
    printf '\nNext: bash discover.sh --instance <INSTANCE_ID>\n'
    exit 0
fi

# shellcheck disable=SC2034  # 'public' is read only to keep the other columns in place
read -r root lifecycle ami profile public state endpoint platform itype arch < <(
    ro ec2 describe-instances --instance-ids "$instance" --output text --query \
    'Reservations[0].Instances[0].[RootDeviceType,InstanceLifecycle,ImageId,IamInstanceProfile.Arn,PublicIpAddress,State.Name,MetadataOptions.HttpEndpoint,Platform,InstanceType,Architecture]'
) || { echo "Cannot describe $instance in $region." >&2; exit 1; }
image=$(ro ec2 describe-images --image-ids "$ami" --query 'Images[0].Name' --output text 2>/dev/null)
printf 'Instance %s: %s, %s (%s), image %s\n\n' "$instance" "$state" "$itype" "$arch" "${image:-unknown}"

# 1. Can it stop and start?
problems=0
if [[ $root != ebs ]]; then
    problems=1; note "UNSUPPORTED: root device is $root. Only EBS-backed instances can stop and start."
fi
stop_protection=$(chk "stop protection" ro ec2 describe-instance-attribute --instance-id "$instance" --attribute disableApiStop \
    --query 'DisableApiStop.Value' --output text)
if [[ $stop_protection == True || $stop_protection == true ]]; then
    problems=1; note "FIX: stop protection is on, so the timer's stop would be refused. Turn it off: EC2 console -> Actions -> Instance settings -> Change stop protection."
fi
tags=$(chk "tags" ro ec2 describe-tags --filters "Name=resource-id,Values=$instance" --query 'Tags[].Key' --output text)
group_name=$(chk "Auto Scaling group tag" ro ec2 describe-tags --filters "Name=resource-id,Values=$instance" "Name=key,Values=aws:autoscaling:groupName" \
    --query 'Tags[0].Value' --output text)
if [[ -n $group_name && $group_name != None ]]; then
    problems=1; note "UNSUPPORTED: the instance belongs to Auto Scaling group $group_name, which replaces instances that stop."
fi
if grep -Eq 'eks:cluster-name|eks:nodegroup-name|karpenter\.sh/' <<< "$tags"; then
    problems=1; note "CHECK: this looks like a Kubernetes node (EKS or Karpenter). Stopping a node disrupts the cluster."
fi
local_disks=$(chk "local disks" ro ec2 describe-instance-types --instance-types "$itype" \
    --query 'InstanceTypes[0].InstanceStorageSupported' --output text)
if [[ $local_disks == True || $local_disks == true ]]; then
    problems=1; note "CHECK: $itype has local (instance store) disks. Their data is erased every time the instance stops. Your EBS disk is safe."
fi
if ((problems)); then echo "1. Can it stop and start: see the findings below"
elif [[ -s $unreadable ]]; then echo "1. Can it stop and start: UNKNOWN, some checks could not be read (see the findings below)"
else echo "1. Can it stop and start: yes (EBS disk, no stop protection, no Auto Scaling group, no local disks)"; fi

# 2. Not Windows, Mac or Spot
kind=Linux
if [[ $(tr '[:upper:]' '[:lower:]' <<< "$platform") == windows ]]; then kind=Windows; note "UNSUPPORTED: Windows instance. The idle timer needs Linux with systemd."; fi
if [[ $itype == mac* ]]; then kind="macOS"; note "UNSUPPORTED: Mac instance. The idle timer needs Linux with systemd."; fi
[[ $lifecycle == spot ]] && { kind="$kind, Spot"; note "CHECK: Spot Instance. Only persistent Spot requests can be stopped."; }
echo "2. Type: $kind"

# 3. Instance metadata on
echo "3. Instance metadata: $endpoint"
[[ $endpoint == enabled ]] || note "FIX: instance metadata is disabled. The idle timer needs it enabled."

# 4. Existing role
role=""
if [[ $profile != None ]]; then
    role=$(chk "the instance's existing role" aws --no-cli-pager iam get-instance-profile --instance-profile-name "${profile##*/}" \
        --query 'InstanceProfile.Roles[0].RoleName' --output text) \
        || { role=""; role_unknown=1; }
    [[ $role == None ]] && role=""
fi
if [[ -n $role ]]; then
    echo "4. Role: $role"
    note "NOTE: the stack will add its policy (stop this instance, send its warning email) to your existing role $role."
elif [[ -n ${role_unknown:-} ]]; then
    echo "4. Role: UNKNOWN (an instance profile is attached, but it could not be read)"
else
    echo "4. Role: none (the stack creates one; you attach it in Setup Step 3)"
fi

# 5. Elastic IP and SSH route
eip=$(chk "Elastic IP" ro ec2 describe-addresses --filters "Name=instance-id,Values=$instance" \
    --query 'Addresses[0].PublicIp' --output text)
[[ $eip == None ]] && eip=""
if grep -q "Elastic IP" "$unreadable"; then echo "5. Elastic IP: UNKNOWN (could not be read)"; else echo "5. Elastic IP: ${eip:-none}"; fi
[[ -n $eip ]] && note "KEEP: the instance already has Elastic IP $eip. Keep it, and don't allocate another."
read -r -a groups < <(ro ec2 describe-instances --instance-ids "$instance" --output text \
    --query 'Reservations[0].Instances[0].SecurityGroups[].GroupId')
rules=""
if ((${#groups[@]})); then
    rules=$(chk "security group rules" ro ec2 describe-security-groups --group-ids "${groups[@]}" --output text --query \
        'SecurityGroups[].IpPermissions[?IpProtocol==`-1` || (FromPort<=`22` && ToPort>=`22`)][].[join(`,`,IpRanges[].CidrIp),join(`,`,Ipv6Ranges[].CidrIpv6),join(`,`,PrefixListIds[].PrefixListId)]')
fi
leftover=$(chk "leftover temporary SSH rule" ro ec2 describe-security-group-rules \
    --filters "Name=group-id,Values=${groups[*]// /,}" "Name=tag:Purpose,Values=ec2-idle-temp" \
    --query 'length(SecurityGroupRules)' --output text)
[[ $leftover =~ ^[1-9][0-9]*$ ]] && note "NOTE: a temporary SSH rule from this project is still open on the security group. Run: bash temp_ssh.sh close $instance"
endpoints=$(chk "Instance Connect Endpoints" ro ec2 describe-instance-connect-endpoints --output text \
    --query 'InstanceConnectEndpoints[?State==`create-complete`].InstanceConnectEndpointId')
# Only CIDR ranges and prefix lists can reach the instance from outside; a rule that
# points at another security group (or itself) cannot.
outside=$(awk -F'\t' '{ for (i = 1; i <= 3; i++) if ($i != "" && $i != "None") { print "yes"; exit } }' <<< "$rules")
if [[ -n $outside ]]; then route="yes (a port 22 rule for an IP range)"; elif grep -q "security group rules" "$unreadable"; then route="UNKNOWN"; else route="none"; fi
echo "   SSH route from outside: $route   Instance Connect Endpoints: ${endpoints:-none}"
grep -Eq '0\.0\.0\.0/0|::/0' <<< "$rules" \
    && note "RECOMMEND: port 22 is open to the whole internet. Limit it to your IP if you can."
if [[ -z $outside && ( -z $endpoints || $endpoints == None ) ]] && ! grep -q "security group rules" "$unreadable"; then
    note "CHECK: no SSH route from outside. Add an inbound port 22 rule for your IP (or the EC2 Instance Connect prefix list), or create an Instance Connect Endpoint. The installer must run on the instance."
fi

if [[ -n $eip ]]; then ip_choice="keep $eip"; else ip_choice="changing, or allocate one"; fi
echo
echo "Suggested stack settings: InstanceId=$instance  InstanceRoleName=${role:-${role_unknown:+<check first>}}  IP: $ip_choice"
if [[ -s $unreadable ]]; then
    note "COULD NOT CHECK: $(sort -u "$unreadable" | paste -sd, - | sed 's/,/, /g'). AWS refused the read (usually a missing permission or the wrong profile), so those answers above are unknown, not \"fine\". Fix the access and run this again."
fi
echo
echo "Findings:"
if ((${#notes[@]})); then printf -- '- %s\n' "${notes[@]}"; else echo "- No blockers found."; fi
echo
echo "Nothing was changed. Keep this output outside your Git repository."
