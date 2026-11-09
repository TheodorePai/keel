#!/usr/bin/env bash
# These downloads run on GitHub-hosted Linux runners, outside a developer's proxy.
set -euo pipefail

tool_root="${RUNNER_TEMP:?}/keel-test-tools"
mkdir -p "$tool_root/bin"

fetch_tool() {
  local name="$1" url="$2" digest="$3" binary="$4"
  local archive="$tool_root/$name.tar.gz" unpack="$tool_root/$name"
  mkdir -p "$unpack"
  printf 'Fetching pinned integration tool: %s\n' "$name"
  curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    --connect-timeout 20 --max-time 180 \
    --retry 5 --retry-delay 15 --retry-max-time 180 \
    --header 'Accept: application/octet-stream' \
    --header 'Cache-Control: no-cache' --output "$archive" "$url"
  printf '%s  %s\n' "$digest" "$archive" | sha256sum --check --status
  printf 'Verified SHA-256: %s\n' "$name"
  tar -xzf "$archive" -C "$unpack"
  install -m 755 "$unpack/$binary" "$tool_root/bin/$(basename "$binary")"
}

# Pin immutable release asset IDs as well as content digests. The asset API avoids
# cached failures at the browser-facing release-download endpoint.
# https://github.com/gastownhall/beads/releases/tag/v1.3.2
fetch_tool beads \
  https://api.github.com/repos/gastownhall/beads/releases/assets/670845745 \
  c6ea3281f6ff82628351b2d0967c9eb85862c8a0ec0ebc24e63e645c8b02462e bd
# https://github.com/jdx/fnox/releases/tag/v1.46.0
fetch_tool fnox \
  https://api.github.com/repos/jdx/fnox/releases/assets/698005206 \
  08d2f8649f968191719938e475a8d6cdd868766a61f3fb397e8e0abbb50244dc fnox
# https://github.com/FiloSottile/age/releases/tag/v1.3.2
fetch_tool age \
  https://api.github.com/repos/FiloSottile/age/releases/assets/535541903 \
  cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10 age/age
install -m 755 "$tool_root/age/age/age-keygen" "$tool_root/bin/age-keygen"
printf '%s\n' "$tool_root/bin" >> "$GITHUB_PATH"
"$tool_root/bin/bd" --version
"$tool_root/bin/fnox" --version
"$tool_root/bin/age" --version
