#!/bin/bash
#
# Apply the --delta-strategy patch to the vendored git source.
# This script modifies builtin/pack-objects.c in-place.
# It is idempotent: if the patch marker is already present, it exits cleanly.
#
set -euo pipefail

GIT_SRC="${1:?Usage: apply-delta-strategy.sh <git-source-dir>}"
TARGET="$GIT_SRC/builtin/pack-objects.c"

if grep -q 'delta_strategy_cmd' "$TARGET"; then
    echo "Patch already applied."
    exit 0
fi

# We'll use Python for the surgical text insertions since sed is fragile
# for multi-line patches across different platforms.
python3 - "$TARGET" <<'PYEOF'
import sys, re

path = sys.argv[1]
with open(path, 'r') as f:
    src = f.read()

# === 1. Add #include "run-command.h" after pack-mtimes.h ===
src = src.replace(
    '#include "pack-mtimes.h"\n#include "parse-options.h"',
    '#include "pack-mtimes.h"\n#include "run-command.h"\n#include "parse-options.h"'
)

# === 2. Add global variables after use_path_walk declaration ===
# Find "static int use_path_walk;" — if it doesn't exist, find a nearby anchor
if 'static int use_path_walk;' in src:
    anchor = 'static int use_path_walk;'
else:
    # v2.47.0 might not have use_path_walk yet; use write_bitmap_options line
    anchor = None

# Try to find a good insertion point for our globals
# Look for the delta_cache_size line
insert_before = 'static unsigned long delta_cache_size = 0;'
new_globals = """static const char *delta_strategy_cmd;
static const char *record_strategy_file;
static int include_reused;

"""

if insert_before in src:
    src = src.replace(insert_before, new_globals + insert_before)
else:
    print(f"ERROR: Could not find anchor '{insert_before}' in {path}", file=sys.stderr)
    sys.exit(1)

