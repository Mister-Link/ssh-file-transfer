#!/bin/bash

TD_API_KEY="RjFvVYH69O1DQmfFgEOECRF1xUnD0YFD"
TD_API_URL="https://dashboard.tensordock.com/api/v2/instances"

error_exit() { echo "❌ $1" >&2; exit 1; }

command -v jq >/dev/null 2>&1 || error_exit "jq is required but not installed."
command -v nc >/dev/null 2>&1 || error_exit "Netcat (nc) is required but not installed."
command -v vastai >/dev/null 2>&1 || error_exit "vastai CLI is required but not installed."

wait_for_status() {
  local id="$1"
  local desired_status="$2"
  local timeout="${3:-300}"
  local interval=10
  local waited=0

  echo "Waiting for host $id to become '$desired_status'..."
  while [ $waited -lt $timeout ]; do
    sleep $interval
    waited=$((waited + interval))
    STATUS=$(curl -s -X GET "$TD_API_URL/$id" -H "Authorization: Bearer $TD_API_KEY" | jq -r '.status // empty')
    if [ "$STATUS" = "$desired_status" ]; then
      echo "✅ Instance is now '$STATUS'."
      return 0
    fi
    echo "   still waiting ($waited s elapsed, current: $STATUS)..."
  done

  error_exit "Timed out waiting for instance to reach status '$desired_status'."
}

update_ssh_config() {
  local host="$1"
  local port="$2"
  local user="$3"
  local ssh_config="$HOME/.ssh/config"
  touch "$ssh_config"

  # Remove ALL Host blocks that use vast_ed25519
  awk '
    BEGIN {
      in_block=0
      block=""
      has_vast_key=0
    }

    /^Host[ \t]+/ {
      # Print previous block only if it does NOT have vast_ed25519
      if (in_block && !has_vast_key) {
        printf "%s", block
      }

      # Start new block
      in_block=1
      block = $0 "\n"
      has_vast_key=0
      next
    }

    {
      if (!in_block) {
        print $0
        next
      }

      block = block $0 "\n"

      # Check if this block uses vast_ed25519
      if ($0 ~ /IdentityFile[ \t]+.*vast_ed25519/) {
        has_vast_key=1
      }
    }

    END {
      # Handle last block - only print if it does NOT have vast_ed25519
      if (in_block && !has_vast_key) {
        printf "%s", block
      }
    }
  ' "$ssh_config" > "$ssh_config.tmp"

  # Add the new host entry at the top, then append the cleaned config
  {
    echo "Host vast-ai"
    echo "    HostName $host"
    echo "    Port $port"
    echo "    User $user"
    echo "    IdentityFile ~/.ssh/vast_ed25519"
    echo "    IdentitiesOnly yes"
    echo "    LocalForward 8080 localhost:8080"
    echo ""
    cat "$ssh_config.tmp"
  } > "$ssh_config"

  rm -f "$ssh_config.tmp"
  chmod 600 "$ssh_config" || true

  echo "🧹 Cleaned up old Vast.ai SSH entries"
}

wait_for_ssh() {
  local host="$1"
  local port="$2"
  local timeout=120

  local waited=0

  echo "⏳ Waiting for SSH service to be ready on ${host}:${port}..."

  while [ $waited -lt $timeout ]; do
    if ssh -i ~/.ssh/vast_ed25519 -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes \
           -p "$port" "user@$host" true >/dev/null 2>&1; then
      echo "✅ SSH service is ready!"
      return 0
    fi

    sleep 3
    waited=$((waited + 3))
    if [ $((waited % 15)) -eq 0 ]; then
      echo "   ... still waiting (${waited}s elapsed) ..."
    fi
  done

  echo "⚠️  SSH service didn't become ready within ${timeout}s, but will try to connect anyway..."
  return 1
}

echo "🔍 Checking Vast.ai instances..."
VAST_INSTANCES=$(vastai show instances --raw 2>/dev/null)
VAST_ID=$(echo "$VAST_INSTANCES" | jq -r '.[] | select(.actual_status == "running") | .id' | head -1)


