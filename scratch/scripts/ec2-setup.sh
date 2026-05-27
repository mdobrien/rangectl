#!/usr/bin/env bash
set -euo pipefail

# One-time EC2 setup: creates security group and stores config
# Run this once after configuring AWS CLI credentials (aws configure)

REGION="us-east-1"
KEY_NAME="aws"  # matches ~/.ssh/aws.pem
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG_FILE="$PROJECT_DIR/.ec2-config"

echo "=== EC2 One-Time Setup ==="
echo "Region: $REGION"
echo "Key pair name: $KEY_NAME"

# Verify credentials
if ! aws sts get-caller-identity &>/dev/null; then
    echo "ERROR: AWS credentials not configured. Run: aws configure"
    exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "Account: $ACCOUNT_ID"

# Create security group
SG_NAME="rangectl-ec2"
EXISTING_SG=$(aws ec2 describe-security-groups \
    --region "$REGION" \
    --filters "Name=group-name,Values=$SG_NAME" \
    --query "SecurityGroups[0].GroupId" \
    --output text 2>/dev/null || echo "None")

if [ "$EXISTING_SG" = "None" ] || [ -z "$EXISTING_SG" ]; then
    echo "Creating security group: $SG_NAME"
    SG_ID=$(aws ec2 create-security-group \
        --region "$REGION" \
        --group-name "$SG_NAME" \
        --description "rangectl EC2 access" \
        --query GroupId --output text)

    # Allow SSH from anywhere (restrict later if needed)
    aws ec2 authorize-security-group-ingress \
        --region "$REGION" \
        --group-id "$SG_ID" \
        --protocol tcp --port 22 --cidr 0.0.0.0/0
    echo "Security group created: $SG_ID"
else
    SG_ID="$EXISTING_SG"
    echo "Security group exists: $SG_ID"
fi

# Verify key pair exists in AWS
if ! aws ec2 describe-key-pairs --region "$REGION" --key-names "$KEY_NAME" &>/dev/null; then
    echo ""
    echo "WARNING: Key pair '$KEY_NAME' not found in region $REGION."
    echo "You need to import your existing key:"
    echo "  aws ec2 import-key-pair --region $REGION --key-name $KEY_NAME --public-key-material fileb://~/.ssh/aws.pub"
    echo ""
    echo "If you don't have aws.pub, generate it from the .pem:"
    echo "  ssh-keygen -y -f ~/.ssh/aws.pem > ~/.ssh/aws.pub"
    echo ""
fi

# Find latest Ubuntu Pro 22.04 AMI
AMI_ID=$(aws ec2 describe-images \
    --region "$REGION" \
    --owners 099720109477 \
    --filters "Name=name,Values=*ubuntu-pro*22.04*amd64*pro-server-*" \
              "Name=state,Values=available" \
    --query "sort_by(Images, &CreationDate)[-1].ImageId" \
    --output text)
echo "Ubuntu Pro 22.04 AMI: $AMI_ID"

# Write config
cat > "$CONFIG_FILE" <<EOF
REGION=$REGION
KEY_NAME=$KEY_NAME
KEY_FILE=~/.ssh/aws.pem
SG_ID=$SG_ID
AMI_ID=$AMI_ID
INSTANCE_TYPE=c5.4xlarge
VOLUME_SIZE=100
SSH_USER=ubuntu
EOF

echo ""
echo "Config written to: $CONFIG_FILE"
echo "Setup complete. You can now use scratch/scripts/ec2.sh"
