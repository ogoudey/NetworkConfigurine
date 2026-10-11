#!/usr/bin/env bash
# Copy the node-side scripts to a node and open the setup TUI there.
#   ./deploy_node.sh olin@192.168.1.31            # installs into ~/netmon on the node
#   ./deploy_node.sh olin@192.168.1.31 /opt/netmon
set -euo pipefail
target="${1:?usage: $0 user@host [remote_dir]}"
dir="${2:-netmon}"
here="$(cd "$(dirname "$0")" && pwd)"

ssh "$target" "mkdir -p '$dir'"
scp -q "$here/node_server.py" "$here/node_setup.py" "$target:$dir/"
echo "Copied node scripts to $target:$dir — opening setup…"
ssh -t "$target" "cd '$dir' && python3 node_setup.py"
