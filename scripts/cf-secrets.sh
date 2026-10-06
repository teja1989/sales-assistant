#!/usr/bin/env bash
# Create or update the Cloud Foundry user-provided service that holds secrets.
# Reads values from .env (or the file given as $1). Secrets go through a temp file
# with 0600 permissions instead of the command line, so they don't land in shell history.
set -euo pipefail

ENV_FILE="${1:-.env}"
SERVICE="${SECRETS_SERVICE_NAME:-sales-assistant-secrets}"
KEYS=(
  AZURE_OPENAI_ENDPOINT AZURE_OPENAI_API_KEY AZURE_OPENAI_DEPLOYMENT AZURE_OPENAI_API_VERSION
  LIVE_MCP_URL LIVE_MCP_TOKEN
  MCP_SERVER_TOKEN LIVE_TOOL_MAP
)

command -v cf >/dev/null || { echo "cf CLI not found" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "$ENV_FILE not found (run: make env)" >&2; exit 1; }

TMP="$(mktemp)"
chmod 600 "$TMP"
trap 'rm -f "$TMP"' EXIT

python3 - "$ENV_FILE" "$TMP" "${KEYS[@]}" <<'PY'
import json, sys
sys.path.insert(0, ".")
from app.config import _read_dotenv
from pathlib import Path
env_file, out, *keys = sys.argv[1:]
values = _read_dotenv(Path(env_file))
creds = {k: values[k] for k in keys if values.get(k)}
if not creds.get("AZURE_OPENAI_ENDPOINT"):
    print("Note: AZURE_OPENAI_ENDPOINT is empty, so the app will use the offline mock model.")
Path(out).write_text(json.dumps(creds))
print("Keys to store:", ", ".join(sorted(creds)))
PY

if cf service "$SERVICE" >/dev/null 2>&1; then
  cf update-user-provided-service "$SERVICE" -p "$TMP"
  echo "Updated $SERVICE. Run 'cf restage sales-assistant' (or cf push) to apply."
else
  cf create-user-provided-service "$SERVICE" -p "$TMP"
  echo "Created $SERVICE."
fi
