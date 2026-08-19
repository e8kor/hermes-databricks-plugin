# Bundled skills

The plugin ships skills as documentation the agent loads **on demand**, registered
as `databricks:<name>`. They are not in the system prompt — the agent loads one
with `skill_view` when the task calls for it, so they cost nothing until used.

```
skill_view(name="databricks:databricks")
```

| Skill | Load it when |
|---|---|
| `databricks` | Entry point. Any Databricks work — it routes to the others |
| `databricks-workspace-setup` | Connecting, multi-workspace, CI or service principals |
| `databricks-sql-authoring` | Writing SQL: dialect, exploration order, cost control |
| `databricks-bulk-export` | The result is large, or headed for on-disk analysis |
| `databricks-compute` | Clusters, libraries, running Spark code as a job |
| `databricks-troubleshooting` | Something failed and the cause isn't obvious |

Each declares `platforms: [linux, macos, windows]`, `category: data`, and
`related_skills`, so the agent can navigate between them.

## databricks

The entry point. Its job is routing: choosing the right tool for the expected
result size, and knowing that `databricks_status` is the first move when anything
misbehaves. If you only ever load one, it's this.

## databricks-compute

Clusters, library upload/install, and Spark job runs — the billable surface.
Load it before touching compute, because the failure modes here are unusually
misleading: `libraries/install` returns HTTP 200 before installing anything, a
finished run is not a successful run, and a `spark_python_task` cannot read its
`python_file` from a UC Volume (the platform blames permissions instead). Also
covers the spend guardrails and how to spot orphaned running clusters.

## databricks-workspace-setup

The three config shapes and how they compose — adopting the CLI's default profile,
pinning a profile, and spelling out host plus auth for a machine with no CLI
profile. Covers the resolution order and multi-workspace selection.

Reference form of the same material: [configuration.md](configuration.md).

## databricks-sql-authoring

Databricks SQL dialect notes, the exploration order that avoids expensive
mistakes (catalog metadata before `SELECT *`), and cost control — because a
warehouse bills for the query you didn't mean to run.

## databricks-bulk-export

The export-then-analyze loop: `databricks_fetch` writes to disk, then `terminal` or
`execute_code` processes the file. The point is that the rows never enter the
context window; only the summary and the path do.

## databricks-troubleshooting

A symptom → cause table for auth, permission, and size failures. Overlaps with
[troubleshooting.md](troubleshooting.md) — the skill is written for the agent
mid-task, the doc for you reading it deliberately.

## Adding a skill

1. Create `plugin/skills/<name>/SKILL.md` with frontmatter: `name`,
   `description` (one sentence, under 60 characters), `version`, `author`,
   `license`, `platforms`, and `metadata.hermes.{tags,category,related_skills}`.
2. Add the directory name to `_SKILLS` in `plugin/__init__.py`.
3. Reference native Hermes tools by name (`terminal`, `read_file`,
   `databricks_fetch`) rather than shell utilities the agent already has wrapped —
   `grep` → `search_files`, `cat` → `read_file`, `sed` → `patch`.
4. Add it to the table above and in `README.md`.

A skill that fails to register never takes the tools down with it: registration is
wrapped, and a missing `SKILL.md` is skipped silently. That means a typo in
`_SKILLS` shows up as a skill quietly not existing — check
`hermes skills list` after adding one.
