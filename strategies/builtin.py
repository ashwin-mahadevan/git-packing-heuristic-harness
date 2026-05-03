#!/usr/bin/env python3
"""
builtin strategy: exact reimplementation of git's type_size_sort + find_deltas.

Issues Q lines to the harness for exact delta sizes, replicating try_delta's
size-budget logic, depth tracking, and window reordering. Should produce
byte-identical packs to git's default algorithm.

Usage:
    git pack-objects --delta-strategy="python3 strategies/builtin.py" ...
"""
import sys

HASH_RAWSZ = 20
DEFAULT_WINDOW = 10
DEFAULT_DEPTH = 50

TYPE_ORDER = {"tag": 4, "blob": 3, "tree": 2, "commit": 1}


def query_delta(trg_oid, src_oid, max_size=0):
    """Issue Q and read matching R from the harness. max_size of 0 means no
    budget (always returns the actual delta size). A positive max_size lets
    the harness bail early using diff_delta's budget — matching git's default
    algorithm requires this, since diff_delta with a budget can refuse a
    delta whose final size would have fit (the budget check trips on
    transient overshoot during emission). Returns the delta size, or 0 if
    the harness reported no delta available."""
    sys.stdout.write(f"Q {trg_oid} {src_oid} {max_size}\n")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        sys.exit("strategy: unexpected EOF on stdin during Q")
    parts = line.rstrip("\n").split()
    if not parts or parts[0] != "R" or len(parts) < 3:
        sys.exit(f"strategy: expected R line, got: {line!r}")
    if parts[1] != trg_oid or parts[2] != src_oid:
        sys.exit(f"strategy: R mismatch: expected ({trg_oid},{src_oid}), got {line!r}")
    if len(parts) == 3:
        return 0
    return int(parts[3])


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
    return (-entry.type_val, -entry.name_hash, -entry.preferred_base,
            -entry.size)


def try_delta_pre_checks(trg, src, max_depth):
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


def find_deltas(entries, window_size, max_depth):
    window = [None] * window_size
    idx = 0
    count = 0

    for entry in entries:
        window[idx] = entry

        if entry.preferred_base:
            idx += 1
            if count + 1 < window_size:
                count += 1
            if idx >= window_size:
                idx = 0
            continue

        best_base_idx = -1

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
                break
            if code == 0:
                continue

            delta_size = query_delta(entry.oid, src.oid, max_size)
            if delta_size == 0:
                continue

            if entry.has_delta():
                if (delta_size == entry.delta_size and
                        src.depth + 1 >= entry.depth):
                    continue

            entry.delta_parent_oid = src.oid
            entry.delta_size = delta_size
            entry.depth = src.depth + 1
            best_base_idx = other_idx

        if entry.has_delta() and max_depth <= entry.depth:
            continue

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

    i = 1
    while i < len(sys.argv):
        if sys.argv[i].startswith("--window="):
            window_size = int(sys.argv[i].split("=", 1)[1])
        elif sys.argv[i].startswith("--depth="):
            max_depth = int(sys.argv[i].split("=", 1)[1])
        i += 1

    entries = []
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            break
        parts = line.split()
        if parts[0] != 'D':
            sys.exit(f"strategy: expected D, got: {line}")
        entries.append(Entry(
            oid=parts[1],
            type_str=parts[2],
            size=int(parts[3]),
            name_hash=int(parts[4], 16),
            preferred_base=int(parts[5]),
            reused_base=parts[6],
        ))

    sorted_entries = sorted(entries, key=sort_key)

    # Mirror C ll_find_deltas: it gets called with `window+1` slots so the
    # inner scan covers the full user-facing window of candidates.
    find_deltas(sorted_entries, window_size + 1, max_depth)

    for entry in entries:
        if entry.preferred_base:
            continue
        parent = entry.delta_parent_oid or "NONE"
        sys.stdout.write(f"A {entry.oid} {parent}\n")

    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
