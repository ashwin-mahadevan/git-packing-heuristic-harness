# Git Packing Heuristic Harness

A test harness for experimenting with alternative delta-selection strategies in `git pack-objects`. Write a strategy in any language, plug it in, and compare the resulting pack size against git's built-in algorithm.

## Background

When git creates a packfile, it decides which objects to store as deltas of other objects. The default algorithm sorts candidates by `(type, name_hash, size)` and scans a sliding window, greedily picking the smallest delta for each object. This heuristic works well in practice but isn't necessarily optimal.

This harness extends `git pack-objects` with a `--delta-strategy=<cmd>` flag that delegates parent selection to an external process. You implement the process — git handles everything else (delta computation, pack assembly).

## Quick start

```bash
# Clone the repo (git source is included as a subtree)
git clone https://github.com/ashwin-mahadevan/git-packing-heuristic-harness.git
cd git-packing-heuristic-harness

# Install build dependencies (Ubuntu/Debian)
sudo apt-get install -y build-essential zlib1g-dev

# Build git + delta-oracle helper
bash harness/build.sh

# Create a test repo in corpus/
mkdir -p corpus/my-repo
git clone --depth 100 https://github.com/vuejs/core.git corpus/my-repo

# Run your strategy and compare against the default
python3 harness/run.py --repo corpus/my-repo                                      # default
python3 harness/run.py --repo corpus/my-repo --strategy "python3 strategies/none.py"  # no deltas
python3 harness/run.py --repo corpus/my-repo --strategy "./my_strategy"               # yours
```

## Writing a strategy

A strategy is any executable that reads object descriptors from stdin and writes parent assignments to stdout. It can be written in any language.

**Important:** Your strategy is invoked once per object type (commit, tree, blob, tag). All objects in a single invocation share the same type, so cross-type delta assignments are impossible by construction. Git trusts your assignments directly — it will not second-guess your parent choices with size budgets or depth limits. If you assign a bad parent, the resulting pack gets bigger; that's your signal to improve.

**Protocol requirements:** Your strategy must read all of stdin before writing any output. Git writes all descriptors and closes stdin before reading your stdout — a strategy that writes before consuming all input will deadlock on large repos.

### Input (git writes to your stdin)

One line per candidate object, terminated by a blank line:

```
<oid> <type> <size> <name_hash> <preferred_base> <reused_delta_base_or_NONE>
```

