#!/usr/bin/env bash
# Entrypoint for deploy/railway/Dockerfile.web.
#
# Railway has service variables but no "secret files", so the OAuth tokens and
# account configs that Render mounts under /etc/secrets arrive here as ONE
# variable, TRANSLOG_SECRET_FILES_TGZ_B64: a base64 tar.gz of that directory
# (made by deploy/railway/pack-secret-files.sh). This script, running as root
# for a moment, writes them to /etc/secrets with owner-only permissions, stages
# the account-*.json files into TRANSLOG_GMAIL__ACCOUNTS_DIR exactly as
# render.yaml's startCommand does, takes ownership of the volume mount, and
# then drops to the unprivileged `translog` user to run the server.
#
# Never prints a secret: only file names and counts are logged.
set -euo pipefail

log() { printf 'web-entrypoint: %s\n' "$*" >&2; }

SECRETS_DIR="${TRANSLOG_SECRET_FILES_DIR:-/etc/secrets}"
STATE_ROOT="${TRANSLOG_STATE_ROOT:-/var/lib/translog}"
ACCOUNTS_DIR="${TRANSLOG_GMAIL__ACCOUNTS_DIR:-}"
RUN_AS="${TRANSLOG_RUN_AS:-translog}"

if [ "$(id -u)" = "0" ]; then
  # The volume is mounted root-owned; the server must be able to write it.
  mkdir -p "$STATE_ROOT" "$SECRETS_DIR"
  chown "$RUN_AS:$RUN_AS" "$STATE_ROOT" "$SECRETS_DIR"
  chmod 700 "$SECRETS_DIR"
  # setpriv changes the ids but not the environment; a HOME the new user cannot
  # write breaks anything that keeps per-user state, so set it explicitly.
  RUN_AS_HOME="$(getent passwd "$RUN_AS" | cut -d: -f6)"
  exec setpriv --reuid="$RUN_AS" --regid="$RUN_AS" --init-groups \
    env HOME="${RUN_AS_HOME:-/tmp}" USER="$RUN_AS" LOGNAME="$RUN_AS" "$0" "$@"
fi

# 1. Secret files from the one variable. Absent variable = single-account
#    mode with token paths supplied some other way; not an error here, the
#    server reports missing credentials itself.
if [ -n "${TRANSLOG_SECRET_FILES_TGZ_B64:-}" ]; then
  umask 077
  find "$SECRETS_DIR" -mindepth 1 -maxdepth 1 -type f -delete
  printf '%s' "$TRANSLOG_SECRET_FILES_TGZ_B64" | base64 -d | tar -xzf - -C "$SECRETS_DIR"
  chmod 600 "$SECRETS_DIR"/* 2>/dev/null || true
  log "materialised $(find "$SECRETS_DIR" -mindepth 1 -maxdepth 1 -type f | wc -l) secret file(s) into $SECRETS_DIR"
  umask 022
else
  log "TRANSLOG_SECRET_FILES_TGZ_B64 is not set; no secret files materialised"
fi

# 2. Stage the account configs for multi-account mode. Same shape as
#    render.yaml: a deterministic directory holding ONLY account-*.json, never
#    the token files beside them.
if [ -n "$ACCOUNTS_DIR" ]; then
  mkdir -p "$ACCOUNTS_DIR"
  rm -f "$ACCOUNTS_DIR"/*.json
  shopt -s nullglob
  staged=0
  for f in "$SECRETS_DIR"/account-*.json; do
    cp "$f" "$ACCOUNTS_DIR/"
    staged=$((staged + 1))
  done
  shopt -u nullglob
  if [ "$staged" -eq 0 ]; then
    log "ERROR: TRANSLOG_GMAIL__ACCOUNTS_DIR=$ACCOUNTS_DIR but no account-*.json in $SECRETS_DIR; refusing to start multi-account with no accounts"
    exit 78
  fi
  log "staged $staged account file(s) into $ACCOUNTS_DIR"
fi

log "starting: $*"
exec "$@"
