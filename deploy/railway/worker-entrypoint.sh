#!/usr/bin/env bash
# Entrypoint for deploy/railway/Dockerfile.worker — the systemd unit's job,
# done by a shell script that is PID 1 in the container.
#
# Modes (selected by TRANSLOG_WORKER_LOGIN):
#   unset/0  normal: Xvfb on :99, then `python -m translog_quote.interface.worker`.
#   1        login:  the same worker started with --login, its display shared
#            through x11vnc + noVNC on $PORT so an operator can sign in to
#            WebCargo from a browser, and its stdin fed from a FIFO so the
#            "press Enter to continue" arrives via `railway ssh`. After the
#            sign-in the very same process serves jobs on that session.
#
# Exit-code contract (from interface/worker/main.py and the systemd unit):
#   0   clean stop                   -> exit 0 (Railway ON_FAILURE leaves it)
#   78  needs operator sign-in       -> PARK: never restart-loop; the operator
#                                      runs login mode. (systemd:
#                                      RestartPreventExitStatus=78)
#   75  WebCargo unreachable / other -> wait out the single-worker lock TTL
#                                      (systemd: RestartSec=130 > 120 s lease),
#                                      then exit non-zero so Railway restarts.
#
# Signals: Railway stops a deployment with SIGTERM. RQ treats SIGTERM like
# SIGINT (warm shutdown: finish/requeue, close Chromium, RELEASE THE LOCK), so
# this script forwards it to the Python child instead of swallowing it as PID 1.
# Pair with deploy.drainingSeconds=200 (the unit's TimeoutStopSec).
set -euo pipefail

log() { printf 'worker-entrypoint: %s\n' "$*" >&2; }

PROFILE_DIR="${TRANSLOG_WEBCARGO__USER_DATA_DIR:-/data/webcargo-profile}"
VOLUME_ROOT="$(dirname "$PROFILE_DIR")"
PROFILE_TAR="$VOLUME_ROOT/webcargo-profile.tar"
LOCK_TTL_WAIT="${TRANSLOG_RESTART_WAIT_SECONDS:-130}"
DISPLAY_NUMBER="${TRANSLOG_XVFB_DISPLAY:-99}"
RUN_AS="${TRANSLOG_RUN_AS:-translog}"
# The login prompt reads its "Enter" from this FIFO. It lives in the runtime
# user's home, not /tmp: with fs.protected_fifos set, a sticky world-writable
# directory refuses even root's `echo > fifo` on a FIFO owned by another user.
FIFO="${HOME:-/tmp}/translog-login.fifo"

if [ "$(id -u)" = "0" ]; then
  # Railway mounts the volume root-owned. Hand it (and an uploaded profile
  # tarball, if any) to the runtime user, then drop privileges for good.
  mkdir -p "$VOLUME_ROOT"
  chown "$RUN_AS:$RUN_AS" "$VOLUME_ROOT"
  [ -f "$PROFILE_TAR" ] && chown "$RUN_AS:$RUN_AS" "$PROFILE_TAR"
  # setpriv changes the ids but not the environment. Chromium's crashpad
  # handler needs a writable HOME ("chrome_crashpad_handler: --database is
  # required" and SIGTRAP otherwise), so set it to the runtime user's home.
  RUN_AS_HOME="$(getent passwd "$RUN_AS" | cut -d: -f6)"
  exec setpriv --reuid="$RUN_AS" --regid="$RUN_AS" --init-groups \
    env HOME="${RUN_AS_HOME:-/tmp}" USER="$RUN_AS" LOGNAME="$RUN_AS" "$0" "$@"
fi

# One-time profile import: an authenticated profile packed on the laptop by
# deploy/railway/pack-browser-profile.sh and uploaded to the volume root with
# `railway volume files upload`. Only when no profile exists yet — an existing
# session is never overwritten by an old tarball.
if [ -f "$PROFILE_TAR" ] && [ ! -d "$PROFILE_DIR" ]; then
  mkdir -p "$PROFILE_DIR"
  tar -xf "$PROFILE_TAR" -C "$PROFILE_DIR"
  mv "$PROFILE_TAR" "$PROFILE_TAR.imported"
  log "imported browser profile from $PROFILE_TAR into $PROFILE_DIR"
fi
# Chromium refuses a profile whose previous owner died without cleaning up.
rm -f "$PROFILE_DIR/SingletonLock" "$PROFILE_DIR/SingletonSocket" "$PROFILE_DIR/SingletonCookie" 2>/dev/null || true

