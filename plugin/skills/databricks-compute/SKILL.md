---
name: databricks-compute
description: Run Spark code on Databricks clusters and job runs.
version: 1.0.0
author: Eugene Korniichuk
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [databricks, spark, clusters, jobs, libraries, compute]
    category: data
    related_skills: [databricks, databricks-troubleshooting]
---

# Databricks Compute Skill

Create and control Databricks clusters, upload and install libraries, and run
Spark code as job runs — then read the real verdict. Everything here is
CLI-only (`hermes databricks cluster|library|job`) and billable; there are no
model tools for compute, deliberately.

## When to Use

- Testing Apache Spark code against a real cluster.
- Installing a locally-built JAR or wheel so a cluster can use it.
- Starting, restarting, or stopping a cluster.
- Finding out why a submitted run failed.
- Checking what is currently running and billing.

## Prerequisites

- The `databricks` CLI on PATH and logged in — see `databricks-workspace-setup`.
- A UC Volume you can **write** to. Workspace-admin does NOT imply Unity
  Catalog privileges: on this workspace, `databricks_ws.default.blooms` is owned
  by another user and returns `User does not have READ VOLUME privilege`. Create
  your own once:
  `hermes databricks library create-volume databricks_ws default hermes_libs`
- For cluster creation: a policy id from `hermes databricks cluster policies`.

## Quick Reference

| Need | Command |
|---|---|
| What is running and billing? | `cluster list` |
| Cluster detail / why it died | `cluster show <id>` |
| Valid runtimes / node types | `cluster versions`, `cluster node-types` |
| Create a cluster | `cluster create <name> --spark-version .. --node-type .. --policy ..` |
| Start / restart / stop | `cluster start|restart|stop <id>` |
| Put a JAR where Spark can read it | `library upload <local> /Volumes/...` |
| Put a Python job script where Spark can read it | `databricks workspace import` → `/Workspace/...` |
| Install it on a cluster | `library install <id> --jar /Volumes/...` |
| Did the install actually work? | `library status <id>` |
| Run Spark code | `job submit --notebook ... ` or `--main-class` / `--python-file` |
| Why did the run fail? | `job show <run_id>`, `job output <run_id>` |
| What is still running? | `job list --active` |

## Procedure

1. **Prefer a job run over a long-lived cluster.** `job submit` with no
   `--cluster` creates an *ephemeral* job cluster that is torn down when the run
   ends — it cannot be forgotten and cannot bill overnight. Pay the ~5 minute
   startup. Only reuse a warm cluster (`--cluster <id>`) when iterating on the
   same code repeatedly, and stop it when you are done.

2. **JARs go to a UC Volume; Python entry-point scripts go to /Workspace.**
   This split is not stylistic — a `spark_python_task` cannot read a
   `python_file` from a Volume at all (see Pitfalls). So:
   - JARs / wheels → `library upload <local> /Volumes/<cat>/<schema>/<vol>/<file>`.
     The upload verifies the remote byte count against the local one and fails
     loudly on a mismatch; a partially-uploaded JAR is otherwise accepted by the
     API and then fails much later on an executor with an opaque class-loading
     error.
   - Python job scripts → `databricks workspace import --format AUTO --file
     job.py /Users/<you>/job.py`, then pass `/Workspace/Users/<you>/job.py`.

3. **For an ephemeral run, pass the JAR as a task library, not a cluster
   library.** `job submit --main-class com.x.Main --jar /Volumes/.../lib.jar`
   installs it before the task starts and disposes of it with the cluster. Do
   not create a cluster just to attach a library to it.

4. **For a reused cluster, install and then verify.** `library install` waits
   for the real verdict by default. If you pass `--no-wait`, you MUST follow up
   with `library status <id>` — see Pitfalls.

5. **Judge a run on `result_state`, never on it finishing.** `job show` prints
   `succeeded` explicitly. `life_cycle_state: TERMINATED` with
   `result_state: FAILED` is the normal shape of "your Spark code threw"; the
   stack trace is in `job output <run_id>`.

6. **Check for orphans when you are done.** `cluster list` flags every RUNNING
   cluster and `job list --active` shows runs still executing. An idle cluster
   with autotermination still bills until it fires.

## Pitfalls

- **A `spark_python_task` cannot load its `python_file` from a UC Volume.**
  Proven live across 3 failed runs plus a successful control: a `/Volumes/...`
  python_file fails with *"Cannot read the python file ... Either it does not
  exist, or the identity used to run this job ... lacks the required
  permissions"* — while you **own** the volume, hold explicit `READ_VOLUME` and
  `WRITE_VOLUME`, can list the file over the Files API, and the cluster runs as
  `SINGLE_USER` with that same identity. The identical file at
  `/Workspace/Users/<you>/<file>.py` succeeded on the same cluster spec. The
  message blames permissions; the real constraint is the path type. Import the
  script as a workspace file:
  `databricks workspace import --format AUTO --file job.py /Users/<you>/job.py`.
  `job submit` refuses a Volume python_file up front and says this, rather than
  letting you spend ~5 minutes of cluster startup on a misleading error.
  **JAR libraries from `/Volumes` DO work** — only `python_file` is restricted.
