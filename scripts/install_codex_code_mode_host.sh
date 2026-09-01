#!/usr/bin/env bash
set -euo pipefail

# Codex 0.151.0 enables the out-of-process Code Mode host by default. The
# standalone CLI installer on this VM did not install that companion binary,
# so install the matching official OpenAI release asset beside `codex`.
# Codex workspace-write on Linux also requires bubblewrap for the filesystem
# sandbox. Provision it explicitly and fail before any model call if missing.
# Fail closed on a version mismatch rather than silently pairing incompatible
# binaries and wasting a model run.

if [[ "$(id -u)" -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

CODEX_BIN="${CODEX_BIN:-/usr/local/bin/codex}"
[[ -x "$CODEX_BIN" ]] || { echo "Codex CLI missing: $CODEX_BIN" >&2; exit 1; }

if ! command -v bwrap >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y --no-install-recommends bubblewrap
fi
[[ -x "$(command -v bwrap)" ]] || { echo "CODEX_SANDBOX=BLOCKED bubblewrap missing" >&2; exit 1; }
echo "CODEX_SANDBOX_BWRAP=READY path=$(command -v bwrap)"

raw_version="$($CODEX_BIN --version)"
version="$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' <<<"$raw_version" | head -n1)"
case "$version" in
  0.151.0)
    asset="codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz"
    expected_sha="332da68215f070321cb52ebe792ecce8dfd614d02ea5541309d0a5df01e14894"
    ;;
  *)
    echo "Unsupported Codex version for pinned Code Mode host: $raw_version" >&2
    echo "Update the reviewed asset digest/version mapping before upgrading Codex." >&2
    exit 1
    ;;
esac

case "$(uname -m)" in
  x86_64) ;;
  *) echo "Unsupported architecture for pinned Code Mode host: $(uname -m)" >&2; exit 1 ;;
esac

bindir="$(dirname "$CODEX_BIN")"
host="$bindir/codex-code-mode-host"
marker="/var/lib/hyperliquid-ai-team/codex-code-mode-host.version"
if [[ -x "$host" && -f "$marker" && "$(cat "$marker")" == "$version" ]]; then
  echo "CODEX_CODE_MODE_HOST=READY version=$version"
  exit 0
fi

url="https://github.com/openai/codex/releases/download/rust-v${version}/${asset}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
archive="$tmp/$asset"

curl --fail --silent --show-error --location "$url" --output "$archive"
printf '%s  %s\n' "$expected_sha" "$archive" | sha256sum --check --status || {
  echo "Codex Code Mode host checksum mismatch" >&2
  exit 1
}

# Reject path traversal/absolute members before extracting the official archive.
while IFS= read -r member; do
  [[ "$member" != /* ]] || { echo "unsafe absolute archive member: $member" >&2; exit 1; }
  [[ "/$member/" != *"/../"* ]] || { echo "unsafe archive member: $member" >&2; exit 1; }
done < <(tar -tzf "$archive")

tar -xzf "$archive" -C "$tmp"
source_bin="$(find "$tmp" -type f -name 'codex-code-mode-host*' ! -name '*.tar.gz' -perm -u+x | head -n1)"
if [[ -z "$source_bin" ]]; then
  source_bin="$(find "$tmp" -type f -name 'codex-code-mode-host*' ! -name '*.tar.gz' | head -n1)"
fi
[[ -n "$source_bin" ]] || { echo "Code Mode host binary not found in archive" >&2; exit 1; }

install -o root -g root -m 0755 "$source_bin" "$host"
install -d -o root -g root -m 0711 "$(dirname "$marker")"
printf '%s\n' "$version" > "$marker"
chmod 0644 "$marker"

[[ -x "$host" ]] || { echo "Code Mode host install failed" >&2; exit 1; }
echo "CODEX_CODE_MODE_HOST=INSTALLED version=$version sha256=$expected_sha"
