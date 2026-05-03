#!/bin/bash
#
# Idempotent build script.
#
# Usage: harness/build.sh
#
# Clones upstream git, applies the delta-strategy patch, and builds.
# The binary ends up at git/git.
#
set -euo pipefail

HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GIT_SRC="$HARNESS_ROOT/git"
GIT_TAG="v2.47.0"
PATCH_FILE="$HARNESS_ROOT/patch/delta-strategy.patch"

if [ ! -d "$GIT_SRC" ]; then
    echo "=== Cloning git at $GIT_TAG ==="
    git clone --branch "$GIT_TAG" --depth 1 \
        https://github.com/git/git.git "$GIT_SRC"

    echo "=== Applying delta-strategy patch ==="
    git -C "$GIT_SRC" apply "$PATCH_FILE"
fi

echo "=== Building git ==="
make -C "$GIT_SRC" -j"$(nproc)" \
    DEVELOPER=1 \
    NO_OPENSSL=1 \
    NO_CURL=1 \
    NO_EXPAT=1 \
    NO_GETTEXT=1 \
    NO_PERL=1 \
    NO_PYTHON=1 \
    NO_TCLTK=1 \
    2>&1 | tail -5

echo ""
echo "=== Build complete ==="
echo "Git binary:         $GIT_SRC/git"
"$GIT_SRC/git" --version