- **An ephemeral job cluster with no access mode cannot read Unity Catalog.**
  A job cluster created without `data_security_mode` gets no UC credential, so
  `job submit` defaults ephemeral runs to `SINGLE_USER` with your identity as
  owner. Note this was *not* the cause of the python_file failure above — it was
  my first theory and the retry disproved it. It is still the right default for
  UC access generally; pass `--security-mode` to override.
- **Workspace-admin ≠ Unity Catalog write.** Being in the `admins` group does
  not grant `WRITE VOLUME`. Uploading to someone else's volume fails with
  HTTP 403 / `does not have READ VOLUME privilege`. Create a volume you own;
  the error message from `library upload` says exactly this.
- **Owning a volume does not give you an explicit privilege assignment.**
  Reading `unity-catalog/permissions/volume/<fqn>` for a volume you created
  returned `{}` — ownership is separate from grants. Grant yourself
  `READ_VOLUME`/`WRITE_VOLUME` explicitly if something downstream needs them.
- **A `sql_task` needs a SAVED query id, not inline SQL.** Passing a statement
  fails with `Missing required field: query_id`. Ad-hoc SQL belongs in
  `databricks_query` / `databricks_fetch` — a job run is the wrong tool for it.
- **`libraries/install` returns HTTP 200 before installing anything.** The
  request is only queued; the library then moves `PENDING` → `INSTALLING` →
  `INSTALLED` or `FAILED`, and a failure appears ONLY in
  `libraries/cluster-status`. A wrapper that reports success on the 200 is
  wrong roughly whenever it matters. `library install` polls to a terminal
  state for this reason — do not "optimize" that away.
- **Installing onto a stopped cluster silently does nothing.** The API accepts
  it, and the library sits unapplied until the next start. `library install`
  refuses when the cluster is not RUNNING rather than accepting the no-op.
- **Uninstall needs a restart.** `library uninstall` marks the library for
  removal; the running JVM keeps it. Restart the cluster to actually drop it.
- **`autotermination_minutes: 0` means never terminate.** The API accepts it
  happily. `cluster create` refuses anything below 10 minutes — this is the
  single most expensive mistake available in this surface.
- **Single-node clusters need two spark_conf keys.** `spark.databricks.cluster.profile=singleNode`,
  `spark.master=local[*]`, plus the `ResourceClass: SingleNode` tag. Without
  them the cluster starts and then fails every task with a scheduling error.
  `--workers 0` sets all three for you.
- **A TERMINATED cluster cannot be restarted.** The API 400s. `cluster restart`
  falls through to a start, since the intent is the same either way.
- **The Files API uses `name`/`file_size`, not the legacy DBFS `path`/`size`.**
  Reading the wrong keys yields a listing of `None` entries that looks like an
  empty directory. (Observed live: `databricks fs ls --output json` returns
  nulls where `/api/2.0/fs/directories/...` returns real values.)
- **Serverless jobs compute cannot take custom JAR libraries.** If the code
  needs your own JAR, it needs a classic cluster — ephemeral or otherwise.
- **`runs/get-output` rejects a multi-task run's parent id.** Fetch output per
  *task* run id; `job output` reports which ids to use instead of surfacing a
  bare HTTP 400.

## Verification

```
# read-only, no spend
hermes databricks cluster list
hermes databricks cluster policies
hermes databricks cluster versions
hermes databricks cluster node-types --limit 5

# one-time: a volume you own
hermes databricks library create-volume databricks_ws default hermes_libs

# byte-verified upload round-trip
hermes databricks library upload ./target/mylib.jar \
    /Volumes/databricks_ws/default/hermes_libs/mylib.jar
hermes databricks library ls /Volumes/databricks_ws/default/hermes_libs
# expect: the same byte count as the local file

# a Python entry point must be a WORKSPACE file, not a Volume file
databricks workspace import --format AUTO --file job.py /Users/$USER_EMAIL/job.py

# cheapest real Spark run: ephemeral single node, dies on its own
hermes databricks job submit --name smoke \
    --spark-version 17.3.x-scala2.13 --node-type Standard_D4ds_v5 \
    --python-file /Workspace/Users/$USER_EMAIL/job.py
# expect: succeeded True. Takes ~5 min — most of it is cluster startup.
# verified live: rows=1000 max_sq=998001 on Spark 4.0.0, 29s execution.

# after any run
hermes databricks job show <run_id>      # expect succeeded: True
hermes databricks job list --active      # expect (none) when idle
hermes databricks cluster list           # expect no RUNNING warning
```