| Field | Description |
|-------|-------------|
| `oid` | Hex object ID (SHA-1) |
| `type` | `commit`, `tree`, `blob`, or `tag` (same for all objects in a batch) |
| `size` | Object size in bytes (decimal) |
| `name_hash` | `pack_name_hash` as 8-digit hex — objects with the same filename get the same hash |
| `preferred_base` | `1` if this object is only available as a potential base (don't assign it as a child), `0` otherwise |
| `reused_delta_base_or_NONE` | If a reused on-disk delta already exists, its base OID; else `NONE` |

### Output (you write to stdout)

One line per non-preferred-base object you received, terminated by a blank line:

```
<child_oid> <parent_oid_or_NONE>
```

- Emit exactly one line for each input object where `preferred_base` is `0`.
- Each `child_oid` must appear at most once. If duplicated, only the last assignment takes effect.
- `NONE` means "store this object in full" (no delta).
- `parent_oid` must be the OID of another object from the input list (or a thin-pack-eligible external base).
- Your assignments must not contain cycles. Depth is your responsibility — chains deeper than `--depth` (default 50) will be written as-is.

### Minimal example (Python)

```python
#!/usr/bin/env python3
"""Assign no deltas — equivalent to --window=0."""
import sys

entries = []
for line in sys.stdin:
    line = line.rstrip("\n")
    if not line:
        break
    parts = line.split()
    entries.append((parts[0], int(parts[4])))  # oid, preferred_base

for oid, preferred_base in entries:
    if not preferred_base:
        sys.stdout.write(f"{oid} NONE\n")

sys.stdout.write("\n")
sys.stdout.flush()
```

### Reading object content

If your strategy needs to inspect object content (to compute similarity, embeddings, etc.), call `git cat-file --batch` against the same repo. Don't pipe content through the protocol — it's designed to be cheap.

```python
import subprocess
proc = subprocess.Popen(
    ["git", "cat-file", "--batch"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE
)
proc.stdin.write(f"{oid}\n".encode())
proc.stdin.flush()
header = proc.stdout.readline()  # <oid> <type> <size>\n
content = proc.stdout.read(int(header.split()[2]))
proc.stdout.read(1)  # trailing newline
```

## Comparing results

`harness/run.py` runs a strategy against a repo and reports pack size, timing, and delta statistics:

```bash
# Default algorithm
python3 harness/run.py --repo corpus/my-repo --label default

# Your strategy
python3 harness/run.py --repo corpus/my-repo \
    --strategy "./my_strategy" \
    --label my-strategy

# Heuristic-only reimplementation of the default (no delta oracle needed)
python3 harness/run.py --repo corpus/my-repo \
    --strategy "python3 strategies/builtin_v1.py" \
    --label heuristic-approx
```

Output:

```
============================================================
  Repo:     my-repo
  Strategy: my-strategy
  Pack size: 1,234,567 bytes
  Elapsed:  2.31s
  Stats:
    delta-strategy/attempted: 4200
    delta-strategy/accepted: 4200
============================================================
```

All runs are logged to `results/runs.jsonl` for later analysis.

**Key flags:**
- `--window=N` — override the delta window size (default 10)
- `--record-file=<path>` — record the default algorithm's assignments to a file (for use with `strategies/replay.py`)

## Verifying correctness

A smaller pack is only useful if it's valid. `harness/verify.py` runs six verification layers:

```bash
# Run all layers on a single repo
python3 harness/verify.py --repo corpus/my-repo

# Run all layers across every repo in corpus/
python3 harness/verify.py --layer 6
```

| Layer | What it checks |
|-------|---------------|
| 1 | Harness git without `--delta-strategy` produces packs identical to stock git |
| 2 | Pack validity: `index-pack`, `verify-pack`, `fsck`, object-list diff |
| 3 | Bracket tests: `none` strategy matches `--window=0`; `replay` matches default |
| 4 | Determinism: 3 consecutive runs produce byte-identical packs |
| 5 | Stat reconciliation: `proposed == accepted` |
| 6 | Corpus sweep: layers 1-5 across all repos in `corpus/` |

## Included strategies

| Strategy | Description | Use case |
|----------|-------------|----------|
| `strategies/none.py` | Emits `NONE` for every object | Lower-bound bracket (no deltas) |
| `strategies/replay.py` | Replays recorded `(child, parent)` pairs | Upper-bound bracket (exact match to default) |
| `strategies/builtin.py` | Exact reimplementation of git's sort + window algorithm using the delta oracle | Reference baseline; byte-identical to default |
| `strategies/builtin_v1.py` | Heuristic-only approximation (no delta computation) | Fast approximate baseline |

## Project layout

```
├── patch/
│   └── delta-strategy.patch      # patch applied on top of upstream git
├── git/                          # cloned + patched by build.sh (not tracked)
├── harness/
│   ├── build.sh                  # idempotent: clones git, applies patch, builds
│   ├── run.py                    # (strategy × repo) → pack size + stats
│   ├── verify.py                 # 6-layer verification suite
│   └── setup-corpus.sh           # clones test repos into corpus/
├── helpers/
│   ├── delta-oracle.c            # C helper for exact delta sizes (linked to libgit.a)
│   └── delta-oracle              # compiled binary (built by build.sh, not tracked)
├── strategies/
│   ├── none.py                   # no deltas
│   ├── replay.py                 # replay recorded assignments
│   ├── builtin.py                # exact default algorithm reimplementation
│   └── builtin_v1.py             # heuristic approximation
├── corpus/                       # test repos (not tracked)
└── results/                      # run logs (not tracked)
```

## How the delta-strategy extension works

The git fork adds three flags to `git pack-objects`:

- **`--delta-strategy=<cmd>`** — replaces the sort + `ll_find_deltas` block in `prepare_pack()` with a subprocess protocol. Git groups candidates by object type, invokes `<cmd>` once per type, streams descriptors, reads back `(child, parent)` assignments, and applies them directly. Git trusts the strategy's choices — no size-budget or depth filtering is applied.

- **`--record-strategy=<file>`** — after a normal delta-finding run, dumps the `(child, parent)` pairs that were actually selected. Used by `strategies/replay.py` for fidelity testing.

- **`--include-reused`** — includes entries with reused on-disk deltas in the strategy input (normally excluded since they're free).

## Determinism

All harness runs force `--threads=1` to eliminate non-determinism from multi-threaded delta search. The `--path-walk` codepath is not used.

## Build dependencies

Ubuntu/Debian:

```bash
sudo apt-get install -y build-essential zlib1g-dev python3
```

The build disables optional git features (OpenSSL, curl, expat, gettext, Perl, Tcl/Tk) to minimize dependencies. Only `zlib` is required.
