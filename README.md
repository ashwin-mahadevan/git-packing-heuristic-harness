# Git Packing Heuristic Harness

A test harness for experimenting with alternative delta-selection strategies in `git pack-objects`. Write a strategy in any language, plug it in, and compare the resulting pack size against git's built-in algorithm.

## Background

When git creates a packfile, it decides which objects to store as deltas of other objects. The default algorithm sorts candidates by `(type, name_hash, size)` and scans a sliding window, greedily picking the smallest delta for each object. This heuristic works well in practice but isn't necessarily optimal.

This harness extends `git pack-objects` with a `--delta-strategy=<cmd>` flag that delegates parent selection to an external process. You implement the process — git handles everything else (delta computation, pack assembly).

## Quick start

```bash
git clone https://github.com/ashwin-mahadevan/git-packing-heuristic-harness.git
cd git-packing-heuristic-harness

# Install build dependencies (Ubuntu/Debian)
sudo apt-get install -y build-essential zlib1g-dev

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

A strategy is any executable that exchanges tagged line-oriented messages with the harness over stdin/stdout. The harness invokes it once per pack, with all candidate objects mixed together regardless of type. Git applies your assignments verbatim — no size-budget, depth, or type filtering. If you assign a bad parent, the resulting pack gets bigger or invalid; that's your signal to improve.

### Protocol

**Stdin** (harness → strategy), one message per line:

| Tag | Format | Meaning |
|-----|--------|---------|
| `D` | `D <oid> <type> <size> <name_hash> <preferred_base> <reused_base\|NONE>` | Candidate descriptor |
| `R` | `R <child_oid> <parent_oid> [<size>]` | Response to a `Q` query |
| (blank) | empty line | End of `D` lines (no more descriptors will arrive) |

**Stdout** (strategy → harness), one message per line:

| Tag | Format | Meaning |
|-----|--------|---------|
| `Q` | `Q <child_oid> <parent_oid> <max_size>` | Ask for the delta size of `child` against `parent` (with budget `max_size`; `0` = unlimited) |
| `A` | `A <child_oid> <parent_oid\|NONE>` | Final assignment for one non-preferred-base entry |
| (blank) | empty line | Strategy is done; no more `Q` or `A` |

#### Descriptor fields

| Field | Description |
|-------|-------------|
| `oid` | Hex object ID (SHA-1) |
| `type` | `commit`, `tree`, `blob`, or `tag` |
| `size` | Object size in bytes (decimal) |
| `name_hash` | `pack_name_hash` as 8-digit hex — objects with the same filename get the same hash |
| `preferred_base` | `1` if this object is only available as a potential base (don't assign it as a child), `0` otherwise |
| `reused_base_or_NONE` | If a reused on-disk delta already exists, its base OID; else `NONE` |

#### Ordering and obligations

- All `D` lines precede any `R` line on stdin. Within those groups, the harness does **not** guarantee that `R` responses arrive in the same order as the `Q` queries that produced them. Match them by the `<child_oid> <parent_oid>` pair embedded in each `R`.
- A successful `R` includes the delta size as the third field; a failed query (delta too big for the supplied `max_size`, or `diff_delta` failed) omits the size field.
- Emit exactly one `A` line per non-preferred-base descriptor. `A <child> NONE` means "store this object in full". A non-`NONE` parent must be the OID of another object from the input list (or a thin-pack-eligible external base).
- Each `child_oid` should appear in at most one `A` line. If duplicated, only the last assignment takes effect.
- Your assignments must not contain cycles. Depth is your responsibility — chains deeper than `--depth` (default 50) will be written as-is.
- Flush stdout after each `Q`. Python's `sys.stdout` is block-buffered when piped — without an explicit `flush()`, the harness never sees the query and you deadlock.
- Keep reading stdin while waiting for an `R`. The harness writes descriptors and `R` responses on the same pipe; if you stop reading, descriptor writes back up and the harness can stall.

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
    parts = line.split()  # parts[0] is "D"
    oid = parts[1]
    preferred_base = int(parts[5])
    entries.append((oid, preferred_base))

for oid, preferred_base in entries:
    if preferred_base:
        continue
    sys.stdout.write(f"A {oid} NONE\n")

sys.stdout.write("\n")
sys.stdout.flush()
```