if [ -n "$VAST_ID" ]; then
  echo "✅ Found running Vast.ai instance: $VAST_ID"

  VAST_DETAILS=$(vastai show instance "$VAST_ID" --raw)
  CPU=$(echo "$VAST_DETAILS" | jq -r '.cpu_cores_effective // .cpu_cores')
  RAM=$(echo "$VAST_DETAILS" | jq -r '.cpu_ram / 1024 | floor')
  STORAGE=$(echo "$VAST_DETAILS" | jq -r '.disk_space')
  GPU_COUNT=$(echo "$VAST_DETAILS" | jq -r '.num_gpus // 0')
  GPU_NAME=$(echo "$VAST_DETAILS" | jq -r '.gpu_name // "Unknown"')
  DPH=$(echo "$VAST_DETAILS" | jq -r '.dph_total')

  echo ""
  echo "Provider:  Vast.ai"
  echo "vCPU:      $CPU"
  echo "RAM:       ${RAM} GB"
  echo "Storage:   ${STORAGE} GB"
  if [ "$GPU_COUNT" -gt 0 ]; then
    echo "GPU:       x${GPU_COUNT} ${GPU_NAME}"
  fi
  echo "Cost:      \$${DPH}/hr"
  echo ""

  VAST_HOST=$(echo "$VAST_DETAILS" | jq -r '.public_ipaddr')
  VAST_PORT=$(echo "$VAST_DETAILS" | jq -r '.ports["22/tcp"][0].HostPort // .ssh_port')
  VAST_USER="user"

  if [ -z "$VAST_PORT" ] || [ "$VAST_PORT" = "null" ]; then
    VAST_HOST=$(echo "$VAST_DETAILS" | jq -r '.ssh_host')
    VAST_PORT=$(echo "$VAST_DETAILS" | jq -r '.ssh_port')
    VAST_USER="root"
  fi

  echo "📝 Updating SSH config..."
  update_ssh_config "$VAST_HOST" "$VAST_PORT" "$VAST_USER"
  echo "✅ SSH config updated for ${VAST_HOST}:${VAST_PORT}"
  echo ""

  wait_for_ssh "$VAST_HOST" "$VAST_PORT"
  echo ""

  echo "🔗 Connecting to ${VAST_HOST}:${VAST_PORT} as ${VAST_USER}..."

  MAX_RETRIES=30
  RETRY_COUNT=0
  while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if [ "$VAST_USER" = "root" ]; then
      if ssh -t vast-ai \
        'exec su user -s /bin/bash -c "cd /home/user && HOME=/home/user exec bash --login"'; then
        break
      fi
    else
      if ssh -t vast-ai \
        'cd /home/user && exec bash --login'; then
        break
      fi
    fi

    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ $RETRY_COUNT -lt $MAX_RETRIES ]; then
      echo "   Connection failed. Retrying in 5 seconds... (attempt $RETRY_COUNT/$MAX_RETRIES)"
      sleep 5
    else
      error_exit "Failed to establish SSH connection after $MAX_RETRIES attempts."
    fi
  done
  exit 0
fi

echo "🔍 Checking TensorDock instances..."
RESPONSE=$(curl -s -w "%{http_code}" -X GET "$TD_API_URL" -H "Authorization: Bearer $TD_API_KEY")
HTTP_CODE="${RESPONSE: -3}"
BODY="${RESPONSE::-3}"
[ "$HTTP_CODE" -ne 200 ] && error_exit "TensorDock API request failed (HTTP $HTTP_CODE)."

TD_INSTANCE_ID=$(echo "$BODY" | jq -r '.data[0].id // empty')

if [ -z "$TD_INSTANCE_ID" ]; then
  echo "⚠️  No instances found on either provider."
  exit 0
fi

DETAILS=$(curl -s -X GET "$TD_API_URL/$TD_INSTANCE_ID" -H "Authorization: Bearer $TD_API_KEY")
STATUS=$(echo "$DETAILS" | jq -r '.status // empty')
IP=$(echo "$DETAILS" | jq -r '.ipAddress // empty')
PORT=$(echo "$DETAILS" | jq -r '.portForwards[]? | select(.internal_port==22) | .external_port // empty')
CPU=$(echo "$DETAILS" | jq -r '.resources.vcpu_count // empty')
STORAGE=$(echo "$DETAILS" | jq -r '.resources.storage_gb // empty')
GPU_COUNT=$(echo "$DETAILS" | jq -r '[.resources.gpus | to_entries[]?.value.count] | add // 0')
GPU_NAME=$(echo "$DETAILS" | jq -r '.resources.gpus | to_entries | map(.key) | join(", ")')
GPU_V0NAME=$(echo "$DETAILS" | jq -r '.resources.gpus | to_entries[0].value.v0Name // "a100-sxm4-80gb"')

