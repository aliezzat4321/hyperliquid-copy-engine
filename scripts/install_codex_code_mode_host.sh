#!/usr/bin/env bash
set -euo pipefail

# Codex 0.151.0's standalone Linux package expects companion runtime binaries
# beside `codex`. Install the matching official OpenAI Code Mode host and bwrap
# release assets, with pinned SHA-256 digests. Fail closed on any version or
# checksum mismatch instead of weakening the sandbox or wasting a model run.

if [[ "$(id -u)" -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

CODEX_BIN="${CODEX_BIN:-/usr/local/bin/codex}"
[[ -x "$CODEX_BIN" ]] || { echo "Codex CLI missing: $CODEX_BIN" >&2; exit 1; }

raw_version="$($CODEX_BIN --version)"
version="$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' <<<"$raw_version" | head -n1)"
case "$version" in
  0.151.0)
    host_asset="codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz"
    host_sha="332da68215f070321cb52ebe792ecce8dfd614d02ea5541309d0a5df01e14894"
    bwrap_asset="bwrap-x86_64-unknown-linux-musl.tar.gz"
    bwrap_sha="139f984a1a4dfb62be5a8c495fa6330db0d2ebe1cdc909ed646c65fa0a3c10ce"
    ;;
  *)
    echo "Unsupported Codex version for pinned runtime companions: $raw_version" >&2
    echo "Update the reviewed asset digest/version mapping before upgrading Codex." >&2
    exit 1
    ;;
esac

case "$(uname -m)" in
  x86_64) ;;
  *) echo "Unsupported architecture for pinned Codex runtime: $(uname -m)" >&2; exit 1 ;;
esac

bindir="$(dirname "$CODEX_BIN")"
host="$bindir/codex-code-mode-host"
packaged_bwrap="$bindir/bwrap"
host_marker="/var/lib/hyperliquid-ai-team/codex-code-mode-host.version"
bwrap_marker="/var/lib/hyperliquid-ai-team/codex-bwrap.version"
base_url="https://github.com/openai/codex/releases/download/rust-v${version}"

install_asset() {
  local asset="$1"
  local expected_sha="$2"
  local name_glob="$3"
  local destination="$4"
  local label="$5"
  local tmp archive source_bin member
  tmp="$(mktemp -d)"
  archive="$tmp/$asset"
  curl --fail --silent --show-error --location "$base_url/$asset" --output "$archive"
  printf '%s  %s\n' "$expected_sha" "$archive" | sha256sum --check --status || {
    echo "$label checksum mismatch" >&2
    rm -rf "$tmp"
    exit 1
  }
  while IFS= read -r member; do
    [[ "$member" != /* ]] || { echo "unsafe absolute archive member: $member" >&2; rm -rf "$tmp"; exit 1; }
    [[ "/$member/" != *"/../"* ]] || { echo "unsafe archive member: $member" >&2; rm -rf "$tmp"; exit 1; }
  done < <(tar -tzf "$archive")
  tar -xzf "$archive" -C "$tmp"
  source_bin="$(find "$tmp" -type f -name "$name_glob" ! -name '*.tar.gz' -perm -u+x | head -n1)"
  if [[ -z "$source_bin" ]]; then
    source_bin="$(find "$tmp" -type f -name "$name_glob" ! -name '*.tar.gz' | head -n1)"
  fi
  [[ -n "$source_bin" ]] || { echo "$label binary not found in archive" >&2; rm -rf "$tmp"; exit 1; }
  install -o root -g root -m 0755 "$source_bin" "$destination"
  rm -rf "$tmp"
}

install -d -o root -g root -m 0711 /var/lib/hyperliquid-ai-team

if [[ ! -x "$host" || ! -f "$host_marker" || "$(cat "$host_marker" 2>/dev/null || true)" != "$version" ]]; then
  install_asset "$host_asset" "$host_sha" 'codex-code-mode-host*' "$host" 'Codex Code Mode host'
  printf '%s\n' "$version" > "$host_marker"
  chmod 0644 "$host_marker"
  echo "CODEX_CODE_MODE_HOST=INSTALLED version=$version sha256=$host_sha"
else
  echo "CODEX_CODE_MODE_HOST=READY version=$version"
fi

if [[ ! -x "$packaged_bwrap" || ! -f "$bwrap_marker" || "$(cat "$bwrap_marker" 2>/dev/null || true)" != "$version" ]]; then
  install_asset "$bwrap_asset" "$bwrap_sha" 'bwrap*' "$packaged_bwrap" 'Codex packaged bwrap'
  printf '%s\n' "$version" > "$bwrap_marker"
  chmod 0644 "$bwrap_marker"
  echo "CODEX_PACKAGED_BWRAP=INSTALLED version=$version sha256=$bwrap_sha"
else
  echo "CODEX_PACKAGED_BWRAP=READY version=$version"
fi

[[ -x "$host" ]] || { echo "Code Mode host install failed" >&2; exit 1; }
[[ -x "$packaged_bwrap" ]] || { echo "Codex packaged bwrap install failed" >&2; exit 1; }
