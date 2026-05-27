# EC2 Instance Management

Wrapper scripts for creating and managing a dev EC2 instance (c5.4xlarge: 16 vCPUs, 32GB RAM, 100GB gp3).

## Setup (one-time)

```bash
scratch/scripts/ec2-setup.sh
```

Creates a security group and writes `.ec2-config`. Requires AWS CLI credentials (`aws configure`).

## Commands

All commands via `scratch/scripts/ec2.sh`:

| Command | Description |
|---------|-------------|
| `ec2.sh create` | Launch a new instance. Waits until SSH is ready. |
| `ec2.sh stop` | Stop instance (preserves storage, no compute charges). |
| `ec2.sh start` | Restart a stopped instance. Waits until SSH is ready. |
| `ec2.sh terminate` | Delete instance and release resources. |
| `ec2.sh status` | Show instance ID, state, and IP. |
| `ec2.sh ip` | Print public IP only. |
| `ec2.sh ssh [cmd]` | Open interactive SSH or run a remote command. |
| `ec2.sh push <local> <remote>` | Copy file/directory to instance via SCP. |
| `ec2.sh pull <remote> <local>` | Copy file/directory from instance via SCP. |

## Examples

```bash
# Create instance and run a command
scratch/scripts/ec2.sh create
scratch/scripts/ec2.sh ssh "uname -a"

# Transfer files
scratch/scripts/ec2.sh push ./data /home/ubuntu/data
scratch/scripts/ec2.sh pull /home/ubuntu/results ./results

# Stop when not in use (saves money, keeps disk)
scratch/scripts/ec2.sh stop

# Resume later
scratch/scripts/ec2.sh start
```

## State files

- `.ec2-config` — region, instance type, AMI, security group (created by setup)
- `.ec2-state` — current instance ID (created by `ec2.sh create`, removed by `ec2.sh terminate`)

Both are in project root and should be gitignored.

## Cost note

c5.4xlarge costs ~$0.68/hr in us-east-1. Always `stop` or `terminate` when done. Stopped instances still pay for EBS storage (~$8/mo for 100GB gp3).
