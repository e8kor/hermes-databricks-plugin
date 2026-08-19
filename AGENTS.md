# Agent instructions — hermes-databricks-plugin

Shared entry point for AI assistants working in this repository. Keep it
project-specific and safe to publish: no credentials, workspace URLs, machine
paths, or local-only notes.

## Read first

1. `README.md` — what the plugin is and why it exists rather than the managed MCP server
2. `CONTRIBUTING.md` — the rules that get a change merged
3. `docs/architecture.md` — module map, request lifecycle, design invariants
4. `CHANGELOG.md` — what shipped, and what is mid-flight under "In development"

Then the reference matching your task:

- `docs/configuration.md` — the three config shapes, resolution order
- `docs/tools.md` — every tool's parameters, response shape, failure modes
- `docs/cli.md` — `hermes databricks` and `/databricks` verbs
- `docs/skills.md` — the bundled skills and how to add one
- `docs/troubleshooting.md` — symptom → cause table
- `SECURITY.md` — credential handling and the threat model

## Repo layout

`plugin/` is the single source of truth. The live plugin at
`$HERMES_HOME/plugins/databricks` is a **symlink** to it, so editing a file here
edits the running plugin.

| Path | Responsibility |
|---|---|
| `plugin/__init__.py` | `register(ctx)` — wires tools, slash command, CLI, skills |
| `plugin/schemas.py` | Tool schemas: what the model reads when choosing a tool |
| `plugin/tools.py` | Tool handlers. JSON string in, JSON string out, never raise |
| `plugin/api.py` | Databricks REST: statements, chunk streaming, Unity Catalog |
| `plugin/config.py` | Workspace resolution, token minting, token cache |
| `plugin/profiles.py` | Databricks CLI profile discovery, warehouse listing |
| `plugin/cli.py` | The `hermes databricks` argparse tree |
| `plugin/slash.py` | `/databricks` dispatch and human-facing table renderers |
| `plugin/skills/<name>/SKILL.md` | Bundled skills, registered as `databricks:<name>` |
| `tests/` | pytest, stdlib only, no network |

Dependency direction is one-way and should stay that way:
`profiles ← config ← api ← tools ← __init__`, with `cli` and `slash` on top of
`tools`. Nothing lower imports anything higher.

## Verification

```bash
python -m pytest tests/ -q          # no network; recorded response shapes
python -m compileall -q plugin      # must pass on the declared 3.9 floor
```

CI runs both plus an `install.sh` smoke test. Run the tests before opening a PR;
they are fast enough that there is no excuse not to.

For a live check against a real workspace, `hermes databricks status` is the
diagnostic — it reports the auth mechanism and whether a credential actually
resolves.

## Hard rules

These are invariants, not preferences. Breaking one makes the plugin lie.

1. **Standard library only.** No third-party imports, ever. HTTP is
   `urllib.request`, JSON is `json`, CSV is `csv`. The plugin installs as a
   directory into someone's Hermes profile; a `pip install` step there is a
   support burden. If you think you need a dependency, open an issue first.
2. **Handlers return a JSON string and never raise** — success and failure alike.
   They accept `**kwargs`.
3. **`databricks_status` is never config-gated.** It is the diagnostic and must
   work precisely when everything else is broken. The other three tools are gated
   on `check_fn=_check_configured`, so they vanish rather than fail confusingly.
4. **Never claim an outcome you did not observe.** Databricks APIs return 200 long
   before the work is done, and a statement can report `SUCCEEDED` while dropping
   rows. Poll to a terminal state, or return the raw observed state and say so.
5. **`truncated: true` is data loss, not success.** Surface it as a warning and
   point at `databricks_fetch`.
6. **Unbounded results go to disk.** Or are capped and say they were capped. Rows
   are expensive context — a wide result serialized into the conversation is what
   actually breaks a session.
7. **No credential, helper stdout/stderr, or command string in an error message.**
   A `key_cmd` can print a token on failure and may embed a secret in its own
   arguments. Errors report the exit code and say "run it yourself".