if [ -z "$PORT" ]; then
  PORT=22
fi

echo "✅ Found TensorDock instance: $TD_INSTANCE_ID"
echo ""
echo "Provider:  TensorDock"
echo "vCPU:      $CPU"
echo "RAM:       ${RAM} GB"
echo "Storage:   ${STORAGE} GB"
if [ "$GPU_COUNT" -gt 0 ]; then
  echo "GPU:       x${GPU_COUNT} ${GPU_NAME}"
else
  echo "⚠️ Host has no GPU."
fi
echo ""

if [ "$STATUS" != "running" ]; then
  echo "💤 Host is not online."

  if [ "$GPU_COUNT" -eq 0 ]; then
    echo -n "Type 'yes' to start, or 'gpu' to add GPU and start: "
  else
    echo -n "Type 'yes' to start: "
  fi
  read -r ACTION

  if [ "$ACTION" = "gpu" ] && [ "$GPU_COUNT" -eq 0 ]; then
    echo ""
    echo "Adding GPU..."
    PAYLOAD=$(jq -n \
      --argjson cpu "$CPU" \
      --argjson ram "$RAM" \
      --argjson disk "$STORAGE" \
      --arg name "$GPU_V0NAME" \
      '{"cpuCores": $cpu, "ramGb": $ram, "diskGb": $disk, "gpus": {"gpuV0Name": $name, "count": 1}}')
    MODIFY=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$TD_API_URL/$TD_INSTANCE_ID/modify" \
      -H "Authorization: Bearer $TD_API_KEY" -H "Content-Type: application/json" -d "$PAYLOAD")
    [ "$MODIFY" -eq 200 ] && echo "GPU modification requested." || error_exit "Failed to add GPU."
    wait_for_status "$TD_INSTANCE_ID" "stopped" 600
  elif [ "$ACTION" != "yes" ]; then
    echo "🛑 Aborted."
    exit 0
  fi

  echo ""
  echo "🚀 Starting host..."
  START=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$TD_API_URL/$TD_INSTANCE_ID/start" \
    -H "Authorization: Bearer $TD_API_KEY" -H "Content-Type: application/json")
  [ "$START" -ne 200 ] && error_exit "Failed to start instance."

  wait_for_status "$TD_INSTANCE_ID" "running" 300
else
  echo "Host is running."
fi

DETAILS=$(curl -s -X GET "$TD_API_URL/$TD_INSTANCE_ID" -H "Authorization: Bearer $TD_API_KEY")
IP=$(echo "$DETAILS" | jq -r '.ipAddress // empty')
PORT=$(echo "$DETAILS" | jq -r '.portForwards[]? | select(.internal_port==22) | .external_port // empty')

if [ -z "$PORT" ]; then
  PORT=22
fi

echo "Waiting for port $PORT on $IP ..."
while ! nc -z "$IP" "$PORT" >/dev/null 2>&1; do
  sleep 5
  echo "   ... still waiting ..."
done

echo "Port is open. Waiting for SSH service..."
wait_for_ssh "$IP" "$PORT"
echo ""

if [ "$PORT" = "22" ]; then
  echo "🔗 Connecting to ${IP} ..."
  SSH_CMD="ssh -t user@${IP}"
else
  echo "🔗 Connecting to ${IP} on port ${PORT} ..."
  SSH_CMD="ssh -t -p ${PORT} user@${IP}"
fi

MAX_RETRIES=30
RETRY_COUNT=0
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
  if $SSH_CMD 'export SHOW_COMFY=1; exec bash --rcfile <(echo '\''source ~/.bashrc
if [ "$SHOW_COMFY" = 1 ]; then
  if curl -fs -o /dev/null -w "%{http_code}" https://comfyui.rephil.us/ | grep -q 200; then
    echo -e "\n\033[1;32m✅ ComfyUI online.\033[0m\n"
  fi
fi
'\'')'; then
    break
  else
    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ $RETRY_COUNT -lt $MAX_RETRIES ]; then
      echo "   Connection failed. Retrying in 5 seconds... (attempt $RETRY_COUNT/$MAX_RETRIES)"
      sleep 5
    else
      error_exit "Failed to establish SSH connection after $MAX_RETRIES attempts."
    fi
  fi
done
