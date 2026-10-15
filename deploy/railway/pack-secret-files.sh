#!/usr/bin/env bash
# Pack the files Render mounts as Secret Files into the ONE Railway variable the
# web entrypoint expects (TRANSLOG_SECRET_FILES_TGZ_B64).
#
#   deploy/railway/pack-secret-files.sh [dir ...]
#
# Default inputs: .secrets/render-accounts (account-<slot>.json with
# /etc/secrets token paths) and .secrets/accounts (<slot>_read.json,
# <slot>_send.json). Every regular file in the given directories lands at the
# top level of /etc/secrets in the container, so names must be unique.
#
# Output: .secrets/railway/secret-files.b64 (mode 0600). The value is NEVER
# printed. Set it with the CLI from that file, e.g.
#   railway variable set "TRANSLOG_SECRET_FILES_TGZ_B64=$(cat .secrets/railway/secret-files.b64)"
# or paste the file's contents into the variable in the Railway dashboard.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if [ "$#" -gt 0 ]; then dirs=("$@"); else dirs=(.secrets/render-accounts .secrets/accounts); fi
# TRANSLOG_PACK_OUT_DIR overrides the output directory (tests, or a different
# secret store); the default stays git-ignored and docker-ignored.
out_dir="${TRANSLOG_PACK_OUT_DIR:-.secrets/railway}"
out="$out_dir/secret-files.b64"
mkdir -p "$out_dir"
umask 077

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
count=0
for d in "${dirs[@]}"; do
  [ -d "$d" ] || { echo "not a directory: $d" >&2; exit 2; }
  for f in "$d"/*; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in
      *.patch|*.md|*.b64) continue ;;   # review-only files never ship
    esac
    if [ -e "$stage/$(basename "$f")" ]; then
      echo "duplicate file name across inputs: $(basename "$f")" >&2; exit 2
    fi
    cp "$f" "$stage/"
    count=$((count + 1))
  done
done
[ "$count" -gt 0 ] || { echo "no files found in: ${dirs[*]}" >&2; exit 2; }

tar -czf - -C "$stage" . | base64 -w 0 > "$out"
chmod 600 "$out"
echo "packed $count file(s) from ${dirs[*]} -> $out ($(wc -c < "$out") bytes base64)"
echo "files:"; ls -1 "$stage" | sed 's/^/  /'
