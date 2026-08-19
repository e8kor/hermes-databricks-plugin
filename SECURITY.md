# Security Policy

## Reporting a vulnerability

**Do not open a public issue.** Report privately via GitHub's
[private vulnerability reporting](https://github.com/e8kor/hermes-databricks-plugin/security/advisories/new)
on this repository.

Please include: what you can do that you shouldn't be able to, the smallest
reproduction you have, and the plugin version (`plugin/plugin.yaml`). Expect an
acknowledgement within a few days. Since this is a single-maintainer project,
there is no SLA beyond "as fast as is reasonable"; a fix ships as a patch release
with credit in `CHANGELOG.md` unless you'd rather not be named.

## Supported versions

The latest release on `main` is the supported version. There are no maintained
release branches.

## Threat model

This plugin runs inside your Hermes Agent process, on your machine, with your
Databricks identity. It does not add a network listener, and it talks to exactly
one remote host: the Databricks workspace you configured.

What it can reach is bounded entirely by the Databricks credential it resolves —
Unity Catalog grants and warehouse permissions are the real access boundary. The
plugin does not attempt to be a second authorization layer, and an agent with the
`databricks` toolset enabled can run any SQL your identity can run. If that isn't
acceptable, give the plugin a service principal scoped to what the agent should
see, rather than your own workspace admin identity.

### Spend is part of the threat model

Databricks compute costs money, so "what can this do" includes "what can this
bill". Two deliberate boundaries:

- **No compute operation is reachable from a tool call.** The
  `cluster` / `library` / `job` surface is CLI-only
  (`hermes databricks cluster …`). The agent's toolset contains exactly four
  tools — `databricks_status`, `databricks_catalog`, `databricks_query`,
  `databricks_fetch` — and none of them can create a cluster, start one, or
  submit a job run. An agent cannot start billable compute on your behalf,
  because the capability is not in its schema at all.
- **Interactive confirmation on every billable mutation.** `cluster create`,
  `start`, `restart` and `job submit` prompt before acting; `--yes` skips the
  prompt, and a **non-interactive shell refuses rather than proceeding**. That
  last part matters for cron and CI: a scripted invocation that forgot `--yes`
  fails closed instead of quietly provisioning a cluster.

Residual risk worth naming honestly: anything that can run shell commands as you
can run `hermes databricks cluster create --yes`. The CLI-only boundary protects
against an agent *choosing* to spend money via its tools; it is not a sandbox
against arbitrary shell execution. If an agent in your setup has an unrestricted
`terminal` tool, treat its spend authority as equal to your own.

Cost guardrails that are enforced in code rather than documented as advice:

- `cluster create` requires an explicit `--policy`, so workspace cluster policies
  (size, instance type, spend caps) always apply.
- `autotermination_minutes` below 10 is refused. The API accepts `0`, which means
  *never terminate* — the most expensive single mistake available here.
- `cluster list` flags every RUNNING cluster, and `job list --active` shows runs
  that are still billing.
- An ephemeral job cluster is the default for `job submit`; it is torn down when
  the run ends, so a forgotten cluster is not the default outcome.

## Credential handling

Design choices that exist specifically for this:

- **Credentials are derived, never stored.** The preferred configuration names a
  Databricks CLI profile; the bearer token is minted per call via
  `databricks auth token -p <profile>`, lives 3600s, and is re-minted 60s before
  expiry. No token is written to Hermes config or to disk by this plugin.
- **Tokens are cached in memory only,** keyed by workspace name, with a
  process-lifetime `dict` guarded by a lock. Static tokens read from `.env` are
  deliberately *not* cached, so rotating one takes effect immediately.
- **Errors never echo credential-helper output.** A `key_cmd` may print a token on
  failure and may legitimately embed a secret in its own arguments
  (`print-token --client-secret=...`). Failures report the exit code and tell you
  to run the command yourself; stdout, stderr, and the command string are never
  surfaced.
- **Static tokens live in `.env`, referenced by name.** `auth.token_env` names an
  environment variable; the value never appears in `config.yaml`. Reads go through
  the Hermes profile secret scope where available.
- **A pasted `Bearer ` prefix is stripped** before use, so a copy-paste mistake
  produces a working call rather than a confusing 401.

## Data handling

- `databricks_fetch` writes query results to
  `$HERMES_HOME/databricks-exports/` — inside the active Hermes profile, using
  `get_hermes_home()`, never a hardcoded path. **These files are plain
  csv/jsonl containing whatever you queried,** with no encryption and no
  automatic cleanup. If you export sensitive data, delete the exports.
- Query results pass through the agent's context window when using
  `databricks_query`. Prefer `databricks_fetch` for anything sensitive or large:
  the summary reaches the model, the rows stay on disk.
- No telemetry, analytics, or third-party network calls of any kind.

## Hardening suggestions

- Use a service principal with a narrow set of Unity Catalog grants for
  agent-driven access, instead of your interactive identity.
- Prefer a SQL warehouse with a query timeout and a modest size, so a bad query
  fails cheaply.
- Keep `databricks_fetch` exports out of any directory you sync or back up
  automatically.
- Attach a restrictive cluster policy to the identity the agent uses, and pass its
  id to `cluster create`. A policy is the only spend limit the workspace can
  actually enforce; everything this plugin does is advisory by comparison.
- If you run `hermes databricks` from cron or CI, do **not** reflexively add
  `--yes` to compute commands. The non-interactive refusal is a safety net; wiring
  around it converts a mistake into a provisioned cluster.
- Uploaded libraries live in a Unity Catalog Volume you name. Grant `WRITE VOLUME`
  on a purpose-made volume (`… .default.hermes_libs`) rather than reusing one that
  holds data — a JAR path is also a data path.
