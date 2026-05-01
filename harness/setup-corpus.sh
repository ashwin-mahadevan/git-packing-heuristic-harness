#!/bin/bash
#
# Clone test repos into corpus/ with shallow depth to cap disk usage.
#
# Corpus targets:
#   1. torvalds/linux (deep-tree, low-blob) — shallow clone, ~200 commits
#   2. vuejs/core (wide-tree, many-blob) — full clone of a mid-size repo
#   3. jgm/pandoc (docs-heavy, blob-heavy)
#
# Total disk budget: ~2 GB
#
set -euo pipefail

HARNESS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CORPUS_DIR="$HARNESS_ROOT/corpus"

mkdir -p "$CORPUS_DIR"

clone_if_missing() {
    local name="$1"
    local url="$2"
    local depth="${3:-}"
    local target="$CORPUS_DIR/$name"

    if [ -d "$target/.git" ] || { [ -d "$target" ] && git -C "$target" rev-parse --git-dir >/dev/null 2>&1; }; then
        echo "=== $name: already cloned ==="
        return
    fi

    echo "=== Cloning $name ==="
    if [ -n "$depth" ]; then
        git clone --depth "$depth" --single-branch "$url" "$target"
    else
        git clone --single-branch "$url" "$target"
    fi
    echo "=== $name: done ($(du -sh "$target" | cut -f1)) ==="
}

clone_if_missing "linux"  "https://github.com/torvalds/linux.git"  200
clone_if_missing "vue"    "https://github.com/vuejs/core.git"      ""
clone_if_missing "pandoc" "https://github.com/jgm/pandoc.git"      ""

echo ""
echo "=== Corpus summary ==="
du -sh "$CORPUS_DIR"/*
