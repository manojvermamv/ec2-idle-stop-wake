#!/usr/bin/env bash
# READ-ONLY discovery for EC2 idle stop and wake. It never creates, changes or deletes anything.
# Run in AWS CloudShell (already signed in) or any shell with AWS CLI v2 credentials.
#   bash discover.sh                       list candidate instances in the Region
#   bash discover.sh --instance i-0abc...  check one instance and suggest stack parameters
#   bash discover.sh --include-plan        also show the account plan (Free or Paid)
#   Options: --region REGION  --stack NAME (default ec2-idle)
set -uo pipefail

instance="" plan=0 stack="ec2-idle"
region=${AWS_REGION:-${AWS_DEFAULT_REGION:-}}
while (($#)); do
    case $1 in
        --instance) instance=${2:?--instance needs an ID}; shift 2 ;;
        --region) region=${2:?--region needs a value}; shift 2 ;;
        --stack) stack=${2:?--stack needs a name}; shift 2 ;;
        --include-plan) plan=1; shift ;;
        -h | --help) sed -n '2,7p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done
command -v aws >/dev/null || { echo "AWS CLI v2 is required. AWS CloudShell has it." >&2; exit 2; }
[[ -n $region ]] || region=$(aws configure get region 2>/dev/null)
[[ -n $region ]] || { echo "Set a Region: bash discover.sh --region <REGION>" >&2; exit 2; }

ro() { aws --region "$region" --no-cli-pager "$@"; }
section() { printf '\n== %s ==\n' "$1"; }
notes=()
note() { notes+=("$1"); }

section "Account"
if ! ro sts get-caller-identity --query '{Account:Account,SignedInAs:Arn}' --output table; then
    echo "Not signed in. Open AWS CloudShell, or run: aws login" >&2
    exit 1
fi
echo "Region: $region  (must be your project's selected Region)"

if ((plan)); then
    section "Account plan"
    aws --no-cli-pager freetier get-account-plan-state --region us-east-1 --output table \
        || echo "Plan state not available for this account."
fi

section "Instances in $region"
ro ec2 describe-instances \
    --filters Name=instance-state-name,Values=pending,running,stopping,stopped \
    --query 'Reservations[].Instances[].{Instance:InstanceId,Name:(Tags[?Key==`Name`].Value)[0],State:State.Name,Type:InstanceType,Root:RootDeviceType,Spot:InstanceLifecycle,Platform:PlatformDetails}' \
    --output table

section "Existing stack '$stack'"
status=$(ro cloudformation describe-stacks --stack-name "$stack" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null) || status="not deployed"
echo "Status: $status"
if [[ $status != "not deployed" ]]; then
    ro cloudformation describe-stacks --stack-name "$stack" \
        --query 'Stacks[0].Outputs[].{Output:OutputKey,Value:OutputValue}' --output table
fi

if [[ -z $instance ]]; then
    printf '\nNext: bash discover.sh --instance <INSTANCE_ID>\n'
    exit 0
fi

section "Instance $instance"
read -r root lifecycle ami profile public private state tokens endpoint platform < <(
    ro ec2 describe-instances --instance-ids "$instance" --output text --query \
    'Reservations[0].Instances[0].[RootDeviceType,InstanceLifecycle,ImageId,IamInstanceProfile.Arn,PublicIpAddress,PrivateIpAddress,State.Name,MetadataOptions.HttpTokens,MetadataOptions.HttpEndpoint,Platform]'
) || { echo "Cannot describe $instance in $region." >&2; exit 1; }
image=$(ro ec2 describe-images --image-ids "$ami" --query 'Images[0].Name' --output text 2>/dev/null)
printf 'State %s, root %s, image %s\n' "$state" "$root" "${image:-unknown}"
printf 'Public IP %s, private IP %s, metadata endpoint %s (tokens %s)\n' \
    "$public" "$private" "$endpoint" "$tokens"

[[ $root == ebs ]] || note "UNSUPPORTED: root device is $root. Only EBS-backed instances can stop."
[[ ${platform,,} == windows ]] && note "UNSUPPORTED: Windows instance. The idle timer needs Linux with systemd."
[[ $lifecycle == spot ]] && note "CHECK: Spot Instance. Only persistent Spot requests can be stopped."
[[ $endpoint == disabled ]] && note "FIX: instance metadata is disabled. The watcher needs IMDS enabled."

case ${image,,} in
    *ubuntu*) user=ubuntu ;;
    *al2023* | *amzn* | *amazon*) user=ec2-user ;;
    *debian*) user=admin ;;
    *) user=ubuntu; note "CHECK: could not tell the OS from the image name. Confirm SshUser." ;;
esac

role=""
if [[ $profile != None ]]; then
    role=$(aws --no-cli-pager iam get-instance-profile --instance-profile-name "${profile##*/}" \
        --query 'InstanceProfile.Roles[0].RoleName' --output text 2>/dev/null)
    [[ $role == None ]] && role=""
    echo "Instance profile ${profile##*/}, role ${role:-none}"
else
    echo "No instance profile attached. The stack can create one."
fi

eip=$(ro ec2 describe-addresses --filters "Name=instance-id,Values=$instance" \
    --query 'Addresses[0].PublicIp' --output text 2>/dev/null)
[[ $eip == None ]] && eip=""
echo "Elastic IP: ${eip:-none}"
[[ -n $eip ]] && note "KEEP: the instance already has Elastic IP $eip. Keep it, and don't allocate another."

