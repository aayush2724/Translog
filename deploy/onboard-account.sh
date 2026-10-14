#!/usr/bin/env bash
# Onboard one Gmail mailbox into the Translog fleet (slots a..p).
#
#   deploy/onboard-account.sh <slot> <mailbox> [approver] [--reauth] [--no-auth]
#
#   slot      a..p; the account_id becomes account-<slot>
#   mailbox   the Gmail address this slot reads and replies from
#   approver  internal review recipient (default: the mailbox itself)
#   --reauth  re-run both OAuth consents even if the slot's token files exist
#             (after Google revokes a token: poll error "invalid_grant")
#   --no-auth write the two config files only; mint the tokens later
#
# Writes (all git-ignored; nothing is committed, nothing is deployed):
#   .secrets/accounts-config/account-<slot>.json  local run, tokens under .secrets/accounts/
#   .secrets/render-accounts/account-<slot>.json  Render, tokens under /etc/secrets/
#   .secrets/accounts/<slot>_read.json            gmail.readonly consent (sign in to <mailbox>)
#   .secrets/accounts/<slot>_send.json            gmail.send consent     (sign in to <mailbox>)
# then verifies the read token's authorised mailbox is <mailbox> and prints the
# three Secret Files to upload in the Render dashboard before a redeploy.
# See deploy/accounts.example/README.md §4.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

usage() { sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2; }

reauth=0
noauth=0
positional=()
for arg in "$@"; do
  case "$arg" in
    --reauth) reauth=1 ;;
    --no-auth) noauth=1 ;;
    -h|--help) usage ;;
    --*) echo "unknown flag: $arg" >&2; usage ;;
    *) positional+=("$arg") ;;
  esac
done
[ "${#positional[@]}" -ge 2 ] || usage
slot="${positional[0]}"
mailbox="${positional[1]}"
approver="${positional[2]:-$mailbox}"

[[ "$slot" =~ ^[a-p]$ ]] || { echo "slot must be one letter a..p, got '$slot'" >&2; exit 2; }
[[ "$mailbox" == *@* ]] || { echo "mailbox must be an email address, got '$mailbox'" >&2; exit 2; }

PY="${PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY=python
# The consent commands read the OAuth client file from settings; keep the
# project default unless the caller already points somewhere else.
export TRANSLOG_GMAIL__CLIENT_SECRET_PATH="${TRANSLOG_GMAIL__CLIENT_SECRET_PATH:-.secrets/gmail_client_secret.json}"

read_token=".secrets/accounts/${slot}_read.json"
send_token=".secrets/accounts/${slot}_send.json"
mkdir -p .secrets/accounts .secrets/accounts-config .secrets/render-accounts

# $1 = output path, $2 = read token path, $3 = send token path (as the
# consuming process will see them: relative locally, /etc/secrets on Render).
write_config() {
  cat > "$1" <<EOF
{
  "account_id": "account-${slot}",
  "address": "${mailbox}",
  "read_token_path": "$2",
  "send_token_path": "$3",
  "approver_address": "${approver}",
  "query": "in:inbox -subject:\"[TRANSLOG INTERNAL]\"",
  "enabled": true
}
EOF
}
write_config ".secrets/accounts-config/account-${slot}.json" "$read_token" "$send_token"
write_config ".secrets/render-accounts/account-${slot}.json" \
  "/etc/secrets/${slot}_read.json" "/etc/secrets/${slot}_send.json"
echo "wrote .secrets/accounts-config/account-${slot}.json and .secrets/render-accounts/account-${slot}.json"

if [ "$noauth" -eq 1 ]; then
  echo "--no-auth: tokens not minted; re-run without it when ready."
  exit 0
fi

if [ "$reauth" -eq 1 ] || [ ! -s "$read_token" ]; then
  echo ">>> read consent (gmail.readonly): sign in to ${mailbox}"
  TRANSLOG_GMAIL__TEST_ADDRESS="$mailbox" TRANSLOG_GMAIL__TOKEN_PATH="$read_token" \
    "$PY" -m translog_quote.interface.demo gmail-auth
else
  echo "read token ${read_token} exists; keeping it (use --reauth to re-mint)"
fi

if [ "$reauth" -eq 1 ] || [ ! -s "$send_token" ]; then
  echo ">>> send consent (gmail.send): sign in to ${mailbox} AGAIN"
  TRANSLOG_GMAIL__TEST_ADDRESS="$mailbox" TRANSLOG_GMAIL__SEND_ENABLED=true \
  TRANSLOG_GMAIL__SEND_TOKEN_PATH="$send_token" \
    "$PY" -m translog_quote.interface.demo gmail-auth-send
else
  echo "send token ${send_token} exists; keeping it (use --reauth to re-mint)"
fi

# The read scope can see the profile, so a consent given in the wrong mailbox
# is caught here rather than as mis-routed replies later. The send scope cannot
# read the profile; that consent is only ever as right as the sign-in was.
"$PY" - "$read_token" "$mailbox" <<'EOF'
import sys
from pathlib import Path

from translog_quote.adapters.email.gmail import HttpxGmailTransport

transport = HttpxGmailTransport(token_path=Path(sys.argv[1]), timeout_seconds=20, max_retries=1)
actual = transport.get_json("profile").get("emailAddress", "")
if actual.lower() != sys.argv[2].lower():
    sys.exit(
        f"read token is authorised for {actual!r}, not {sys.argv[2]!r}: "
        f"delete {sys.argv[1]} and re-run with --reauth"
    )
print(f"read token verified: authorised mailbox = {actual}")
EOF

cat <<EOF

Slot ${slot} ready. In the Render dashboard upload three Secret Files, then redeploy:
  account-${slot}.json  <- .secrets/render-accounts/account-${slot}.json
  ${slot}_read.json     <- ${read_token}
  ${slot}_send.json     <- ${send_token}
Local multi-account run: TRANSLOG_GMAIL__ACCOUNTS_DIR=.secrets/accounts-config
EOF
