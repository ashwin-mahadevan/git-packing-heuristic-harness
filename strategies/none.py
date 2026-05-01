#!/usr/bin/env python3
"""
none strategy: emits NONE for every child.

This is the lower-bound bracket — the resulting pack should have no deltas
at all, matching git pack-objects --window=0.
"""
import sys


def main():
    descriptors = []
    for line in sys.stdin:
        line = line.rstrip('\n')
        if not line:
            break
        parts = line.split()
        oid = parts[0]
        preferred_base = int(parts[4])
        descriptors.append((oid, preferred_base))

    for oid, preferred_base in descriptors:
        if preferred_base:
            continue
        sys.stdout.write(f"{oid} NONE\n")

    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
