#!/usr/bin/env bash
# Pack the authenticated WebCargo browser profile for a one-time import into
# the worker's Railway volume.
#
#   deploy/railway/pack-browser-profile.sh [profile-dir]
#
# Default profile-dir: .browser/webcargo-profile (TRANSLOG_WEBCARGO__USER_DATA_DIR).
# Caches are left out (they are hundreds of MB and rebuilt on first use);
# Chromium's Singleton* lock files are left out too. Output:
# .secrets/railway/webcargo-profile.tar (mode 0600) — it holds the session
# cookies, so treat it as a credential.
#
# Upload it to the volume ROOT of the worker service (the entrypoint imports
# /data/webcargo-profile.tar on the next start, only if no profile exists yet):
#   railway volume files upload .secrets/railway/webcargo-profile.tar /webcargo-profile.tar
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

profile="${1:-.browser/webcargo-profile}"
[ -d "$profile" ] || { echo "no profile directory at $profile" >&2; exit 2; }
# TRANSLOG_PACK_OUT_DIR overrides the output directory (tests, or a different
# secret store); the default stays git-ignored and docker-ignored.
out_dir="${TRANSLOG_PACK_OUT_DIR:-.secrets/railway}"
out="$out_dir/webcargo-profile.tar"
mkdir -p "$out_dir"
umask 077
tar -cf "$out" -C "$profile" \
  --exclude='./Default/Cache' --exclude='./Default/Code Cache' --exclude='./Default/GPUCache' \
  --exclude='./Default/Service Worker/CacheStorage' --exclude='./Default/DawnCache' \
  --exclude='./GrShaderCache' --exclude='./ShaderCache' --exclude='./GraphiteDawnCache' \
  --exclude='./SingletonLock' --exclude='./SingletonSocket' --exclude='./SingletonCookie' \
  .
chmod 600 "$out"
echo "packed $profile -> $out ($(du -h "$out" | cut -f1))"
