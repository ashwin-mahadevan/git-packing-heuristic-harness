#!/bin/bash
#
# Idempotent build script for the patched git.
#
# Usage: harness/build.sh
#
# Steps:
#   1. Ensure git submodule is checked out at the pinned tag.
#   2. Apply the delta-strategy patch (idempotent).
#   3. Build git with DEVELOPER=1.
#
# The patched binary ends up at git/bin-wrappers/git (usable in-tree)
# or can be referenced as GIT_SRC/git.
#
set -euo pipefail

HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GIT_SRC="$HARNESS_ROOT/git"
PATCH_SCRIPT="$HARNESS_ROOT/patches/apply-delta-strategy.sh"

echo "=== Checking git submodule ==="
if [ ! -f "$GIT_SRC/Makefile" ]; then
    echo "Git submodule not initialized. Running git submodule update..."
    git -C "$HARNESS_ROOT" submodule update --init --recursive
fi

echo "=== Applying delta-strategy patch ==="
bash "$PATCH_SCRIPT" "$GIT_SRC"

echo "=== Building patched git ==="
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

echo "=== Building delta-oracle helper ==="
CFLAGS_FILE="$GIT_SRC/.cflags-for-oracle"
# Extract the BASIC_CFLAGS that git's Makefile computed
make -C "$GIT_SRC" -n -p NO_OPENSSL=1 NO_CURL=1 NO_EXPAT=1 NO_GETTEXT=1 NO_PERL=1 NO_PYTHON=1 NO_TCLTK=1 2>/dev/null \
    | grep '^BASIC_CFLAGS =' | head -1 | sed 's/^BASIC_CFLAGS = //' > "$CFLAGS_FILE"

BASIC_CFLAGS=$(cat "$CFLAGS_FILE")
cc -g -O2 $BASIC_CFLAGS \
    -o "$HARNESS_ROOT/helpers/delta-oracle" \
    "$HARNESS_ROOT/helpers/delta-oracle.c" \
    "$GIT_SRC/common-main.o" \
    "$GIT_SRC/libgit.a" \
    "$GIT_SRC/xdiff/lib.a" \
    "$GIT_SRC/reftable/libreftable.a" \
    -lz -lpthread -lrt 2>&1 | tail -5
rm -f "$CFLAGS_FILE"

echo ""
echo "=== Build complete ==="
echo "Patched git binary: $GIT_SRC/git"
echo "Delta oracle:       $HARNESS_ROOT/helpers/delta-oracle"
"$GIT_SRC/git" --version
