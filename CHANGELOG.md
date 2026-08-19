# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Open-source documentation set: `docs/configuration.md`, `docs/tools.md`,
  `docs/cli.md`, `docs/skills.md`, `docs/architecture.md`,
  `docs/troubleshooting.md`.
- Community health files: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `SECURITY.md`, this changelog, issue and pull request templates.
- GitHub Actions CI: test suite on Python 3.9–3.12 plus a byte-compile check of
  every plugin module.
- **Compute surface — clusters, libraries, and Spark job runs.** CLI-only
  (`hermes databricks cluster|library|job`), adding **zero** model tools: this
  work spends money, so nothing in a conversation can start billable compute via
  a tool call. New modules `plugin/compute.py` (cluster lifecycle, library
  install, UC Volume upload), `plugin/jobs.py` (run submit/poll/output), and
  `plugin/compute_cli.py` (the argparse trees plus the confirmation layer), and a
  sixth bundled skill, `databricks:databricks-compute`.
  - Spend guardrails: `cluster create` requires an explicit `--policy`, refuses
    `autotermination_minutes < 10` (0 means *never terminate*), and every
    billable mutation confirms interactively — a non-interactive shell refuses
    rather than proceeding. `cluster list` flags every RUNNING cluster.
  - Correctness over API acceptance, because several of these endpoints return
    200 long before the work is done or succeeded:
    - `libraries/install` is polled to a terminal state; a `FAILED` install
      raises instead of being reported as success.
    - Job runs are judged on `result_state`, never on having finished — a
      `TERMINATED` + `FAILED` run exits non-zero with its stack trace.
    - Volume uploads verify the remote byte count against the local one.
  - Documented in `docs/cli.md` (COMPUTE section), `docs/skills.md`, and
    `docs/architecture.md`.
- 82 new tests (126 total, still stdlib + pytest with no network), including
  regression tests for each live-observed failure below.

### Fixed

Three platform behaviours found by running against a real Azure workspace, all
of which produce misleading errors and are now either prevented or re-labelled:

- **A `spark_python_task` cannot load its `python_file` from a UC Volume.** It
  fails with *"Cannot read the python file … lacks the required permissions"*
  even when the caller owns the volume, holds explicit `READ_VOLUME`, can list
  the file over the Files API, and the cluster runs as `SINGLE_USER` with that
  identity. The identical file at `/Workspace/…` succeeds on the same cluster
  spec, so the constraint is the path type, not access. `job submit` now rejects
  a Volume `python_file` up front instead of spending ~5 minutes of cluster
  startup to reach a wrong error. JAR libraries from `/Volumes` are unaffected.
- **A `sql_task` requires a saved `query_id`,** not inline SQL, which fails with
  `Missing required field: query_id`. The flag is `--sql-query-id`; ad-hoc SQL
  belongs in `query`/`fetch`.
- **`hermes databricks job submit` silently dropped `--spark-version` when
  `--cluster` was also given,** hiding a real mistake about where the code ran
  and what it cost. Ambiguous compute is now refused locally, before any API
  call.

Also worth recording: the Files API returns `name`/`file_size`, not the legacy
DBFS `path`/`size` (reading the wrong keys yields a listing of `None`s that looks
empty), and owning a UC Volume does **not** create an explicit privilege
assignment — `permissions/volume/<fqn>` came back `{}` for a volume created by
that same principal.

### Changed

- `README.md` restructured around badges, a table of contents, a two-command
  quickstart, and links out to `docs/` instead of carrying every detail inline.
- The CLI lifecycle grouping is now CONNECT → EXPLORE → QUERY → COMPUTE.

## [1.2.0] - 2026-08-18

### Added

- Zero-config operation: with a logged-in Databricks CLI and no `databricks:`
  section at all, the CLI's default profile is adopted and reported as the
  workspace `cli-default`.
- `hermes databricks profiles [--refresh] [--warehouses]` — list the CLI profiles
  available to adopt, with the CLI's own validity verdict and, optionally, each
  profile's SQL warehouses.
- `hermes databricks setup --profile <name>` — pin a workspace to a CLI profile,
  deriving host and auth from it, so re-running `databricks auth login` needs no
  config edit.
- `databricks_catalog` single-table path, with a `DESCRIBE TABLE` fallback for
  table kinds where Unity Catalog returns no column metadata.

### Changed

- Profile discovery goes through `databricks auth profiles -o json` rather than
  parsing `~/.databrickscfg`, so environment overrides, account-vs-workspace hosts,
  and the CLI's validity verdict come for free. Ini parsing remains as a fallback
  when the CLI is absent.
- Profile listings are cached for 300s, because `auth profiles` contacts every
  workspace to validate and takes seconds rather than milliseconds. Use
  `--refresh` after logging in.

### Fixed

- Unity Catalog list endpoints are now paginated to completion; reading only the
  first page reported a partial catalog as complete.
- A `truncated: true` inline result is surfaced as data loss with a pointer to
  `databricks_fetch`, instead of being reported as a plain success.
- Account-console hosts (`accounts.*`) are skipped when auto-picking a profile;
  they have no SQL warehouses.

## [1.0.0] - 2026-08-18

### Added

- Initial release: `databricks_status`, `databricks_query`, `databricks_fetch`,
  `databricks_catalog`.
- Per-call OAuth token minting with a 60s pre-expiry refresh window, mirroring the
  Hermes model-provider `key_cmd` contract.
- `databricks_fetch` bulk path using Databricks `EXTERNAL_LINKS` chunked delivery,
  streaming results to csv/jsonl on disk with no inline size ceiling.
- `hermes databricks` CLI and `/databricks` slash command over the same
  CONNECT → EXPLORE → QUERY lifecycle.
- Five bundled skills registered as `databricks:<name>`.

[Unreleased]: https://github.com/e8kor/hermes-databricks-plugin/compare/main...HEAD
