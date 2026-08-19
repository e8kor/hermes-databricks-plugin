## What this changes

<!-- One paragraph. What behaviour is different after this PR. -->

## Why

<!--
For a bug fix: the symptom, the exact file:line where it manifests, and why this
change alters that line's behaviour. For a feature: the problem it solves and why
it belongs on this surface (tool / CLI / skill).
-->

## How it was verified

<!--
Real output, not a description of output. Recorded response shapes for new API
paths, and the actual command you ran against a workspace if applicable.
-->

```
$ python -m pytest tests/ -q

```

## Checklist

- [ ] No new third-party Python dependency (the plugin is standard library only)
- [ ] Tests added or updated; they assert behaviour, not snapshots, and make no network calls
- [ ] No credential, token, or helper stdout/stderr can reach an error message
- [ ] Unbounded results go to disk, or are capped and say they were capped
- [ ] Paths use `get_hermes_home()`; no hardcoded `~/.hermes`
- [ ] Docs updated in this PR (`docs/tools.md` / `docs/cli.md` / `docs/configuration.md`)
- [ ] `CHANGELOG.md` entry added under `Unreleased`
- [ ] No version bump (that happens at release time)
