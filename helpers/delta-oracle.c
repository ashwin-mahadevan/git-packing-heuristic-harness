#define USE_THE_REPOSITORY_VARIABLE
#include "git-compat-util.h"
#include "delta.h"
#include "environment.h"
#include "hex.h"
#include "object.h"
#include "object-file.h"
#include "object-store-ll.h"
#include "repository.h"
#include "setup.h"
#include "strbuf.h"

/*
 * delta-oracle: compute exact delta sizes using git's delta algorithm.
 *
 * Protocol (line-oriented, stdin/stdout):
 *   Input:  <trg_oid> <src_oid> <max_delta_size>
 *   Output: <delta_size>     (0 if delta failed or exceeded max_delta_size)
 *
 * Empty input line → flush output, print empty line, continue.
 * EOF → exit.
 *
 * Must be run from within a git repository (or with GIT_DIR set).
 */

int cmd_main(int argc, const char **argv)
{
	struct strbuf line = STRBUF_INIT;

	setup_git_directory();

	while (strbuf_getline_lf(&line, stdin) == 0) {
		struct object_id trg_oid, src_oid;
		unsigned long max_delta_size;
		const char *p;
		enum object_type trg_type, src_type;
		unsigned long trg_size, src_size, delta_size;
		void *trg_data, *src_data, *delta_buf;

		if (!line.len) {
			printf("\n");
			fflush(stdout);
			continue;
		}

		/* Parse: <trg_oid> <src_oid> <max_delta_size> */
		p = line.buf;
		if (parse_oid_hex(p, &trg_oid, &p) || *p != ' ')
			die("malformed input: %s", line.buf);
		p++;
		if (parse_oid_hex(p, &src_oid, &p) || *p != ' ')
			die("malformed input: %s", line.buf);
		p++;
		max_delta_size = strtoul(p, NULL, 10);

		trg_data = repo_read_object_file(the_repository, &trg_oid,
						 &trg_type, &trg_size);
		if (!trg_data)
			die("cannot read object %s", oid_to_hex(&trg_oid));

		src_data = repo_read_object_file(the_repository, &src_oid,
						 &src_type, &src_size);
		if (!src_data) {
			free(trg_data);
			printf("0\n");
			fflush(stdout);
			continue;
		}

		delta_buf = diff_delta(src_data, src_size,
				       trg_data, trg_size,
				       &delta_size, max_delta_size);

		if (delta_buf) {
			printf("%lu\n", delta_size);
			free(delta_buf);
		} else {
			printf("0\n");
		}
		fflush(stdout);

		free(trg_data);
		free(src_data);
	}

	strbuf_release(&line);
	return 0;
}
