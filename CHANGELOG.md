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

### Changed

- `README.md` restructured around badges, a table of contents, a two-command
  quickstart, and links out to `docs/` instead of carrying every detail inline.

### In development

Landing on `main` but not yet documented in `docs/` or covered by the tool tables
above: cluster lifecycle, library installation, UC Volume upload
(`plugin/compute.py`), job-run submission and polling (`plugin/jobs.py`), their CLI
surface (`plugin/compute_cli.py`), and the `databricks-compute` skill. These
modules report *observed* state rather than API acceptance, because
`clusters/start` and `libraries/install` both return 200 long before anything is
actually done.

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
