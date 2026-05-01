#!/usr/bin/env python3
"""
builtin strategy: exact reimplementation of git's type_size_sort + find_deltas.

Uses the delta-oracle helper (compiled from git's own diff-delta.c) to
compute exact delta sizes, replicating try_delta's size-budget logic,
depth tracking, and window reordering.

Should produce byte-identical packs to git's default algorithm.

Usage:
    git pack-objects --delta-strategy="python3 strategies/builtin.py" ...

Requires: helpers/delta-oracle (built by harness/build.sh)
"""
import os
import subprocess
import sys

HARNESS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORACLE_BIN = os.path.join(HARNESS_ROOT, "helpers", "delta-oracle")

HASH_RAWSZ = 20  # SHA-1
DEFAULT_WINDOW = 10
DEFAULT_DEPTH = 50

TYPE_ORDER = {"tag": 4, "blob": 3, "tree": 2, "commit": 1}


class DeltaOracle:
    def __init__(self):
        self.proc = subprocess.Popen(
            [ORACLE_BIN],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def query(self, trg_oid, src_oid, max_size):
        self.proc.stdin.write(f"{trg_oid} {src_oid} {max_size}\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline().strip()
        return int(line)

    def close(self):
        self.proc.stdin.close()
        self.proc.wait()


class Entry:
    __slots__ = ("oid", "type_str", "type_val", "size", "name_hash",
                 "preferred_base", "reused_base",
                 "depth", "delta_size", "delta_parent_oid")

    def __init__(self, oid, type_str, size, name_hash, preferred_base,
                 reused_base):
        self.oid = oid
        self.type_str = type_str
        self.type_val = TYPE_ORDER.get(type_str, 0)
        self.size = size
        self.name_hash = name_hash
        self.preferred_base = preferred_base
        self.reused_base = reused_base
        self.depth = 0
        self.delta_size = 0
        self.delta_parent_oid = None

    def has_delta(self):
        return self.delta_parent_oid is not None


def sort_key(entry):
    """Matches type_size_sort: type desc, name_hash desc,
       preferred_base desc, size desc."""
    return (-entry.type_val, -entry.name_hash, -entry.preferred_base,
            -entry.size)


def try_delta_pre_checks(trg, src, max_depth):
    """Replicate try_delta's checks that don't require delta computation.
    Returns: -1 (type mismatch / break), 0 (skip), or max_size to query."""
    if trg.type_val != src.type_val:
        return -1, 0

    if src.depth >= max_depth:
        return 0, 0

    trg_size = trg.size
    src_size = src.size

    if not trg.has_delta():
        max_size = trg_size // 2 - HASH_RAWSZ
        ref_depth = 1
    else:
        max_size = trg.delta_size
        ref_depth = trg.depth

    if max_depth == ref_depth - 1:
        return 0, 0

    max_size = max_size * (max_depth - src.depth) // (max_depth - ref_depth + 1)

    if max_size == 0:
        return 0, 0

    sizediff = trg_size - src_size if src_size < trg_size else 0
    if sizediff >= max_size:
        return 0, 0

    if trg_size < src_size // 32:
        return 0, 0

    return 1, max_size


def find_deltas(entries, oracle, window_size, max_depth):
    """Replicate find_deltas from builtin/pack-objects.c."""
    window = [None] * window_size
    idx = 0
    count = 0

    for entry in entries:
        # Place entry in window slot (overwriting oldest)
        window[idx] = entry

        if entry.preferred_base:
            # Don't compute deltas for preferred bases, but they stay
            # in the window as potential sources
            idx += 1
            if count + 1 < window_size:
                count += 1
            if idx >= window_size:
                idx = 0
            continue

        best_base_idx = -1

        # Scan backward through window
        j = window_size
        while True:
            j -= 1
            if j <= 0:
                break

            other_idx = (idx + j) % window_size
            src = window[other_idx]
            if src is None:
                break

            code, max_size = try_delta_pre_checks(entry, src, max_depth)
            if code < 0:
                # type mismatch → break scan
                break
            if code == 0:
                continue

            # Ask oracle for exact delta size
            delta_size = oracle.query(entry.oid, src.oid, max_size)
            if delta_size == 0:
                continue

            # "Prefer only shallower same-sized deltas"
            if entry.has_delta():
                if (delta_size == entry.delta_size and
                        src.depth + 1 >= entry.depth):
                    continue

            # Accept this delta
            entry.delta_parent_oid = src.oid
            entry.delta_size = delta_size
            entry.depth = src.depth + 1
            best_base_idx = other_idx

        # If at max depth after deltification, evict from window
        if entry.has_delta() and max_depth <= entry.depth:
            # Don't advance idx — next entry overwrites this slot
            continue

        # Window reordering: move best_base up to stay in window longer
        if entry.has_delta() and best_base_idx >= 0:
            swap = window[best_base_idx]
            dist = (window_size + idx - best_base_idx) % window_size
            dst = best_base_idx
            while dist > 0:
                dist -= 1
                src_idx = (dst + 1) % window_size
                window[dst] = window[src_idx]
                dst = src_idx
            window[dst] = swap

        idx += 1
        if count + 1 < window_size:
            count += 1
        if idx >= window_size:
            idx = 0


def main():
    window_size = DEFAULT_WINDOW
    max_depth = DEFAULT_DEPTH

    # Parse optional args from argv (after the script name)
    i = 1
    while i < len(sys.argv):
        if sys.argv[i].startswith("--window="):
            window_size = int(sys.argv[i].split("=", 1)[1])
        elif sys.argv[i].startswith("--depth="):
            max_depth = int(sys.argv[i].split("=", 1)[1])
        i += 1

    # Read descriptors from stdin
    entries = []
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            break
        parts = line.split()
        entries.append(Entry(
            oid=parts[0],
            type_str=parts[1],
            size=int(parts[2]),
            name_hash=int(parts[3], 16),
            preferred_base=int(parts[4]),
            reused_base=parts[5],
        ))

    # Sort matching type_size_sort
    sorted_entries = sorted(entries, key=sort_key)

    oracle = DeltaOracle()
    find_deltas(sorted_entries, oracle, window_size, max_depth)
    oracle.close()

    # Build lookup from oid → entry for assignments
    entry_by_oid = {e.oid: e for e in entries}

    # Emit assignments in input order for non-preferred-base entries
    for entry in entries:
        if entry.preferred_base:
            continue
        parent = entry.delta_parent_oid or "NONE"
        sys.stdout.write(f"{entry.oid} {parent}\n")

    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
