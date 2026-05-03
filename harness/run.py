#!/usr/bin/env python3
"""
Main harness driver: run (strategy × repo) → pack size + stats + timing.

Usage:
    python3 harness/run.py --repo corpus/vue --strategy strategies/none.py
    python3 harness/run.py --repo corpus/vue   # default algorithm (no strategy)
    python3 harness/run.py --repo corpus/vue --strategy strategies/replay.py \
                           --record-file results/vue-default.record

Forces --threads=1 and disables --path-walk for determinism.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HARNESS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS_GIT = os.path.join(HARNESS_ROOT, "git", "git")
HARNESS_GIT_DIR = os.path.dirname(HARNESS_GIT)

# Make sure that when the harness git binary spawns child `git` processes,
# they find the same harness binary instead of a system git on PATH.
os.environ["PATH"] = HARNESS_GIT_DIR + os.pathsep + os.environ.get("PATH", "")
os.environ["GIT_EXEC_PATH"] = HARNESS_GIT_DIR


def run_pack_objects(repo_path, strategy_cmd=None, record_file=None,
                     include_reused=False, extra_args=None):
    """
    Run git pack-objects on the given repo.

    Returns dict with:
      - pack_file: path to .pack
      - pack_size: size in bytes
      - elapsed: wall-clock seconds
      - idx_file: path to .idx
    """
    with tempfile.TemporaryDirectory(prefix="harness-") as tmpdir:
        pack_base = os.path.join(tmpdir, "pack")

        # Get all objects in the repo
        rev_list = subprocess.run(
            [HARNESS_GIT, "-C", repo_path, "rev-list", "--all", "--objects"],
            capture_output=True, text=True, check=True
        )
        object_list = rev_list.stdout

        # Build pack-objects command
        cmd = [
            HARNESS_GIT, "-C", repo_path,
            "pack-objects",
            "--threads=1",
            "--stdout",
        ]

        if strategy_cmd:
            cmd.append(f"--delta-strategy={strategy_cmd}")

        if record_file:
            cmd.append(f"--record-strategy={record_file}")

        if include_reused:
            cmd.append("--include-reused")

        if extra_args:
            cmd.extend(extra_args)

        env = os.environ.copy()

        t0 = time.monotonic()
        result = subprocess.run(
            cmd,
            input=object_list.encode(),
            capture_output=True,
            env=env,
        )
        elapsed = time.monotonic() - t0

        if result.returncode != 0:
            print(f"pack-objects failed:\n{result.stderr.decode()}", file=sys.stderr)
            return None

        pack_file = f"{pack_base}.pack"
        with open(pack_file, 'wb') as f:
            f.write(result.stdout)

        pack_size = os.path.getsize(pack_file)
        pack_hash = hashlib.sha1(result.stdout).hexdigest()

        # Copy pack to results dir for further analysis
        results_dir = os.path.join(HARNESS_ROOT, "results")
        os.makedirs(results_dir, exist_ok=True)

        final_pack = os.path.join(results_dir, os.path.basename(pack_file))
        shutil.copy2(pack_file, final_pack)

        return {
            "pack_file": final_pack,
            "pack_size": pack_size,
            "elapsed": elapsed,
            "pack_hash": pack_hash,
        }


def main():
    parser = argparse.ArgumentParser(description="Pack-objects harness driver")
    parser.add_argument("--repo", required=True, help="Path to test repo")
    parser.add_argument("--strategy", default=None,
                        help="Strategy command (e.g. 'python3 strategies/none.py')")
    parser.add_argument("--record-file", default=None,
                        help="Record strategy results to this file")
    parser.add_argument("--include-reused", action="store_true",
                        help="Include reused-delta entries in strategy input")
    parser.add_argument("--window", type=int, default=None,
                        help="Override delta window size")
    parser.add_argument("--label", default=None,
                        help="Label for this run in results output")
    args = parser.parse_args()

    if not os.path.isdir(args.repo):
        print(f"Repo not found: {args.repo}", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(HARNESS_GIT):
        print(f"Harness git not found at {HARNESS_GIT}. Run harness/build.sh first.",
              file=sys.stderr)
        sys.exit(1)

    extra_args = []
    if args.window is not None:
        extra_args.append(f"--window={args.window}")

    result = run_pack_objects(
        args.repo,
        strategy_cmd=args.strategy,
        record_file=args.record_file,
        include_reused=args.include_reused,
        extra_args=extra_args,
    )

    if result is None:
        sys.exit(1)

    label = args.label or args.strategy or "default"
    repo_name = os.path.basename(os.path.normpath(args.repo))

    print(f"\n{'='*60}")
    print(f"  Repo:     {repo_name}")
    print(f"  Strategy: {label}")
    print(f"  Pack size: {result['pack_size']:,} bytes")
    print(f"  Elapsed:  {result['elapsed']:.2f}s")
    print(f"  Pack:     {result['pack_file']}")
    print(f"{'='*60}")

    # Append to results log
    results_log = os.path.join(HARNESS_ROOT, "results", "runs.jsonl")
    with open(results_log, 'a') as f:
        entry = {
            "repo": repo_name,
            "strategy": label,
            "pack_size": result["pack_size"],
            "elapsed": result["elapsed"],
            "pack_hash": result["pack_hash"],
            "timestamp": time.time(),
        }
        f.write(json.dumps(entry) + "\n")


if __name__ == "__main__":
    main()