### Querying delta sizes

Strategies that want to compare candidate parents by delta size can ask the harness:

```python
def query_delta(trg_oid, src_oid, max_size=0):
    sys.stdout.write(f"Q {trg_oid} {src_oid} {max_size}\n")
    sys.stdout.flush()
    parts = sys.stdin.readline().split()
    # parts: ["R", child, parent] or ["R", child, parent, size]
    assert parts[0] == "R" and parts[1] == trg_oid and parts[2] == src_oid
    return int(parts[3]) if len(parts) > 3 else 0
```

`max_size` of `0` always returns the actual delta size. A positive `max_size` lets `diff_delta` bail early — useful for replicating git's default algorithm exactly, which uses a per-iteration size budget. (Note: `diff_delta` with a budget can refuse a delta whose final size would have fit, because the budget check trips on transient overshoot during emission. With `max_size=0` you always get the exact size.)

The harness caches each computed delta on the entry — if your final `A` line picks the smallest parent you queried for that child, the pack-write step reuses the cached delta instead of recomputing.

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

`harness/run.py` runs a strategy against a repo and reports pack size and timing:

```bash
# Default algorithm
python3 harness/run.py --repo corpus/my-repo --label default

# Your strategy
python3 harness/run.py --repo corpus/my-repo \
    --strategy "./my_strategy" \
    --label my-strategy
```

Output:

```
============================================================
  Repo:     my-repo
  Strategy: my-strategy
  Pack size: 1,234,567 bytes
  Elapsed:  2.31s
============================================================
```

All runs are logged to `results/runs.jsonl` for later analysis.

**Key flags:**
- `--window=N` — override the delta window size (default 10)
- `--record-file=<path>` — record the default algorithm's assignments to a file (for use with `strategies/replay.py`)

## Verifying correctness

A smaller pack is only useful if it's valid. `harness/verify.py` runs five verification layers:

```bash
# Run all layers on a single repo
python3 harness/verify.py --repo corpus/my-repo

# Run all layers across every repo in corpus/
python3 harness/verify.py --layer 5
```

| Layer | What it checks |
|-------|---------------|
| 1 | Harness git without `--delta-strategy` produces packs identical to stock git |
| 2 | Pack validity: `index-pack`, `verify-pack`, `fsck`, object-list diff |
| 3 | Bracket tests: `none` strategy matches `--window=0`; `replay` matches default |
| 4 | Determinism: 3 consecutive runs produce byte-identical packs |
| 5 | Corpus sweep: layers 1-4 across all repos in `corpus/` |

## Included strategies

| Strategy | Description | Use case |
|----------|-------------|----------|
| `strategies/none.py` | Emits `NONE` for every object | Lower-bound bracket (no deltas) |
| `strategies/replay.py` | Replays recorded `(child, parent)` pairs | Upper-bound bracket (exact match to default) |
| `strategies/builtin.py` | Exact reimplementation of git's sort + window algorithm using `Q` queries | Reference baseline; byte-identical to default |

## Project layout

```
├── patch/
│   └── delta-strategy.patch      # patch applied on top of upstream git
├── git/                          # cloned + patched by build.sh (not tracked)
├── harness/
│   ├── build.sh                  # idempotent: clones git, applies patch, builds
│   ├── run.py                    # (strategy × repo) → pack size + stats
│   ├── verify.py                 # 5-layer verification suite
│   └── setup-corpus.sh           # clones test repos into corpus/
├── strategies/
│   ├── none.py                   # no deltas
│   ├── replay.py                 # replay recorded assignments
│   └── builtin.py                # exact default algorithm reimplementation
├── corpus/                       # test repos (not tracked)
└── results/                      # run logs (not tracked)
```

## How the delta-strategy extension works

The git fork adds three flags to `git pack-objects`:

- **`--delta-strategy=<cmd>`** — replaces the sort + `ll_find_deltas` block in `prepare_pack()` with the subprocess protocol described above. Git streams all candidates as `D` lines, services `Q` queries by computing deltas with `diff_delta` (caching the result on the candidate's entry), and reads back `A` assignments. Cached deltas are reused at pack-write time when the assignment matches the parent that produced the cached delta.

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
