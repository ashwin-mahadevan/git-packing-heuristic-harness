#!/bin/bash
#
# Idempotent build script.
#
# Usage: harness/build.sh
#
# Steps:
#   1. Ensure git submodule is checked out.
#   2. Build git with DEVELOPER=1.
#
# The binary ends up at git/bin-wrappers/git (usable in-tree)
# or can be referenced as GIT_SRC/git.
#
set -euo pipefail

HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GIT_SRC="$HARNESS_ROOT/git"

echo "=== Checking git submodule ==="
if [ ! -f "$GIT_SRC/Makefile" ]; then
    echo "Git submodule not initialized. Running git submodule update..."
    git -C "$HARNESS_ROOT" submodule update --init --recursive
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

echo "=== Building delta-oracle helper ==="
# Compile the oracle by having git's Makefile do a one-off compilation.
# This reuses the exact CFLAGS/LDFLAGS that git itself was built with.
cat > "$GIT_SRC/.build-oracle.mak" <<'ORACLE_MAK'
include Makefile
delta-oracle: $(HARNESS_ROOT)/helpers/delta-oracle.c common-main.o libgit.a xdiff/lib.a reftable/libreftable.a
	$(QUIET_LINK)$(CC) $(ALL_CFLAGS) -o $(HARNESS_ROOT)/helpers/delta-oracle \
		$(HARNESS_ROOT)/helpers/delta-oracle.c \
		common-main.o libgit.a xdiff/lib.a reftable/libreftable.a \
		$(LIBS) $(EXTLIBS)
ORACLE_MAK
make -C "$GIT_SRC" -f .build-oracle.mak delta-oracle \
    HARNESS_ROOT="$HARNESS_ROOT" \
    NO_OPENSSL=1 NO_CURL=1 NO_EXPAT=1 NO_GETTEXT=1 NO_PERL=1 NO_PYTHON=1 NO_TCLTK=1 \
    2>&1 | tail -5
rm -f "$GIT_SRC/.build-oracle.mak"

echo ""
echo "=== Build complete ==="
echo "Git binary:         $GIT_SRC/git"
echo "Delta oracle:       $HARNESS_ROOT/helpers/delta-oracle"
"$GIT_SRC/git" --version