section "SSH access (inbound rules that cover port 22)"
read -r -a groups < <(ro ec2 describe-instances --instance-ids "$instance" --output text \
    --query 'Reservations[0].Instances[0].SecurityGroups[].GroupId')
rules=""
if ((${#groups[@]})); then
    rules=$(ro ec2 describe-security-groups --group-ids "${groups[@]}" --output text --query \
        'SecurityGroups[].IpPermissions[?IpProtocol==`-1` || (FromPort<=`22` && ToPort>=`22`)][].[IpProtocol,join(`,`,IpRanges[].CidrIp),join(`,`,Ipv6Ranges[].CidrIpv6),join(`,`,PrefixListIds[].PrefixListId),join(`,`,UserIdGroupPairs[].GroupId)]')
fi
ic_list=$(ro ec2 describe-managed-prefix-lists --output text --query 'PrefixLists[0].PrefixListId' \
    --filters "Name=prefix-list-name,Values=com.amazonaws.$region.ec2-instance-connect" 2>/dev/null)
endpoints=$(ro ec2 describe-instance-connect-endpoints --output text 2>/dev/null \
    --query 'InstanceConnectEndpoints[?State==`create-complete`].InstanceConnectEndpointId')
external=0
if [[ -z $rules ]]; then
    echo "No inbound rule covers port 22."
else
    # awk keeps empty tab-separated fields (bash read would collapse them).
    awk -F'\t' 'BEGIN { printf "%-6s %-22s %-14s %-24s %s\n", "PROTO", "IPV4", "IPV6", "PREFIX_LISTS", "FROM_GROUPS" }
        { for (i = 2; i <= 5; i++) if ($i == "" || $i == "None") $i = "-"
          printf "%-6s %-22s %-14s %-24s %s\n", $1, $2, $3, $4, $5 }' <<< "$rules"
    awk -F'\t' '($2 != "" && $2 != "None") || ($3 != "" && $3 != "None") || ($4 != "" && $4 != "None") { found = 1 }
        END { exit !found }' <<< "$rules" && external=1
fi
echo "Instance Connect Endpoints: ${endpoints:-none}"
grep -Eq '0\.0\.0\.0/0|::/0' <<< "$rules" \
    && note "RECOMMEND: port 22 is open to the whole internet. Limit it to your IP if you can."
if ((!external)) && [[ -z $endpoints || $endpoints == None ]]; then
    note "CHECK: no SSH route from outside. Browser EC2 Instance Connect needs an inbound port 22 rule for prefix list ${ic_list:-com.amazonaws.$region.ec2-instance-connect}. Or create an Instance Connect Endpoint, or use Session Manager. The installer must run on the instance."
fi

section "EBS volumes"
ro ec2 describe-volumes --filters "Name=attachment.instance-id,Values=$instance" --output table \
    --query 'Volumes[].{Volume:VolumeId,GiB:Size,Type:VolumeType,Encrypted:Encrypted,KmsKey:KmsKeyId}'
kms_key=""
for key in $(ro ec2 describe-volumes --filters "Name=attachment.instance-id,Values=$instance" \
    --query 'Volumes[?KmsKeyId].KmsKeyId' --output text); do
    manager=$(ro kms describe-key --key-id "$key" --query 'KeyMetadata.KeyManager' \
        --output text 2>/dev/null) || manager=UNKNOWN
    if [[ $manager != AWS ]]; then
        kms_key=$key
        note "SET: EBS uses a customer managed KMS key. Pass EbsKmsKeyArn=$key."
    fi
done

section "Lambda concurrency quota"
limit=$(ro lambda get-account-settings --query 'AccountLimit.ConcurrentExecutions' \
    --output text 2>/dev/null) || limit=unknown
echo "Account concurrency limit: $limit  (rule: 100 or less -> WakeMaxConcurrency=0; above 100 -> 3 is a good optional cap)"
concurrency=0
[[ $limit =~ ^[0-9]+$ ]] && ((limit > 100)) && concurrency=3
[[ -n $role ]] && note "NOTE: the stack will add its policy (stop this instance, send its warning email) to your existing role $role."
[[ $status != "not deployed" ]] && note "CHECK: stack '$stack' already exists ($status). Update it, or choose another --stack name."
if [[ -n $eip ]]; then ip_choice="keep Elastic IP $eip (already attached)"; else ip_choice="changing IP, or allocate an Elastic IP"; fi

section "Suggested stack parameters"
cat <<EOF
InstanceId=$instance
InstanceRoleName=${role}
SshUser=$user
EbsKmsKeyArn=${kms_key}
WakeMaxConcurrency=$concurrency
WakeKeySha256=<FROM_SETUP_STEP_1>
NotificationEmail=<YOUR_EMAIL_OR_EMPTY>
WorkHoursStart=<EMPTY_OR_CRON>   ScheduleTimezone=<IANA_TIME_ZONE>
IP choice: $ip_choice
EOF
[[ -n $role ]] || echo "(InstanceRoleName empty: the stack creates a role; run its AttachRoleCommand output.)"

section "Findings"
if ((${#notes[@]})); then printf -- '- %s\n' "${notes[@]}"; else echo "- No blockers found."; fi
echo
echo "Nothing was changed. Keep this output outside your Git repository."