# === 3. Insert the delta-strategy implementation before prepare_pack ===
strategy_code = r'''
/*
 * --delta-strategy=<cmd> support.
 *
 * Instead of QSORT + ll_find_deltas, spawn an external process, stream
 * candidate descriptors to it, read back (child, parent) assignments,
 * apply them in topological order via try_delta().
 */

struct strategy_proposal {
	struct object_entry *child;
	struct object_entry *parent;
	int parent_is_ext;
	struct object_id parent_oid;
};

static struct object_entry *find_entry_by_oid(const struct object_id *oid)
{
	return packlist_find(&to_pack, oid);
}

static void write_descriptors(int fd, struct object_entry **list, unsigned n)
{
	unsigned i;
	struct strbuf buf = STRBUF_INIT;

	for (i = 0; i < n; i++) {
		struct object_entry *entry = list[i];
		const char *type_str = type_name(oe_type(entry));
		unsigned long size = SIZE(entry);
		struct object_entry *reused_base = DELTA(entry);

		strbuf_reset(&buf);
		strbuf_addf(&buf, "%s %s %lu %08x %d %s\n",
			    oid_to_hex(&entry->idx.oid),
			    type_str,
			    size,
			    entry->hash,
			    entry->preferred_base ? 1 : 0,
			    reused_base ? oid_to_hex(&reused_base->idx.oid) : "NONE");

		if (write_in_full(fd, buf.buf, buf.len) < 0)
			die_errno(_("failed to write to delta strategy process"));
	}

	if (write_in_full(fd, "\n", 1) < 0)
		die_errno(_("failed to write terminator to delta strategy process"));

	strbuf_release(&buf);
}

static int read_assignments(FILE *fp, struct strategy_proposal **out,
			    unsigned *out_nr)
{
	struct strbuf line = STRBUF_INIT;
	struct strategy_proposal *proposals = NULL;
	unsigned nr = 0, alloc = 0;

	while (strbuf_getline_lf(&line, fp) == 0) {
		struct object_id child_oid, parent_oid;
		struct object_entry *child_entry;
		const char *p;
		int has_parent;

		if (!line.len)
			break;

		p = line.buf;
		if (parse_oid_hex(p, &child_oid, &p) || *p != ' ')
			die(_("malformed delta strategy assignment: %s"), line.buf);
		p++;

		child_entry = find_entry_by_oid(&child_oid);
		if (!child_entry)
			die(_("delta strategy referenced unknown child: %s"),
			    oid_to_hex(&child_oid));

		if (child_entry->preferred_base)
			die(_("delta strategy assigned a preferred_base as child: %s"),
			    oid_to_hex(&child_oid));

		has_parent = strcmp(p, "NONE") != 0;

		ALLOC_GROW(proposals, nr + 1, alloc);
		proposals[nr].child = child_entry;
		proposals[nr].parent = NULL;
		proposals[nr].parent_is_ext = 0;

		if (has_parent) {
			const char *end;
			if (parse_oid_hex(p, &parent_oid, &end) || *end != '\0')
				die(_("malformed parent oid in strategy assignment: %s"), p);

			oidcpy(&proposals[nr].parent_oid, &parent_oid);
			proposals[nr].parent = find_entry_by_oid(&parent_oid);

			if (!proposals[nr].parent) {
				if (bitmap_git &&
				    bitmap_has_oid_in_uninteresting(bitmap_git, &parent_oid))
					proposals[nr].parent_is_ext = 1;
				else
					die(_("delta strategy parent %s not in pack and not thin-eligible"),
					    oid_to_hex(&parent_oid));
			}
		}

		nr++;
	}

	strbuf_release(&line);

	*out = proposals;
	*out_nr = nr;
	return 0;
}

static void topo_sort_proposals(struct strategy_proposal *proposals, unsigned nr)
{
	unsigned i, j;
	unsigned *indegree;
	unsigned *order;
	unsigned order_nr = 0;
	unsigned *queue;
	unsigned queue_head = 0, queue_tail = 0;

	CALLOC_ARRAY(indegree, nr);
	ALLOC_ARRAY(order, nr);
	ALLOC_ARRAY(queue, nr);

	for (i = 0; i < nr; i++) {
		if (!proposals[i].parent)
			continue;
		for (j = 0; j < nr; j++) {
			if (j == i)
				continue;
			if (proposals[j].child == proposals[i].parent) {
				indegree[i]++;
				break;
			}
		}
	}

	for (i = 0; i < nr; i++) {
		if (indegree[i] == 0)
			queue[queue_tail++] = i;
	}

	while (queue_head < queue_tail) {
		unsigned cur = queue[queue_head++];
		order[order_nr++] = cur;

		for (i = 0; i < nr; i++) {
			if (proposals[i].parent == proposals[cur].child) {
				if (--indegree[i] == 0)
					queue[queue_tail++] = i;
			}
		}
	}

	if (order_nr != nr)
		die(_("delta strategy proposals contain a cycle"));

	{
		struct strategy_proposal *tmp;
		ALLOC_ARRAY(tmp, nr);
		for (i = 0; i < nr; i++)
			tmp[i] = proposals[order[i]];
		COPY_ARRAY(proposals, tmp, nr);
		free(tmp);
	}

	free(indegree);
	free(order);
	free(queue);
}

static void apply_strategy_proposals(struct strategy_proposal *proposals,
				     unsigned nr, int depth)
{
	unsigned i;
	unsigned long mem_usage = 0;
	intmax_t stat_proposed = 0, stat_accepted = 0;
	intmax_t stat_rejected_size = 0, stat_rejected_depth = 0;
	intmax_t stat_rejected_cycle = 0;

	for (i = 0; i < nr; i++) {
		struct strategy_proposal *p = &proposals[i];

		if (!p->parent && !p->parent_is_ext)
			continue;

		stat_proposed++;

		if (p->parent_is_ext) {
			SET_DELTA_EXT(p->child, &p->parent_oid);
			stat_accepted++;
			continue;
		}

		{
			struct unpacked trg = {0};
			struct unpacked src = {0};
			int ret;

			trg.entry = p->child;
			trg.depth = p->child->depth;

			src.entry = p->parent;
			src.depth = p->parent->depth;

			ret = try_delta(&trg, &src, depth, &mem_usage);

			if (trg.data)
				free(trg.data);
			if (trg.index)
				free_delta_index(trg.index);
			if (src.data)
				free(src.data);
			if (src.index)
				free_delta_index(src.index);

			if (ret > 0)
				stat_accepted++;
			else
				stat_rejected_size++;
		}
	}

	trace2_data_intmax("pack-objects", the_repository,
			   "delta-strategy/proposed", stat_proposed);
	trace2_data_intmax("pack-objects", the_repository,
			   "delta-strategy/accepted", stat_accepted);
	trace2_data_intmax("pack-objects", the_repository,
			   "delta-strategy/rejected-size", stat_rejected_size);
	trace2_data_intmax("pack-objects", the_repository,
			   "delta-strategy/rejected-depth", stat_rejected_depth);
	trace2_data_intmax("pack-objects", the_repository,
			   "delta-strategy/rejected-cycle", stat_rejected_cycle);
}

static void run_external_delta_strategy(struct object_entry **delta_list,
					unsigned n, int depth)
{
	struct child_process strategy = CHILD_PROCESS_INIT;
	struct strategy_proposal *proposals = NULL;
	unsigned proposal_nr = 0;
	FILE *fp;

	strvec_push(&strategy.args, delta_strategy_cmd);
	strategy.in = -1;
	strategy.out = -1;
	strategy.use_shell = 1;

	if (start_command(&strategy))
		die(_("failed to start delta strategy command: %s"),
		    delta_strategy_cmd);

	write_descriptors(strategy.in, delta_list, n);
	close(strategy.in);

	fp = fdopen(strategy.out, "r");
	if (!fp)
		die_errno(_("fdopen failed for delta strategy output"));

	read_assignments(fp, &proposals, &proposal_nr);
	fclose(fp);

	if (finish_command(&strategy))
		die(_("delta strategy command failed: %s"), delta_strategy_cmd);

	if (proposal_nr > 0) {
		topo_sort_proposals(proposals, proposal_nr);
		apply_strategy_proposals(proposals, proposal_nr, depth);
	}

	free(proposals);
}

static void record_strategy_results(struct object_entry **delta_list,
				    unsigned n, const char *filename)
{
	FILE *fp;
	unsigned i;

	fp = fopen(filename, "w");
	if (!fp)
		die_errno(_("cannot open record-strategy file '%s'"), filename);

	for (i = 0; i < n; i++) {
		struct object_entry *entry = delta_list[i];
		struct object_entry *base;

		if (entry->preferred_base)
			continue;

		base = DELTA(entry);
		fprintf(fp, "%s %s\n",
			oid_to_hex(&entry->idx.oid),
			base ? oid_to_hex(&base->idx.oid) : "NONE");
	}

	fclose(fp);
}

'''