8. **Paths go through `get_hermes_home()`.** Never a hardcoded `~/.hermes` — each
   Hermes profile owns its own exports and state.
9. **Behavioural config in `config.yaml` under `databricks:`, secret values in
   `.env`.** This mirrors Hermes core policy. `.env` holds secrets only.

## Adding capability

A new **model tool** is the most expensive change here: its schema ships on every
API call the agent makes, for every user, whether they touch Databricks that turn
or not. Prefer, in order: extend an existing tool's parameters → a CLI subcommand
→ a skill → a new tool.

If it genuinely needs to be a tool:

1. Schema in `plugin/schemas.py`. Write the description for a model choosing
   between tools, and say when **not** to use it.
2. Handler in `plugin/tools.py`, per rule 2 above.
3. Register in `plugin/__init__.py`, gated with `check_fn=_check_configured`
   unless it is a diagnostic.
4. Add to `provides_tools` in `plugin/plugin.yaml`, to `docs/tools.md`, and to the
   tool table in `README.md`.

Skills go in `plugin/skills/<name>/SKILL.md` and get added to `_SKILLS` in
`plugin/__init__.py`. Keep the frontmatter `description` to one sentence under 60
characters. A skill that fails to register is skipped silently, so a typo in
`_SKILLS` shows up as a skill quietly not existing — check `hermes skills list`.

## Tests

- **stdlib + pytest + `unittest.mock`. No network.** The API layer runs against
  recorded Databricks response shapes; add a shape for any new remote path.
- **Assert behaviour, not snapshots.** A test that freezes a current value (a list
  of node types, a count of registered tools) breaks on every routine update and
  covers nothing. Assert the relationship instead.
- **Never read source files in a test.** Regexing `api.py` for a call site tests
  the shape of the source, not its behaviour. Extract the logic into a callable
  and call it.
- `tests/conftest.py` imports `plugin/` as the package `databricks_plugin`, so the
  suite does not require Hermes to be installed.

## Working on the live plugin

```bash
./install.sh --symlink    # $HERMES_HOME/plugins/databricks -> ./plugin
```

Python changes need a Hermes restart to take effect; skills and docs do not.

**Do not run a bare `./install.sh` in a symlinked dev checkout** unless you mean
to leave dev mode — it replaces the symlink with a copy, and from then on your
edits here stop reaching the running plugin. This repo's `install.sh` backs the
old target up rather than deleting it, so recovery is possible, but the divergence
is silent until something confuses you.

## Contribution style

- One concern per PR. Drive-by reformatting and renames make a fix unreviewable.
- For a bug fix, state the symptom, the exact `file:line` where it manifests, and
  why the change alters that line's behaviour. The Databricks API surface punishes
  assumptions; "plausible-sounding" is not the standard.
- Update the matching doc in the same PR, and add a `CHANGELOG.md` entry under
  `Unreleased`.
- Do not bump versions in `plugin/plugin.yaml` / `plugin/__init__.py`; that
  happens at release time.
- Conventional Commits: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.

## Things that will surprise you

Each of these is worked around in code because it was observed, not assumed:

- **Unity Catalog list endpoints are paginated.** Reading only the first page
  reports a partial catalog as complete. `samples.tpcds_sf1` at `max_results=3`
  needs 8 pages for 24 tables.
- **`columns: []` from Unity Catalog is not "no columns".** For
  `TABLE_DELTASHARING` securables (all of `samples.tpch`, `samples.tpcds_*`) UC
  returns no column metadata even on a single-table GET. `describe_table` falls
  back to `DESCRIBE TABLE`.
- **Presigned chunk URLs must be fetched without an `Authorization` header.** They
  carry auth in the query string; adding a header makes S3/ADLS reject them.
- **`urllib.parse.quote` defaults to `safe="/"`,** which silently corrupts a
  schema name or page token containing a slash. Use `_q` (`safe=""`).
- **`databricks auth profiles` takes seconds,** because it contacts every
  workspace to validate. Listings cache for 300s; `--refresh` after a login.
- **Account-console hosts (`accounts.*`) have no SQL warehouses** and are skipped
  when auto-picking a profile.
