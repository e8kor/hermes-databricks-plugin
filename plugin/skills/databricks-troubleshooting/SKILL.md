---
name: databricks-troubleshooting
description: Diagnose Databricks auth, permission, and query failures.
version: 1.0.0
author: Eugene Korniichuk
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [databricks, troubleshooting, auth, permissions, errors]
    category: data
    related_skills: [databricks, databricks-workspace-setup]
---

# Databricks Troubleshooting Skill

Turn a failing Databricks call into a specific cause. Each symptom below has a
distinct fix; the goal is to stop guessing and stop retrying the same call.
Covers auth, permission, warehouse and result-size failures.

## When to Use

- Any Databricks tool returned `success: false`.
- Tools are missing from the toolset entirely.
- A query worked yesterday and fails today.
- Results look smaller than expected.

## Prerequisites

- `databricks_status` — always the first call. It separates a config/auth problem
  from a query problem and lists the CLI profiles it can see.
- The `databricks` CLI, for cross-checking what Hermes sees against the CLI's own
  view.

## Quick Reference

| Symptom | Cause | Fix |
|---|---|---|
| Only `databricks_status` exists | no workspace resolves | log in / configure |
| `key_cmd ... exited 1` | CLI OAuth session expired | `databricks auth login` |
| `HTTP 403 PERMISSION_DENIED` | missing UC grant | ask a data owner |
| `HTTP 401` | bad or stale token | re-login; check `token_env` |
| `profile ... does not exist` | config names a dead profile | fix name or re-login |
| `too_large_for_inline` | >25 MiB inline | use `databricks_fetch` |
| `truncated_by_platform` | rows silently dropped | use `databricks_fetch` |
| First query takes ~30s | cold warehouse | wait; it is not a hang |
| `column_count: 0` | UC lazy metadata | describe the table by name |

## Procedure

1. **Run `databricks_status` and read three things:** whether a workspace
   resolved, what `auth` says, and whether the credential check passed. A missing
   `verification` block with `token_resolved: false` is a credential problem, not
   a query problem.

2. **If only `databricks_status` is available**, no workspace resolves, so the
   other tools are gated off. `status` stays reachable precisely because it is the
   diagnostic. Follow its `hint`, or see `databricks-workspace-setup`.

3. **For a `key_cmd` failure, run the command by hand** with `terminal`:
   `databricks auth token -p <profile>`. The plugin deliberately never echoes a
   helper's output (it can contain a token), so the real error is only visible
   directly. An expired OAuth session is the usual cause:
   `databricks auth login --host <url>`.

4. **Distinguish 401 from 403.** 401 means the credential is wrong or stale —
   re-authenticate. 403 with `PERMISSION_DENIED` means the credential is fine but
   the principal lacks a grant; the message names the exact missing privilege and
   object (e.g. `User does not have USE SCHEMA on Schema 'system.billing'`). No
   amount of re-login fixes a 403 — someone must grant it.

5. **Cross-check the CLI when Hermes and your expectations disagree.**
   `databricks auth describe -p <profile>` shows the CLI's resolved auth type,
   host, and where each setting came from — including env-var overrides like
   `DATABRICKS_CONFIG_PROFILE` that can silently repoint a profile.

6. **For anything about missing rows, treat size as the first suspect.** Check
   `total_rows_available` against `row_count`, and look for
   `truncated_by_platform` or `capped_by_max_rows`. Raising `max_rows` is only
   correct up to the display cap — past that, switch to `databricks_fetch`.

7. **For a warehouse error, confirm one exists and is reachable:**
   `hermes databricks profiles --warehouses`. If the configured `warehouse_id`
   was deleted, clear it and let auto-discovery pick a RUNNING one.

## Pitfalls

- **Do not retry the same failing call.** A 403, a missing profile and an
  oversized inline result are all permanent until something changes. Retrying
  wastes a warehouse spin-up and clarifies nothing.
- **Raising `max_rows` cannot fix `too_large_for_inline`.** The 25 MiB ceiling is
  enforced by the platform, not the plugin. Use `databricks_fetch`.
- **`success: true` does not mean "complete".** A truncated inline result is
  reported as SUCCEEDED by Databricks. Always read the `warning` field.
- **`column_count: 0` is not a permission error.** Unity Catalog populates column
  metadata lazily for some table kinds; describe the table by name.
- **A stale profile list can mislead you** for up to 5 minutes after a login.
  Use `hermes databricks profiles --refresh`.
- **Never paste a token into `config.yaml`.** If a credential is needed
  statically, put it in the profile's `.env` and reference it with
  `auth.token_env`.

## Verification

```
hermes databricks status                       # resolved workspace + live check
databricks auth describe -p <profile> -o json  # the CLI's own resolution
databricks auth token -p <profile>             # see the real minting error
hermes databricks profiles --refresh --warehouses
hermes databricks query "SELECT 1 AS ok"       # cheapest end-to-end proof
```
