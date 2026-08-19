<h1 align="center">hermes-databricks-plugin</h1>

<p align="center">
  <em>Query Databricks Unity Catalog from Hermes Agent — with credentials derived from your Databricks CLI profiles, and a bulk path that keeps multi-GB results out of the model's context window.</em>
</p>

<p align="center">
  <a href="https://github.com/e8kor/hermes-databricks-plugin/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/e8kor/hermes-databricks-plugin/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green.svg"></a>
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-blue.svg">
  <img alt="Dependencies: none" src="https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg">
  <a href="https://github.com/NousResearch/hermes-agent"><img alt="Hermes Agent plugin" src="https://img.shields.io/badge/hermes--agent-plugin-8A2BE2.svg"></a>
</p>

---

## Contents

- [Why this exists](#why-this-exists)
- [What you get](#what-you-get)
- [Requirements](#requirements)
- [Install](#install)
- [Quickstart](#quickstart)
- [Usage](#usage)
- [Documentation](#documentation)
- [Design notes](#design-notes)
- [Development](#development)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

## Why this exists

Databricks ships managed MCP servers, and for small lookups they work fine. Two
things pushed me to write a plugin instead:

- **The credential can't refresh.** The managed `/api/2.0/mcp/sql` endpoint takes
  a static bearer in a config header, so a long session outlives its token.
- **Large results are truncated or refused.** Asking the managed `sql` endpoint
  for 20,000 rows returned 9,166 of them. This plugin's inline path returned all
  20,000, and its bulk path streams a 100,000-row export to disk in under two
  seconds.

So: tokens are minted **per call** from your CLI profile (1-hour OAuth, re-minted
60s before expiry), and anything big goes to a file via Databricks
`EXTERNAL_LINKS` chunked delivery instead of into the conversation.

## What you get

**4 tools**

| Tool | Purpose |
|---|---|
| `databricks_status` | Workspaces, auth mechanism, visible CLI profiles, live credential check |
| `databricks_catalog` | Browse catalogs → schemas → tables → one table's columns |
| `databricks_query` | Run SQL, rows returned inline, capped so a wide result can't flood context |
| `databricks_fetch` | Stream a complete result to csv/jsonl on disk; returns a summary + path |

Full parameter reference: [docs/tools.md](docs/tools.md).

**5 skills** (registered as `databricks:<name>`, loaded explicitly with `skill_view`)

| Skill | Covers |
|---|---|
| `databricks` | Entry point: choosing the right tool for the result size |
| `databricks-workspace-setup` | CLI profiles, multi-workspace, CI/service principals |
| `databricks-sql-authoring` | Databricks SQL dialect, exploration order, cost control |
| `databricks-bulk-export` | The export-then-analyze-on-disk loop |
| `databricks-compute` | Clusters, library upload/install, Spark job runs |
| `databricks-troubleshooting` | Symptom → cause table for auth/permission/size failures |

**A CLI** (`hermes databricks ...`) and a slash command (`/databricks ...`), both
covering the same lifecycle: CONNECT → EXPLORE → QUERY → COMPUTE.

**Compute is CLI-only, on purpose.** Clusters and job runs cost money, so they
are not model tools — no cluster can be created or started from a tool call
inside a conversation. Every billable mutation confirms interactively (`--yes`
to skip), `cluster create` requires an explicit `--policy`, and an
autotermination under 10 minutes is refused outright.

## Requirements

- [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- Python 3.9+ — standard library only, no third-party dependencies
- The [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html),
  v0.240 or newer (optional if you configure a host + static token instead)

## Install

```bash
git clone https://github.com/e8kor/hermes-databricks-plugin.git
cd hermes-databricks-plugin
./install.sh
hermes plugins enable databricks
```

`install.sh` copies `plugin/` to `$HERMES_HOME/plugins/databricks` (honouring
`HERMES_HOME`, so it installs into the active profile). Pass `--symlink` to
symlink instead of copy — that's what you want if you intend to hack on it.
Restart Hermes for the plugin to load.

## Quickstart

```bash
databricks auth login --host https://<your-workspace>.cloud.databricks.com
hermes databricks status
```

That's it. With a logged-in CLI and **no Hermes config at all**, the plugin adopts
the CLI's default profile and reports the workspace as `cli-default`.

Pinning a profile, multiple workspaces, and CI / service-principal setups are all
covered in [docs/configuration.md](docs/configuration.md).

## Usage

```bash
# CONNECT
hermes databricks status
hermes databricks profiles --warehouses

# EXPLORE
hermes databricks catalogs
hermes databricks catalogs samples
hermes databricks catalogs samples tpch
hermes databricks catalogs samples tpch lineitem     # one table's columns

# QUERY
hermes databricks query "SELECT count(*) FROM samples.nyctaxi.trips"
hermes databricks fetch "SELECT * FROM samples.nyctaxi.trips" --format csv

# COMPUTE — clusters, libraries, Spark job runs (billable, CLI only)
hermes databricks cluster list                       # what is running and billing
hermes databricks cluster policies                   # policy ids for create
hermes databricks cluster versions                   # LTS runtimes
hermes databricks cluster start|restart|stop <id>

# put a locally-built JAR where Spark can read it (byte-verified)
hermes databricks library create-volume main default hermes_libs
hermes databricks library upload ./target/mylib.jar \
    /Volumes/main/default/hermes_libs/mylib.jar
hermes databricks library install <cluster_id> --jar /Volumes/.../mylib.jar
hermes databricks library status <cluster_id>        # the REAL install verdict

# run Spark code — ephemeral job cluster by default, dies when the run ends
# note: a python entry point must be a WORKSPACE file, not a Volume file
hermes databricks job submit --name smoke \
    --spark-version 17.3.x-scala2.13 --node-type Standard_D4ds_v5 \
    --python-file /Workspace/Users/you@example.com/job.py
hermes databricks job submit --cluster <id> --main-class com.x.Main \
    --jar /Volumes/.../mylib.jar          # reuse a warm cluster for iteration
hermes databricks job show <run_id>
hermes databricks job output <run_id>                # stack trace on failure
hermes databricks job list --active                  # what is still billing
```

The same verbs exist as `/databricks status`, `/databricks catalogs ...`,
`/databricks query <sql>`, `/databricks fetch <sql>`. Full flag reference:
[docs/cli.md](docs/cli.md).

## Documentation

| Document | What's in it |
|---|---|
| [docs/configuration.md](docs/configuration.md) | The three config shapes, resolution order, multi-workspace, CI |
| [docs/tools.md](docs/tools.md) | Every tool's parameters, response shape, and failure modes |
| [docs/cli.md](docs/cli.md) | `hermes databricks` and `/databricks` command reference |
| [docs/skills.md](docs/skills.md) | What each bundled skill covers and when it loads |
| [docs/architecture.md](docs/architecture.md) | Module map, request lifecycle, token cache, pagination |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Symptom → cause → fix |

## Design notes

Things that are deliberate, and worth knowing before you change them:

- **`databricks_status` is never gated on configuration.** It's the diagnostic —
  it has to work precisely when everything else is broken. The other three tools
  are gated, so they vanish rather than fail confusingly.
- **Credential errors never echo the helper's output.** A `key_cmd` can print a
  token on failure, or embed a secret in its arguments. The error tells you to run
  the command yourself instead.
- **`truncated: true` is treated as data loss.** Databricks reports a
  moderately-oversized inline result as `SUCCEEDED` while dropping rows. The tool
  surfaces that as a warning and points at `databricks_fetch`.
- **Unity Catalog list endpoints are paginated in full.** Reading only the first
  page reports a partial catalog as complete — verified: `samples.tpcds_sf1` at 3
  rows/page needs 8 pages for 24 tables.
- **`columns: []` from Unity Catalog does not mean "no columns".** For
  `TABLE_DELTASHARING` tables (all of `samples.tpch`, `samples.tpcds_*`) UC
  returns no column metadata even on a single-table GET. The single-table path
  falls back to `DESCRIBE TABLE`; the listing reports `column_count` explicitly
  so an empty list can't be misread.
- **Exports go to `$HERMES_HOME/databricks-exports/`,** profile-aware via
  `get_hermes_home()` — never a hardcoded `~/.hermes`.
- **`libraries/install` returning 200 means "queued", not "installed".** The
  library then moves `PENDING` → `INSTALLING` → `INSTALLED`/`FAILED`, and a
  failure appears only in `libraries/cluster-status`. `library install` polls to
  a terminal state; reporting success on the 200 would be wrong exactly when it
  matters.
- **A finished run is not a successful run.** `life_cycle_state: TERMINATED`
  with `result_state: FAILED` is the normal shape of "your Spark code threw", so
  runs are judged on `result_state` and expose an explicit `succeeded` field.
- **Uploads verify the remote byte count.** A truncated JAR is accepted by the
  Files API and fails much later on an executor with an opaque class-loading
  error; comparing sizes turns that into an immediate, legible failure.
- **`autotermination_minutes: 0` is refused.** The API accepts it, and it means
  *never terminate* — the single most expensive mistake available here.
- **Workspace-admin does not imply Unity Catalog write.** A 403 on upload is
  re-labelled to say so, since the two permission systems are independent and
  the raw error does not explain the difference.
- **A `spark_python_task` cannot load its `python_file` from a UC Volume,** and
  the platform's error blames permissions instead. Proven live across 3 failed
  runs plus a successful control: the same file at `/Workspace/...` succeeds on
  an identical cluster spec while `/Volumes/...` fails even when you own the
  volume and hold explicit `READ_VOLUME`. `job submit` rejects a Volume
  python_file up front rather than spending ~5 minutes of cluster startup to
  reach a misleading message. JAR libraries from `/Volumes` are unaffected.

Rationale for each is expanded in [docs/architecture.md](docs/architecture.md).

## Development

```bash
./install.sh --symlink            # live-edit the repo as the installed plugin
python -m pytest tests/ -q        # 100 tests, stdlib + pytest, no network
```

Tests exercise the API layer against recorded response shapes; nothing reaches
Databricks. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

## Contributing

Issues and pull requests are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md) for setup, conventions, and what a good PR
looks like here, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for expectations.
Release history lives in [CHANGELOG.md](CHANGELOG.md).

## Security

Never open a public issue for a vulnerability. Reporting instructions and this
plugin's credential-handling model are in [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).