# --- virtual display ---------------------------------------------------------
export DISPLAY=":$DISPLAY_NUMBER"
# Railway restarts this service in the same container, and /tmp survives the
# restart. A display left by the previous run (killed before it could tidy up)
# leaves /tmp/.X99-lock naming a PID that the new run may well reuse — Xvfb then
# refuses with "server already active" and exits, while the stale socket below
# still exists and would pass the readiness check. This container runs exactly
# one Xvfb, started here, so anything already there is stale: remove it.
rm -f "/tmp/.X${DISPLAY_NUMBER}-lock" "/tmp/.X11-unix/X$DISPLAY_NUMBER"
Xvfb "$DISPLAY" -screen 0 1280x720x24 -nolisten tcp >/dev/null 2>&1 &
XVFB_PID=$!
for _ in $(seq 1 50); do
  [ -e "/tmp/.X11-unix/X$DISPLAY_NUMBER" ] && break
  kill -0 "$XVFB_PID" 2>/dev/null || break
  sleep 0.1
done
# Ready means the socket exists AND the server that made it is still running.
if [ ! -e "/tmp/.X11-unix/X$DISPLAY_NUMBER" ] || ! kill -0 "$XVFB_PID" 2>/dev/null; then
  log "Xvfb did not come up on $DISPLAY"
  exit 1
fi

CHILD=""
STOP_REQUESTED=""
cleanup() {
  [ -n "$CHILD" ] && kill -TERM "$CHILD" 2>/dev/null || true
  kill -TERM "$XVFB_PID" 2>/dev/null || true
  # Give Xvfb a moment to remove its own lock and socket before the container
  # stops; the start-up cleanup above covers the case where it cannot.
  for _ in $(seq 1 20); do
    kill -0 "$XVFB_PID" 2>/dev/null || break
    sleep 0.1
  done
}
forward() {
  # SIGTERM/SIGINT from Railway -> the Python worker's warm shutdown. The flag
  # also ends the park loop and skips the restart wait: a stop is a stop.
  STOP_REQUESTED=1
  [ -n "$CHILD" ] && kill -TERM "$CHILD" 2>/dev/null || true
}
trap forward TERM INT
trap cleanup EXIT

# Run the worker as a child (so this PID 1 can forward signals and read the
# exit code). `wait` returns early when a trapped signal arrives; loop until
# the child is really gone.
run_worker() {
  # `<&0` is an explicit stdin redirection: without one, bash gives a
  # background job /dev/null as stdin, and the login prompt would see EOF.
  "$@" <&0 &
  CHILD=$!
  local rc=0
  while kill -0 "$CHILD" 2>/dev/null; do
    wait "$CHILD" && rc=0 || rc=$?
  done
  CHILD=""
  return "$rc"
}

interruptible_sleep() {
  sleep "$1" &
  CHILD=$!
  wait "$CHILD" || true
  CHILD=""
}

if [ "${TRANSLOG_WORKER_LOGIN:-0}" = "1" ]; then
  # --- login mode ------------------------------------------------------------
  : "${TRANSLOG_VNC_PASSWORD:?login mode needs TRANSLOG_VNC_PASSWORD}"
  PORT="${PORT:-8080}"
  x11vnc -storepasswd "$TRANSLOG_VNC_PASSWORD" /tmp/vncpasswd >/dev/null 2>&1
  x11vnc -display "$DISPLAY" -localhost -rfbport 5900 -rfbauth /tmp/vncpasswd \
    -forever -shared -quiet -bg >/dev/null 2>&1
  websockify --web /usr/share/novnc "0.0.0.0:$PORT" localhost:5900 >/dev/null 2>&1 &
  rm -f "$FIFO"; mkfifo "$FIFO"
  exec 3<>"$FIFO"
  log "LOGIN MODE: open https://<this service's Railway domain>/vnc.html, enter the VNC password, sign in to WebCargo until the Search & Book form is visible,"
  log "LOGIN MODE: then open a shell with 'railway ssh -s <worker service>' and run:  echo > $FIFO"
  log "LOGIN MODE: afterwards remove TRANSLOG_WORKER_LOGIN and the public domain, and redeploy."
  set +e
  run_worker python -m translog_quote.interface.worker --login <&3
  rc=$?
  set -e
else
  # --- normal mode -----------------------------------------------------------
  set +e
  run_worker python -m translog_quote.interface.worker
  rc=$?
  set -e
fi

case "$rc" in
  0)
    log "worker stopped cleanly"
    exit 0
    ;;
  78)
    log "worker exited 78: the WebCargo session needs an operator sign-in (or the mode is refused)."
    log "PARKED — not restarting. Run login mode (TRANSLOG_WORKER_LOGIN=1, see docs/railway-deployment.md) and redeploy."
    while [ -z "$STOP_REQUESTED" ]; do
      interruptible_sleep 600
      [ -z "$STOP_REQUESTED" ] && log "still parked: waiting for an operator sign-in via login mode"
    done
    log "stop requested while parked; exiting"
    exit 0
    ;;
  *)
    if [ -n "$STOP_REQUESTED" ]; then
      log "worker exited $rc after a stop request; exiting now"
      exit "$rc"
    fi
    log "worker exited $rc; waiting ${LOCK_TTL_WAIT}s for the single-worker lease to lapse before Railway restarts this service"
    interruptible_sleep "$LOCK_TTL_WAIT"
    exit "$rc"
    ;;
esac
