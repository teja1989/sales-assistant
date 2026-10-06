#!/usr/bin/env bash
# Create or update the Cloud Foundry user-provided service that holds secrets.
# Reads values from .env (or the file given as $1). Secrets go through a temp file
# with 0600 permissions instead of the command line, so they don't land in shell history.
set -euo pipefail

ENV_FILE="${1:-.env}"
SERVICE="${SECRETS_SERVICE_NAME:-sales-assistant-secrets}"
KEYS=(
  AZURE_OPENAI_ENDPOINT AZURE_OPENAI_API_KEY AZURE_OPENAI_DEPLOYMENT AZURE_OPENAI_API_VERSION
  OPENAI_COMPAT_BASE_URL OPENAI_COMPAT_API_KEY OPENAI_COMPAT_MODEL OPENAI_COMPAT_KEY_HEADER
  MCP_SERVER_TOKEN HANDOFF_SECRET OAUTH_SIGNING_SECRET
  LIVE_MCP_URL LIVE_MCP_TOKEN LIVE_MCP_AUTH_SCHEME LIVE_TOOL_MAP
  DEMO_BASIC_AUTH_USER DEMO_BASIC_AUTH_PASSWORD
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
for required in ("MCP_SERVER_TOKEN", "HANDOFF_SECRET", "OAUTH_SIGNING_SECRET"):
    if len(creds.get(required, "")) < 32:
        sys.exit(f"{required} must be at least 32 characters (run: make env)")
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
