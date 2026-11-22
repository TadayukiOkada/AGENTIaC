#!/usr/bin/env bash
# Install the gitleaks release that `make publish-scan` uses (site-separation.md D6.7 item 2).
# The version and the sha256 of the release tarball are pinned here; a checksum mismatch installs nothing.
# Usage: scripts/gitleaks-install.sh [DESTINATION]   (default: .venv-lint/bin/gitleaks)
set -euo pipefail

version=8.30.1
sha256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
archive="gitleaks_${version}_linux_x64.tar.gz"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
destination="${1:-$root/.venv-lint/bin/gitleaks}"

if [ -x "$destination" ] && [ "$("$destination" version 2>/dev/null || true)" = "$version" ]; then
    echo "gitleaks $version is already installed at $destination"
    exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

curl -fsSL -o "$work/$archive" "https://github.com/gitleaks/gitleaks/releases/download/v${version}/${archive}"
if ! echo "$sha256  $work/$archive" | sha256sum --check --status -; then
    echo "gitleaks $version: the downloaded archive does not match the pinned sha256; nothing installed" >&2
    exit 1
fi

tar -xzf "$work/$archive" -C "$work" gitleaks
mkdir -p "$(dirname "$destination")"
install -m 0755 "$work/gitleaks" "$destination"
echo "installed gitleaks $version at $destination"
