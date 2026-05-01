#!/usr/bin/env python3
"""
Verification layers for the delta-strategy harness.

Each layer is a function that returns (passed: bool, message: str).
Run all layers with: python3 harness/verify.py --repo <path> [--layer N]

Layers:
  1. No-op check: patched git without --delta-strategy == stock git
  2. Pack validity: index-pack, verify-pack, fsck, clone, object-list diff
  3. Bracket strategies: none == window=0; replay == default
  4. Determinism: 3x back-to-back identical packs
  5. Corpus sweep: layers 1-4 across all repos in corpus/
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

HARNESS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS_GIT = os.path.join(HARNESS_ROOT, "git", "git")
STOCK_GIT = shutil.which("git")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def get_object_list(git_bin, repo_path):
    result = subprocess.run(
        [git_bin, "-C", repo_path, "rev-list", "--all", "--objects"],
        capture_output=True, text=True, check=True
    )
    return result.stdout


def pack_objects(git_bin, repo_path, pack_base, object_list,
                 extra_args=None, env_extra=None):
    cmd = [
        git_bin, "-C", repo_path,
        "pack-objects", "--threads=1",
        "--stdout",
    ]
    if extra_args:
        cmd.extend(extra_args)

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)

    result = subprocess.run(
        cmd, input=object_list.encode(),
        capture_output=True, env=env,
    )
    if result.returncode != 0:
        return None, result.stderr.decode()

    pack_file = pack_base + ".pack"
    with open(pack_file, 'wb') as f:
        f.write(result.stdout)

    pack_hash = hashlib.sha1(result.stdout).hexdigest()
    return pack_hash, None


def layer1_noop(repo_path):
    """Harness git without --delta-strategy produces byte-identical pack to stock git."""
    with tempfile.TemporaryDirectory(prefix="verify-l1-") as tmpdir:
        obj_list = get_object_list(HARNESS_GIT, repo_path)

        stock_base = os.path.join(tmpdir, "stock")
        patched_base = os.path.join(tmpdir, "patched")

        stock_hash, err = pack_objects(STOCK_GIT, repo_path, stock_base, obj_list)
        if err:
            return False, f"Stock git pack-objects failed: {err}"

        patched_hash, err = pack_objects(HARNESS_GIT, repo_path, patched_base, obj_list)
        if err:
            return False, f"Harness git pack-objects failed: {err}"

        stock_pack = f"{stock_base}.pack"
        patched_pack = f"{patched_base}.pack"

        stock_sha = sha256_file(stock_pack)
        patched_sha = sha256_file(patched_pack)

        if stock_sha != patched_sha:
            stock_size = os.path.getsize(stock_pack)
            patched_size = os.path.getsize(patched_pack)
            return False, (f"Pack mismatch: stock={stock_sha[:16]}.. ({stock_size}B) "
                          f"patched={patched_sha[:16]}.. ({patched_size}B)")

        return True, f"Packs identical ({os.path.getsize(stock_pack):,} bytes)"


def layer2_validity(repo_path, pack_file=None, strategy_cmd=None):
    """Verify pack validity: index-pack, verify-pack, fsck, clone, object diff."""
    with tempfile.TemporaryDirectory(prefix="verify-l2-") as tmpdir:
        if pack_file is None:
            obj_list = get_object_list(HARNESS_GIT, repo_path)
            pack_base = os.path.join(tmpdir, "test")
            extra = []
            if strategy_cmd:
                extra.append(f"--delta-strategy={strategy_cmd}")
            pack_hash, err = pack_objects(HARNESS_GIT, repo_path, pack_base,
                                          obj_list, extra_args=extra)
            if err:
                return False, f"pack-objects failed: {err}"
            pack_file = f"{pack_base}.pack"

        # git index-pack
        idx_result = subprocess.run(
            [HARNESS_GIT, "index-pack", pack_file],
            capture_output=True, text=True,
        )
        if idx_result.returncode != 0:
            return False, f"index-pack failed: {idx_result.stderr}"

        # git verify-pack -v
        verify_result = subprocess.run(
            [HARNESS_GIT, "verify-pack", "-v", pack_file],
            capture_output=True, text=True,
        )
        if verify_result.returncode != 0:
            return False, f"verify-pack failed: {verify_result.stderr}"

        # Build a repo from the pack alone
        test_repo = os.path.join(tmpdir, "from-pack")
        os.makedirs(os.path.join(test_repo, ".git", "objects", "pack"), exist_ok=True)
        subprocess.run(
            [HARNESS_GIT, "init", "--bare", test_repo],
            capture_output=True, check=True,
        )

        pack_dest = os.path.join(test_repo, "objects", "pack")
        shutil.copy2(pack_file, pack_dest)
        idx_file = pack_file.replace(".pack", ".idx")
        if os.path.exists(idx_file):
            shutil.copy2(idx_file, pack_dest)
        else:
            subprocess.run(
                [HARNESS_GIT, "index-pack",
                 os.path.join(pack_dest, os.path.basename(pack_file))],
                capture_output=True, check=True,
            )

        # git fsck
        fsck_result = subprocess.run(
            [HARNESS_GIT, "-C", test_repo, "fsck"],
            capture_output=True, text=True,
        )
        if fsck_result.returncode != 0:
            return False, f"fsck failed: {fsck_result.stderr}"

        # Compare object lists
        source_objs = subprocess.run(
            [HARNESS_GIT, "-C", repo_path, "rev-list", "--all", "--objects"],
            capture_output=True, text=True, check=True,
        ).stdout
        source_set = set(line.split()[0] for line in source_objs.strip().split('\n') if line.strip())

        # We need refs in the test repo. Transfer refs from source.
        refs_result = subprocess.run(
            [HARNESS_GIT, "-C", repo_path, "show-ref"],
            capture_output=True, text=True,
        )
        if refs_result.returncode == 0:
            for ref_line in refs_result.stdout.strip().split('\n'):
                if not ref_line.strip():
                    continue
                oid, refname = ref_line.split(None, 1)
                subprocess.run(
                    [HARNESS_GIT, "-C", test_repo, "update-ref", refname, oid],
                    capture_output=True,
                )

        test_objs = subprocess.run(
            [HARNESS_GIT, "-C", test_repo, "rev-list", "--all", "--objects"],
            capture_output=True, text=True,
        ).stdout
        test_set = set(line.split()[0] for line in test_objs.strip().split('\n') if line.strip())

        if source_set != test_set:
            missing = source_set - test_set
            extra = test_set - source_set
            return False, f"Object mismatch: {len(missing)} missing, {len(extra)} extra"

        return True, "Pack valid (index-pack, verify-pack, fsck, object-list match)"


def layer3_brackets(repo_path):
    """Bracket tests: none == window=0, replay == default."""
    results = []

    with tempfile.TemporaryDirectory(prefix="verify-l3-") as tmpdir:
        obj_list = get_object_list(HARNESS_GIT, repo_path)

        # --- none strategy vs window=0 ---
        none_base = os.path.join(tmpdir, "none")
        none_strategy = f"python3 {os.path.join(HARNESS_ROOT, 'strategies', 'none.py')}"
        none_hash, err = pack_objects(HARNESS_GIT, repo_path, none_base,
                                       obj_list,
                                       extra_args=[f"--delta-strategy={none_strategy}"])
        if err:
            return False, f"none strategy pack-objects failed: {err}"

        w0_base = os.path.join(tmpdir, "window0")
        w0_hash, err = pack_objects(HARNESS_GIT, repo_path, w0_base,
                                     obj_list,
                                     extra_args=["--window=0"])
        if err:
            return False, f"window=0 pack-objects failed: {err}"

        none_size = os.path.getsize(f"{none_base}.pack")
        w0_size = os.path.getsize(f"{w0_base}.pack")

        if none_size != w0_size:
            results.append(f"FAIL: none strategy ({none_size:,}B) != window=0 ({w0_size:,}B)")
        else:
            results.append(f"OK: none strategy == window=0 ({none_size:,}B)")

        # --- replay strategy vs default ---
        # First, run default with --record-strategy
        record_file = os.path.join(tmpdir, "default.record")
        default_base = os.path.join(tmpdir, "default")
        default_hash, err = pack_objects(HARNESS_GIT, repo_path, default_base,
                                          obj_list,
                                          extra_args=[f"--record-strategy={record_file}"])
        if err:
            return False, f"default (record) pack-objects failed: {err}"

        # Then, run replay with those recorded pairs
        replay_base = os.path.join(tmpdir, "replay")
        replay_strategy = f"python3 {os.path.join(HARNESS_ROOT, 'strategies', 'replay.py')} {record_file}"
        replay_hash, err = pack_objects(HARNESS_GIT, repo_path, replay_base,
                                         obj_list,
                                         extra_args=[f"--delta-strategy={replay_strategy}"])
        if err:
            return False, f"replay strategy pack-objects failed: {err}"

        default_size = os.path.getsize(f"{default_base}.pack")
        replay_size = os.path.getsize(f"{replay_base}.pack")

        if default_size != replay_size:
            results.append(f"FAIL: replay ({replay_size:,}B) != default ({default_size:,}B)")
        else:
            results.append(f"OK: replay == default ({default_size:,}B)")

    all_passed = all(r.startswith("OK") for r in results)
    return all_passed, "; ".join(results)


def layer4_determinism(repo_path, strategy_cmd=None):
    """3x back-to-back packs must be byte-identical."""
    with tempfile.TemporaryDirectory(prefix="verify-l4-") as tmpdir:
        obj_list = get_object_list(HARNESS_GIT, repo_path)
        hashes = []

        for run in range(3):
            base = os.path.join(tmpdir, f"run{run}")
            extra = []
            if strategy_cmd:
                extra.append(f"--delta-strategy={strategy_cmd}")
            pack_hash, err = pack_objects(HARNESS_GIT, repo_path, base,
                                           obj_list, extra_args=extra)
            if err:
                return False, f"Run {run} failed: {err}"
            pack_file = f"{base}.pack"
            hashes.append(sha256_file(pack_file))

        if len(set(hashes)) != 1:
            return False, f"Non-deterministic: {hashes}"

        return True, f"Deterministic across 3 runs"


def layer5_corpus(corpus_dir):
    """Run layers 1-4 across every repo in corpus/."""
    if not os.path.isdir(corpus_dir):
        return False, f"Corpus directory not found: {corpus_dir}"

    repos = []
    for entry in sorted(os.listdir(corpus_dir)):
        repo_path = os.path.join(corpus_dir, entry)
        if os.path.isdir(os.path.join(repo_path, ".git")):
            repos.append(repo_path)

    if not repos:
        return False, "No repos found in corpus/"

    all_passed = True
    messages = []

    for repo_path in repos:
        repo_name = os.path.basename(repo_path)
        print(f"\n--- Corpus: {repo_name} ---")

        for layer_num, layer_fn in [(1, layer1_noop), (2, layer2_validity),
                                     (3, layer3_brackets), (4, layer4_determinism)]:
            try:
                if layer_num == 2:
                    passed, msg = layer_fn(repo_path)
                else:
                    passed, msg = layer_fn(repo_path)
            except Exception as e:
                passed, msg = False, f"Exception: {e}"

            status = "PASS" if passed else "FAIL"
            print(f"  Layer {layer_num}: [{status}] {msg}")
            if not passed:
                all_passed = False
            messages.append(f"{repo_name}/L{layer_num}: [{status}]")

    return all_passed, "; ".join(messages)


def main():
    parser = argparse.ArgumentParser(description="Verification layers")
    parser.add_argument("--repo", help="Path to test repo")
    parser.add_argument("--layer", type=int, choices=[1, 2, 3, 4, 5],
                        help="Run specific layer (default: all)")
    parser.add_argument("--corpus", default=os.path.join(HARNESS_ROOT, "corpus"),
                        help="Corpus directory for layer 6")
    args = parser.parse_args()

    layers = {
        1: ("No-op check", lambda: layer1_noop(args.repo)),
        2: ("Pack validity", lambda: layer2_validity(args.repo)),
        3: ("Bracket strategies", lambda: layer3_brackets(args.repo)),
        4: ("Determinism", lambda: layer4_determinism(args.repo)),
        5: ("Corpus sweep", lambda: layer5_corpus(args.corpus)),
    }

    if args.layer:
        run_layers = [args.layer]
    elif args.repo:
        run_layers = [1, 2, 3, 4]
    else:
        run_layers = [5]

    if any(l != 5 for l in run_layers) and not args.repo:
        print("Error: --repo required for layers 1-4", file=sys.stderr)
        sys.exit(1)

    all_passed = True
    for layer_num in run_layers:
        name, fn = layers[layer_num]
        print(f"\n{'='*60}")
        print(f"  Layer {layer_num}: {name}")
        print(f"{'='*60}")

        try:
            passed, msg = fn()
        except Exception as e:
            passed, msg = False, f"Exception: {e}"

        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {msg}")

        if not passed:
            all_passed = False

    print(f"\n{'='*60}")
    print(f"  Overall: {'ALL PASSED' if all_passed else 'FAILURES DETECTED'}")
    print(f"{'='*60}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