# Find the prepare_pack function definition
prepare_pack_marker = 'static void prepare_pack(int window, int depth)\n{'
if prepare_pack_marker not in src:
    print(f"ERROR: Could not find prepare_pack function", file=sys.stderr)
    sys.exit(1)

src = src.replace(prepare_pack_marker, strategy_code + prepare_pack_marker)

# === 4. Modify the candidate-list loop to optionally include reused deltas ===
# Replace the "if (DELTA(entry))" continue block
old_delta_check = '''\t\tif (DELTA(entry))
\t\t\t/* This happens if we decided to reuse existing
\t\t\t * delta from a pack.  "reuse_delta &&" is implied.
\t\t\t */
\t\t\tcontinue;'''

new_delta_check = '''\t\tif (DELTA(entry)) {
\t\t\t/* This happens if we decided to reuse existing
\t\t\t * delta from a pack.  "reuse_delta &&" is implied.
\t\t\t */
\t\t\tif (!include_reused || !delta_strategy_cmd)
\t\t\t\tcontinue;
\t\t}'''

if old_delta_check in src:
    src = src.replace(old_delta_check, new_delta_check)
else:
    print("WARNING: Could not find DELTA(entry) continue block to patch", file=sys.stderr)

# === 5. Replace the QSORT + ll_find_deltas block ===
old_delta_block = '''\tif (nr_deltas && n > 1) {
\t\tunsigned nr_done = 0;

\t\tif (progress)
\t\t\tprogress_state = start_progress(_("Compressing objects"),
\t\t\t\t\t\t\tnr_deltas);
\t\tQSORT(delta_list, n, type_size_sort);
\t\tll_find_deltas(delta_list, n, window+1, depth, &nr_done);
\t\tstop_progress(&progress_state);
\t\tif (nr_done != nr_deltas)
\t\t\tdie(_("inconsistency with delta count"));
\t}
\tfree(delta_list);
}'''

