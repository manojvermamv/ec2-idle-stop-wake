#!/usr/bin/env bash
# READ-ONLY. Checks the 5 things that can block setup. It never creates, changes or deletes anything.
#   1. EBS root disk   2. Not Windows or Spot   3. Instance metadata on
#   4. Existing role   5. Elastic IP and SSH route
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

echo "Region: $region  (must be your project's selected Region)"
if [[ -z $instance ]]; then
    ro ec2 describe-instances \
        --filters Name=instance-state-name,Values=pending,running,stopping,stopped \
        --query 'Reservations[].Instances[].{Instance:InstanceId,Name:(Tags[?Key==`Name`].Value)[0],State:State.Name,Type:InstanceType}' \
        --output table || { echo "Not signed in. Open AWS CloudShell, or run: aws login" >&2; exit 1; }
    printf '\nNext: bash discover.sh --instance <INSTANCE_ID>\n'
    exit 0
fi

read -r root lifecycle ami profile public state endpoint platform < <(
    ro ec2 describe-instances --instance-ids "$instance" --output text --query \
    'Reservations[0].Instances[0].[RootDeviceType,InstanceLifecycle,ImageId,IamInstanceProfile.Arn,PublicIpAddress,State.Name,MetadataOptions.HttpEndpoint,Platform]'
) || { echo "Cannot describe $instance in $region." >&2; exit 1; }
image=$(ro ec2 describe-images --image-ids "$ami" --query 'Images[0].Name' --output text 2>/dev/null)
printf 'Instance %s: %s, image %s\n\n' "$instance" "$state" "${image:-unknown}"

# 1. EBS root disk
if [[ $root == ebs ]]; then echo "1. Root disk: EBS (ok)"; else
    echo "1. Root disk: $root"; note "UNSUPPORTED: root device is $root. Only EBS-backed instances can stop and start."; fi

# 2. Not Windows or Spot
kind=Linux
if [[ ${platform,,} == windows ]]; then kind=Windows; note "UNSUPPORTED: Windows instance. The idle timer needs Linux with systemd."; fi
[[ $lifecycle == spot ]] && { kind="$kind, Spot"; note "CHECK: Spot Instance. Only persistent Spot requests can be stopped."; }
echo "2. Type: $kind"

# 3. Instance metadata on
echo "3. Instance metadata: $endpoint"
[[ $endpoint == enabled ]] || note "FIX: instance metadata is disabled. The idle timer needs it enabled."

# 4. Existing role
role=""
if [[ $profile != None ]]; then
    role=$(aws --no-cli-pager iam get-instance-profile --instance-profile-name "${profile##*/}" \
        --query 'InstanceProfile.Roles[0].RoleName' --output text 2>/dev/null)
    [[ $role == None ]] && role=""
fi
if [[ -n $role ]]; then
    echo "4. Role: $role"
    note "NOTE: the stack will add its policy (stop this instance, send its warning email) to your existing role $role."
else
    echo "4. Role: none (the stack creates one; you attach it in Setup Step 3)"
fi

# 5. Elastic IP and SSH route
eip=$(ro ec2 describe-addresses --filters "Name=instance-id,Values=$instance" \
    --query 'Addresses[0].PublicIp' --output text 2>/dev/null)
[[ $eip == None ]] && eip=""
echo "5. Elastic IP: ${eip:-none}"
[[ -n $eip ]] && note "KEEP: the instance already has Elastic IP $eip. Keep it, and don't allocate another."
read -r -a groups < <(ro ec2 describe-instances --instance-ids "$instance" --output text \
    --query 'Reservations[0].Instances[0].SecurityGroups[].GroupId')
rules=""
if ((${#groups[@]})); then
    rules=$(ro ec2 describe-security-groups --group-ids "${groups[@]}" --output text --query \
        'SecurityGroups[].IpPermissions[?IpProtocol==`-1` || (FromPort<=`22` && ToPort>=`22`)][].[join(`,`,IpRanges[].CidrIp),join(`,`,Ipv6Ranges[].CidrIpv6),join(`,`,PrefixListIds[].PrefixListId)]')
fi
endpoints=$(ro ec2 describe-instance-connect-endpoints --output text 2>/dev/null \
    --query 'InstanceConnectEndpoints[?State==`create-complete`].InstanceConnectEndpointId')
# Only CIDR ranges and prefix lists can reach the instance from outside; a rule that
# points at another security group (or itself) cannot.
outside=$(awk -F'\t' '{ for (i = 1; i <= 3; i++) if ($i != "" && $i != "None") { print "yes"; exit } }' <<< "$rules")
if [[ -n $outside ]]; then route="yes (a port 22 rule for an IP range)"; else route="none"; fi
echo "   SSH route from outside: $route   Instance Connect Endpoints: ${endpoints:-none}"
grep -Eq '0\.0\.0\.0/0|::/0' <<< "$rules" \
    && note "RECOMMEND: port 22 is open to the whole internet. Limit it to your IP if you can."
if [[ -z $outside && ( -z $endpoints || $endpoints == None ) ]]; then
    note "CHECK: no SSH route from outside. Add an inbound port 22 rule for your IP (or the EC2 Instance Connect prefix list), or create an Instance Connect Endpoint. The installer must run on the instance."
fi

if [[ -n $eip ]]; then ip_choice="keep $eip"; else ip_choice="changing, or allocate one"; fi
echo
echo "Suggested stack settings: InstanceId=$instance  InstanceRoleName=$role  IP: $ip_choice"
echo
echo "Findings:"
if ((${#notes[@]})); then printf -- '- %s\n' "${notes[@]}"; else echo "- No blockers found."; fi
echo
echo "Nothing was changed. Keep this output outside your Git repository."
