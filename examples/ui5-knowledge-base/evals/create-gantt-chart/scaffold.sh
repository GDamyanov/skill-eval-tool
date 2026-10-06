#!/usr/bin/env bash
# Copies repos/webcomponents/packages/main (excluding node_modules) into the sandbox CWD.
set -euo pipefail

REPO_ROOT="/Users/I554627/Projects/repos/webcomponents"
PACKAGES_SRC="$REPO_ROOT/packages/main"

if [ ! -d "$PACKAGES_SRC" ]; then
  echo "scaffold: source not found at $PACKAGES_SRC, skipping" >&2
  exit 0
fi

rsync -a --exclude='node_modules/' "$PACKAGES_SRC/" packages/main/
