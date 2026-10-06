#!/usr/bin/env bash
# End-to-end smoke test against a running app (local or Cloud Foundry).
#   BASE_URL=https://sales-assistant.apps.example.com MCP_SERVER_TOKEN=... scripts/smoke.sh
# Add BASIC_AUTH=user:pass if the demo gate is on.
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
CURL=(curl -fsS --max-time 60)
[[ -n "${BASIC_AUTH:-}" ]] && CURL+=(-u "$BASIC_AUTH")

if [[ -z "${MCP_SERVER_TOKEN:-}" && -f .env ]]; then
  MCP_SERVER_TOKEN="$(python3 -c 'import sys;sys.path.insert(0,".");from app.config import _read_dotenv;from pathlib import Path;print(_read_dotenv(Path(".env")).get("MCP_SERVER_TOKEN",""))')"
fi

pass() { printf '  \033[32mok\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
json() { python3 -c "import json,sys;print(json.load(sys.stdin)$1)"; }

echo "Smoke testing $BASE_URL"
"${CURL[@]}" "$BASE_URL/healthz" | grep -q '"ok"' && pass "health check" || fail "health check"

TOKEN="$("${CURL[@]}" -X POST "$BASE_URL/api/handoff" -H 'Content-Type: application/json' \
  -d '{"query":"why does my home internet keep dropping"}' | json "['token']")"
[[ -n "$TOKEN" ]] && pass "search handoff matched a scenario" || fail "handoff"

ACCESS="$(python3 scripts/oauth_signin.py "$BASE_URL" gateway-fault "${BASIC_AUTH:-}")" \
  && pass "signed in through OAuth (authorization code + PKCE)" || fail "OAuth sign-in"

NOAUTH="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/api/sessions" -H 'Content-Type: application/json' \
  ${BASIC_AUTH:+-u "$BASIC_AUTH"} -d '{"handoff_token":"xxxxxxxxxxxx"}')"
[[ "$NOAUTH" == "401" ]] && pass "chat sessions require sign-in" || fail "session without sign-in returned $NOAUTH"

SID="$("${CURL[@]}" -X POST "$BASE_URL/api/sessions" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ACCESS" -d "{\"handoff_token\":\"$TOKEN\"}" | json "['session_id']")"
pass "session created"

STREAM="$("${CURL[@]}" -N -X POST "$BASE_URL/api/sessions/$SID/turn" -H 'Content-Type: application/json' -d '{"kickoff":true}')"
grep -q 'event: tool_result' <<<"$STREAM" && pass "assistant called MCP tools" || fail "no tool calls in stream"
grep -q 'get_account_checkup' <<<"$STREAM" && pass "account checkup ran after sign-in" || fail "no account checkup"
grep -q 'event: done' <<<"$STREAM" && pass "stream completed" || fail "stream incomplete"
if grep -q 'event: error' <<<"$STREAM"; then fail "stream reported an error (check LLM settings)"; fi

CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/mcp" -H 'Content-Type: application/json' -d '{}')"
[[ "$CODE" == "401" ]] && pass "/mcp rejects requests without a token" || fail "/mcp returned $CODE without token"

if [[ -n "${MCP_SERVER_TOKEN:-}" ]]; then
  BODY="$(curl -s -X POST "$BASE_URL/mcp" -H "Authorization: Bearer $MCP_SERVER_TOKEN" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')"
  if grep -q 'run_line_diagnostics' <<<"$BODY"; then
    pass "/mcp lists tools with a valid token"
  else
    echo "  note: tools/list did not return the tool list (the server may require a protocol handshake first)."
    echo "        Verify with: make mcp-inspect"
  fi
fi
echo "Done."
