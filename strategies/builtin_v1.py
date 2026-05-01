#!/usr/bin/env python3
"""
builtin strategy: reimplements git's default sort+window heuristic in Python.

This replicates the logic of type_size_sort + find_deltas from
builtin/pack-objects.c as closely as the protocol allows.

Sort order (matching type_size_sort):
  1. type descending (commits > trees > blobs > tags by enum value)
  2. name_hash descending
  3. preferred_base descending (preferred bases sort first)
  4. size descending

Window walk (matching find_deltas):
  For each non-preferred-base entry, scan backward through the window
  of preceding entries. Pick the best candidate as parent.

Limitation: the real algorithm calls try_delta on every window candidate
and keeps whichever produces the smallest delta. This strategy can only
propose ONE parent per child, so it uses heuristics to pick the most
likely candidate:
  - Same type required (the real algorithm breaks the scan on type mismatch)
  - Prefer same name_hash (same filename → similar content)
  - Among same-name_hash candidates, prefer the one closest in size
    (smaller size difference → smaller delta)
  - Fall back to closest-size same-type entry if no name_hash match

Usage:
    git pack-objects --delta-strategy="python3 strategies/builtin.py" ...
    git pack-objects --delta-strategy="python3 strategies/builtin.py --window=10" ...
"""
import argparse
import sys

TYPE_ORDER = {"commit": 3, "tree": 2, "blob": 1, "tag": 0}


def parse_descriptors(stdin):
    entries = []
    for line in stdin:
        line = line.rstrip("\n")
        if not line:
            break
        parts = line.split()
        entries.append({
            "oid": parts[0],
            "type": parts[1],
            "size": int(parts[2]),
            "name_hash": int(parts[3], 16),
            "preferred_base": int(parts[4]),
            "reused_base": parts[5],
        })
    return entries


def sort_key(entry):
    return (
        -TYPE_ORDER.get(entry["type"], -1),
        -entry["name_hash"],
        -entry["preferred_base"],
        -entry["size"],
    )


def pick_best_parent(child, window_entries):
    """
    From the window, pick the best delta base candidate for child.

    Mirrors find_deltas window scan heuristics:
    - Must be same type
    - Prefer same name_hash (same filename groups)
    - Among matches, prefer closest size (smaller delta)
    """
    child_type = child["type"]
    child_hash = child["name_hash"]
    child_size = child["size"]

    best = None
    best_score = None

    for entry in window_entries:
        if entry["type"] != child_type:
            continue

        same_hash = entry["name_hash"] == child_hash
        size_diff = abs(entry["size"] - child_size)

        # Score: (same_hash priority, -size_diff)
        # Higher is better: same_hash entries always beat different-hash
        score = (same_hash, -size_diff)

        if best_score is None or score > best_score:
            best = entry
            best_score = score

    return best


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=10)
    args = parser.parse_args()

    window_size = args.window
    entries = parse_descriptors(sys.stdin)

    # Sort matching type_size_sort
    sorted_entries = sorted(entries, key=sort_key)

    # Build assignments via sliding window
    assignments = {}
    window = []

    for entry in sorted_entries:
        oid = entry["oid"]

        if not entry["preferred_base"]:
            parent = pick_best_parent(entry, window)
            if parent is not None:
                assignments[oid] = parent["oid"]
            else:
                assignments[oid] = "NONE"

        # Add to window (preferred bases enter the window but don't get
        # assignments — they're available as delta bases only)
        window.append(entry)
        if len(window) > window_size:
            window.pop(0)

    # Emit assignments for non-preferred-base entries in input order
    for entry in entries:
        if entry["preferred_base"]:
            continue
        parent = assignments.get(entry["oid"], "NONE")
        sys.stdout.write(f"{entry['oid']} {parent}\n")

    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
