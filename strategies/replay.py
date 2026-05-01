#!/usr/bin/env python3
"""
replay strategy: reads recorded (child, parent) pairs from a file and emits them.

Usage:
    git pack-objects --delta-strategy="python3 strategies/replay.py <record-file>" ...

The record file is produced by --record-strategy=<file> on a default-algorithm run.
This strategy is the upper-bound bracket: its pack size must match the default
algorithm's pack size exactly.
"""
import sys


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <record-file>", file=sys.stderr)
        sys.exit(1)

    record_file = sys.argv[1]

    # Load recorded assignments
    recorded = {}
    with open(record_file, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            child_oid = parts[0]
            parent_oid = parts[1]
            recorded[child_oid] = parent_oid

    # Read descriptors from stdin (consume until blank line)
    descriptors = []
    for line in sys.stdin:
        line = line.rstrip('\n')
        if not line:
            break
        parts = line.split()
        oid = parts[0]
        preferred_base = int(parts[4])
        descriptors.append((oid, preferred_base))

    # Emit assignments
    for oid, preferred_base in descriptors:
        if preferred_base:
            continue
        parent = recorded.get(oid, "NONE")
        sys.stdout.write(f"{oid} {parent}\n")

    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
