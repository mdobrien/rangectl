#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG_FILE="$PROJECT_DIR/.ec2-config"
STATE_FILE="$PROJECT_DIR/.ec2-state"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "ERROR: Run scratch/scripts/ec2-setup.sh first"
    exit 1
fi

source "$CONFIG_FILE"
KEY_FILE="${KEY_FILE/#\~/$HOME}"

# Load instance state if exists
INSTANCE_ID=""
if [ -f "$STATE_FILE" ]; then
    source "$STATE_FILE"
fi

get_ip() {
    aws ec2 describe-instances \
        --region "$REGION" \
        --instance-ids "$INSTANCE_ID" \
        --query "Reservations[0].Instances[0].PublicIpAddress" \
        --output text 2>/dev/null
}

get_state() {
    aws ec2 describe-instances \
        --region "$REGION" \
        --instance-ids "$INSTANCE_ID" \
        --query "Reservations[0].Instances[0].State.Name" \
        --output text 2>/dev/null
}

wait_for_ssh() {
    local ip="$1"
    local max_attempts=30
    echo -n "Waiting for SSH"
    for i in $(seq 1 $max_attempts); do
        if ssh -i "$KEY_FILE" -o ConnectTimeout=2 -o StrictHostKeyChecking=no -o BatchMode=yes "$SSH_USER@$ip" true 2>/dev/null; then
            echo " ready"
            return 0
        fi
        echo -n "."
        sleep 2
    done
    echo " timeout"
    return 1
}

require_instance() {
    if [ -z "$INSTANCE_ID" ]; then
        echo "ERROR: No instance. Run: ec2.sh create"
        exit 1
    fi
}

cmd_create() {
    if [ -n "$INSTANCE_ID" ]; then
        local state
        state=$(get_state)
        if [ "$state" != "terminated" ]; then
            echo "Instance $INSTANCE_ID already exists (state: $state)"
            echo "Terminate it first: ec2.sh terminate"
            exit 1
        fi
    fi

    echo "Creating $INSTANCE_TYPE instance (${VOLUME_SIZE}GB)..."
    INSTANCE_ID=$(aws ec2 run-instances \
        --region "$REGION" \
        --image-id "$AMI_ID" \
        --instance-type "$INSTANCE_TYPE" \
        --key-name "$KEY_NAME" \
        --security-group-ids "$SG_ID" \
        --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$VOLUME_SIZE,VolumeType=gp3}" \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=rangectl-dev}]" \
        --query "Instances[0].InstanceId" \
        --output text)

    echo "INSTANCE_ID=$INSTANCE_ID" > "$STATE_FILE"
    echo "Instance: $INSTANCE_ID"

    echo "Waiting for instance to start..."
    aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"

    local ip
    ip=$(get_ip)
    echo "IP: $ip"
    wait_for_ssh "$ip"
    echo "Done. Connect with: ec2.sh ssh"
}

cmd_start() {
    require_instance
    echo "Starting $INSTANCE_ID..."
    aws ec2 start-instances --region "$REGION" --instance-ids "$INSTANCE_ID" > /dev/null
    aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"
    local ip
    ip=$(get_ip)
    echo "Running. IP: $ip"
    wait_for_ssh "$ip"
}

cmd_stop() {
    require_instance
    echo "Stopping $INSTANCE_ID..."
    aws ec2 stop-instances --region "$REGION" --instance-ids "$INSTANCE_ID" > /dev/null
    echo "Stopping (instance will retain storage)."
}

cmd_terminate() {
    require_instance
    echo "Terminating $INSTANCE_ID..."
    aws ec2 terminate-instances --region "$REGION" --instance-ids "$INSTANCE_ID" > /dev/null
    rm -f "$STATE_FILE"
    echo "Terminated."
}

cmd_status() {
    require_instance
    local state ip
    state=$(get_state)
    echo "Instance: $INSTANCE_ID"
    echo "State:    $state"
    if [ "$state" = "running" ]; then
        ip=$(get_ip)
        echo "IP:       $ip"
    fi
}

cmd_ip() {
    require_instance
    get_ip
}

cmd_ssh() {
    require_instance
    local ip
    ip=$(get_ip)
    if [ "$ip" = "None" ] || [ -z "$ip" ]; then
        echo "ERROR: Instance not running"
        exit 1
    fi
    if [ $# -eq 0 ]; then
        ssh -i "$KEY_FILE" -o StrictHostKeyChecking=no "$SSH_USER@$ip"
    else
        ssh -i "$KEY_FILE" -o StrictHostKeyChecking=no "$SSH_USER@$ip" "$@"
    fi
}

cmd_push() {
    require_instance
    if [ $# -lt 2 ]; then
        echo "Usage: ec2.sh push <local-path> <remote-path>"
        exit 1
    fi
    local ip
    ip=$(get_ip)
    scp -i "$KEY_FILE" -o StrictHostKeyChecking=no -r "$1" "$SSH_USER@$ip:$2"
}

cmd_pull() {
    require_instance
    if [ $# -lt 2 ]; then
        echo "Usage: ec2.sh pull <remote-path> <local-path>"
        exit 1
    fi
    local ip
    ip=$(get_ip)
    scp -i "$KEY_FILE" -o StrictHostKeyChecking=no -r "$SSH_USER@$ip:$1" "$2"
}

cmd_help() {
    cat <<EOF
Usage: ec2.sh <command> [args]

Commands:
  create              Launch a new instance
  start               Start a stopped instance
  stop                Stop the instance (keeps storage)
  terminate           Terminate and delete instance
  status              Show instance state and IP
  ip                  Print public IP
  ssh [command]       SSH into instance or run a remote command
  push <local> <rem>  Copy file/dir to instance
  pull <rem> <local>  Copy file/dir from instance
  help                Show this message
EOF
}

case "${1:-help}" in
    create)    cmd_create ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    terminate) cmd_terminate ;;
    status)    cmd_status ;;
    ip)        cmd_ip ;;
    ssh)       shift; cmd_ssh "$@" ;;
    push)      shift; cmd_push "$@" ;;
    pull)      shift; cmd_pull "$@" ;;
    help|*)    cmd_help ;;
esac
