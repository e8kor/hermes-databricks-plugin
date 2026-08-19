# Contributing

Thanks for looking. This is a small, focused plugin — the bar for changes is
"does it make the plugin lie less, or cost the user less context/money".

## Ground rules

- **No third-party dependencies.** The plugin is standard library only, on
  purpose: it installs into someone's Hermes profile as a directory, and a
  `pip install` step there is a support burden. HTTP is `urllib.request`, JSON is
  `json`, CSV is `csv`. If you think you need a dependency, open an issue first.
- **Never claim an outcome you didn't observe.** The Databricks APIs are
  asynchronous in ways that make naive wrappers lie — `clusters/start` returns
  200 while the cluster sits `PENDING`, `libraries/install` returns 200 before
  anything downloads, and a job run can reach `TERMINATED` with a failed task
  inside. Poll to a terminal state, or return the raw observed state and say so.
- **Rows are expensive context.** Anything that can return an unbounded result
  must either cap and say it capped, or write to disk and return a summary.
- **Secrets never reach an error message.** A credential helper can print a token
  on failure and may embed a secret in its own arguments. Errors say "run the
  command yourself"; they don't echo stdout, stderr, or the command string.
- **Config, not `.env`.** `.env` holds secret *values* only. Everything
  behavioural goes in `config.yaml` under `databricks:`. This mirrors the Hermes
  core policy.
- **Profile-aware paths.** Use `get_hermes_home()`; never hardcode `~/.hermes`.
  Each Hermes profile has its own home and its own export directory.

## Development setup

```bash
git clone https://github.com/e8kor/hermes-databricks-plugin.git
cd hermes-databricks-plugin
./install.sh --symlink     # $HERMES_HOME/plugins/databricks -> ./plugin
hermes plugins enable databricks
```

With `--symlink`, the installed plugin *is* the repo working tree — editing a file
here edits the live plugin. Restart Hermes (or start a new session) to pick up
Python changes.

Run the tests with the interpreter Hermes itself uses, so you're testing against
the same standard library:

```bash
python -m pytest tests/ -q
```

## Tests

- **stdlib + pytest + `unittest.mock` only. No network.** The API layer is
  exercised against recorded Databricks response shapes; if you add a code path
  that talks to Databricks, add a recorded shape for it in `tests/`.
- **Assert behaviour, not snapshots.** A test that freezes a current value (a
  hardcoded list of Databricks node types, an exact count of registered tools)
  breaks on every routine update and covers nothing. Assert the relationship:
  "every workspace resolved from a profile ends up with a host", "a truncated
  inline result surfaces a warning and points at `databricks_fetch`".
- **Never read source files in a test.** Regexing `api.py` for a call site tests
  the shape of the source, not its behaviour. Extract the logic into a callable
  function and call it.

Tests are loaded via `tests/conftest.py`, which imports `plugin/` as the package
`databricks_plugin` — so tests don't require Hermes to be installed.

## Pull requests

Keep the diff to the thing being fixed. Specifically:

1. **One concern per PR.** Drive-by reformatting and renames make a fix
   unreviewable.
2. **Say what you observed.** For a bug fix: the symptom, the exact line where it
   manifests, and why the fix changes that line's behaviour. "Plausible-sounding"
   is not the standard — the Databricks API surface punishes assumptions.
3. **Update the docs in the same PR.** If you change a tool's parameters, update
   `docs/tools.md`; a CLI flag, `docs/cli.md`; a config key,
   `docs/configuration.md`. Add a `CHANGELOG.md` entry under `Unreleased`.
4. **Bump nothing.** Version bumps in `plugin/plugin.yaml` and
   `plugin/__init__.py` happen at release time, not per PR.
5. **CI must pass.** The workflow runs the test suite on Python 3.9–3.12 and
   compiles every module; both are cheap and both catch real breakage.

Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`,
`test:`, `refactor:`, `chore:`).

## Adding a tool

A new model tool is the most expensive kind of change here — its schema ships on
every API call the agent makes, for every user, whether or not they use it. Before
adding one, check whether the capability is reachable through an existing tool, the
CLI, or a skill.

If it genuinely needs to be a tool:

1. Add the schema to `plugin/schemas.py` (this is what the model reads — write the
   description for a model choosing between tools, and say when *not* to use it).
2. Add the handler to `plugin/tools.py`. Handlers return a JSON **string** for both
   success and failure, accept `**kwargs`, and never raise.
3. Register it in `plugin/__init__.py`. Gate it with `check_fn=_check_configured`
   unless it is a diagnostic that must survive a broken config.
4. Add it to `provides_tools` in `plugin/plugin.yaml`, to `docs/tools.md`, and to
   the tool table in `README.md`.

## Adding a skill

Skills live in `plugin/skills/<name>/SKILL.md` and are registered in `_SKILLS` in
`plugin/__init__.py`. Keep the frontmatter `description` to one sentence under 60
characters, and reference native Hermes tools by name (`terminal`, `read_file`,
`databricks_fetch`) rather than shell utilities the agent already has wrapped.

## Reporting bugs

Use the issue templates. The single most useful thing you can attach is the output
of:

```bash
hermes databricks status
```

It reports workspaces, the auth mechanism in use, visible CLI profiles, and
whether a credential actually resolves — and it deliberately keeps working when
everything else is broken. Redact the host if you need to; don't redact the
`auth` field, that's usually the answer.