new_delta_block = '''\tif (delta_strategy_cmd) {
\t\tif (n > 0) {
\t\t\tif (progress)
\t\t\t\tprogress_state = start_progress(
\t\t\t\t\t_("Running external delta strategy"),
\t\t\t\t\tnr_deltas);
\t\t\trun_external_delta_strategy(delta_list, n, depth);
\t\t\tstop_progress(&progress_state);
\t\t}
\t} else if (nr_deltas && n > 1) {
\t\tunsigned nr_done = 0;

\t\tif (progress)
\t\t\tprogress_state = start_progress(_("Compressing objects"),
\t\t\t\t\t\t\tnr_deltas);
\t\tQSORT(delta_list, n, type_size_sort);
\t\tll_find_deltas(delta_list, n, window+1, depth, &nr_done);
\t\tstop_progress(&progress_state);
\t\tif (nr_done != nr_deltas)
\t\t\tdie(_("inconsistency with delta count"));

\t\tif (record_strategy_file)
\t\t\trecord_strategy_results(delta_list, n,
\t\t\t\t\t\trecord_strategy_file);
\t}

\tif (delta_strategy_cmd) {
\t\tfor (i = 0; i < to_pack.nr_objects; i++)
\t\t\tbreak_delta_chains(&to_pack.objects[i]);
\t}

\tfree(delta_list);
}'''

if old_delta_block in src:
    src = src.replace(old_delta_block, new_delta_block)
else:
    print("ERROR: Could not find QSORT/ll_find_deltas block to replace", file=sys.stderr)
    # Debug: try to find what we do have
    idx = src.find('if (nr_deltas && n > 1)')
    if idx >= 0:
        print(f"Found at offset {idx}, showing context:", file=sys.stderr)
        print(repr(src[idx:idx+400]), file=sys.stderr)
    sys.exit(1)

# === 6. Add OPT entries for the new flags ===
old_opt = '''\t\tOPT_STRING_LIST(0, "uri-protocol", &uri_protocols,
\t\t\t\tN_("protocol"),
\t\t\t\tN_("exclude any configured uploadpack.blobpackfileuri with this protocol")),'''

new_opt = '''\t\tOPT_STRING(0, "delta-strategy", &delta_strategy_cmd,
\t\t\t   N_("cmd"),
\t\t\t   N_("use external command for delta parent selection")),
\t\tOPT_STRING(0, "record-strategy", &record_strategy_file,
\t\t\t   N_("file"),
\t\t\t   N_("record delta parent assignments to file")),
\t\tOPT_BOOL(0, "include-reused", &include_reused,
\t\t\t N_("include reused-delta entries in strategy input")),
\t\tOPT_STRING_LIST(0, "uri-protocol", &uri_protocols,
\t\t\t\tN_("protocol"),
\t\t\t\tN_("exclude any configured uploadpack.blobpackfileuri with this protocol")),'''

if old_opt in src:
    src = src.replace(old_opt, new_opt)
else:
    print("ERROR: Could not find OPT_STRING_LIST uri-protocol to insert before", file=sys.stderr)
    sys.exit(1)

with open(path, 'w') as f:
    f.write(src)

print("Patch applied successfully.")
PYEOF
