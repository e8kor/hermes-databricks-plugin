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
