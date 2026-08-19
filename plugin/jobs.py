"""Job runs: submit Spark work, poll it, and read the real outcome.

The important asymmetry in the Jobs API is that a run can *succeed* at the run
level while the task inside it failed.  ``state.life_cycle_state`` reaching
``TERMINATED`` only means "the run stopped"; the verdict lives in
``state.result_state`` (``SUCCESS`` / ``FAILED`` / ``CANCELED``) and, for a
multi-task run, in each task's own state.  A wrapper that polls life-cycle and
reports success is wrong exactly when it matters — when the Spark code threw.

Two submission shapes, both via ``jobs/runs/submit`` (a one-off run, no
persistent job definition left behind):

* **ephemeral** — ``new_cluster``: a job cluster created for this run and torn
  down when it ends.  Isolated and self-cleaning; costs ~5 minutes of startup.
* **existing** — ``existing_cluster_id``: reuse a warm all-purpose cluster.
  Fast for iteration; the cluster keeps billing until it autoterminates, which
  is the caller's problem and is called out in the returned payload.
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any, Dict, List, Optional

from .api import DatabricksAPIError, _request
from .compute import ClusterError

_RUN_TERMINAL_LIFECYCLE = {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}
_POLL_INTERVAL_SECONDS = 10.0

# Guardrail mirrored from compute.create_cluster: an ephemeral job cluster is
# torn down by the run, but a wedged run should not bill forever either.
_DEFAULT_JOB_TIMEOUT_SECONDS = 3600


class JobError(DatabricksAPIError):
    """A job run failed, or ended in a state the caller cannot use."""


# An ephemeral job cluster with no access mode gets NO Unity Catalog credential,
# so any task reading /Volumes/... fails with a message that blames a missing
# file.  SINGLE_USER is the mode that carries the submitting identity's UC
# grants, which is what a one-off test run wants.
_DEFAULT_UC_SECURITY_MODE = "SINGLE_USER"


def default_uc_security_mode() -> str:
    """The access mode an ephemeral run needs to read Unity Catalog volumes."""
    return _DEFAULT_UC_SECURITY_MODE


def _quoted(value: Any) -> str:
    return urllib.parse.quote(str(value), safe="")


def _current_user(workspace: Optional[str] = None) -> str:
    """The submitting principal, needed as SINGLE_USER's cluster owner."""
    body = _request(workspace, "/api/2.0/preview/scim/v2/Me", method="GET")
    return str(body.get("userName") or "")


def _new_cluster_spec(
    *,
    spark_version: str,
    node_type_id: str,
    num_workers: int = 0,
    policy_id: Optional[str] = None,
    data_security_mode: Optional[str] = None,
    single_user_name: Optional[str] = None,
    spark_conf: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Build a ``new_cluster`` block for an ephemeral job cluster.

    Single-node (``num_workers=0``) needs the same two spark_conf keys and the
    ``ResourceClass`` tag as an all-purpose single-node cluster; without them
    the run starts and then fails to schedule any task.

    ``data_security_mode`` is not cosmetic: a job cluster created without one
    gets NO Unity Catalog credential, so a task that reads
    ``/Volumes/...`` fails with "Cannot read the python file ... or the identity
    used to run this job lacks the required permissions" — which reads like a
    missing file but is an access-mode problem.  See
    :func:`default_uc_security_mode`.
    """
    conf: Dict[str, str] = dict(spark_conf or {})
    spec: Dict[str, Any] = {
        "spark_version": spark_version,
        "node_type_id": node_type_id,
    }
    if num_workers <= 0:
        conf.setdefault("spark.databricks.cluster.profile", "singleNode")
        conf.setdefault("spark.master", "local[*]")
        spec["num_workers"] = 0
        spec["custom_tags"] = {"ResourceClass": "SingleNode"}
    else:
        spec["num_workers"] = int(num_workers)
    if conf:
        spec["spark_conf"] = conf
    if policy_id:
        spec["policy_id"] = policy_id
    if data_security_mode:
        spec["data_security_mode"] = data_security_mode
    if single_user_name:
        spec["single_user_name"] = single_user_name
    return spec


def _libraries_block(
    jars: Optional[List[str]] = None,
    whls: Optional[List[str]] = None,
    pypi: Optional[List[str]] = None,
    maven: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Task-scoped libraries.

    For an ephemeral run this is the correct place for a JAR — task libraries
    are installed before the task starts and die with the cluster, avoiding the
    legacy cluster-scoped install path entirely.
    """
    out: List[Dict[str, Any]] = []
    for ref in jars or []:
        out.append({"jar": ref})
    for ref in whls or []:
        out.append({"whl": ref})
    for ref in pypi or []:
        out.append({"pypi": {"package": ref}})
    for ref in maven or []:
        out.append({"maven": {"coordinates": ref}})
    return out


def submit_run(
    *,
    run_name: str,
    workspace: Optional[str] = None,
    # exactly one compute choice
    existing_cluster_id: Optional[str] = None,
    spark_version: Optional[str] = None,
    node_type_id: Optional[str] = None,
    num_workers: int = 0,
    policy_id: Optional[str] = None,
    data_security_mode: Optional[str] = None,
    single_user_name: Optional[str] = None,
    spark_conf: Optional[Dict[str, str]] = None,
    # exactly one task type
    notebook_path: Optional[str] = None,
    notebook_params: Optional[Dict[str, str]] = None,
    spark_python_file: Optional[str] = None,
    python_params: Optional[List[str]] = None,
    main_class_name: Optional[str] = None,
    jar_params: Optional[List[str]] = None,
    sql_query_id: Optional[str] = None,
    warehouse_id: Optional[str] = None,
    # libraries
    jars: Optional[List[str]] = None,
    whls: Optional[List[str]] = None,
    pypi: Optional[List[str]] = None,
    maven: Optional[List[str]] = None,
    timeout_seconds: int = _DEFAULT_JOB_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Submit a one-off run and return ``{run_id, run_page_url, compute}``.

    Compute: pass ``existing_cluster_id`` to reuse a warm cluster, or
    ``spark_version`` + ``node_type_id`` to get an ephemeral job cluster.
    Passing both is rejected — silently preferring one would hide a real
    mistake about where the code ran (and what it costs).
    """
    if existing_cluster_id and (spark_version or node_type_id):
        raise JobError(
            "pass either existing_cluster_id (reuse a warm cluster) or "
            "spark_version+node_type_id (ephemeral job cluster), not both"
        )

    task: Dict[str, Any] = {"task_key": "main"}

    if existing_cluster_id:
        task["existing_cluster_id"] = existing_cluster_id
        compute = {"kind": "existing", "cluster_id": existing_cluster_id}
    elif spark_version and node_type_id:
        # Default to a UC-capable access mode.  Omitting it yields a cluster
        # with no Unity Catalog credential, whose failure message blames a
        # missing file rather than the access mode (observed live).
        mode = data_security_mode or _DEFAULT_UC_SECURITY_MODE
        owner = single_user_name
        if mode == "SINGLE_USER" and not owner:
            try:
                owner = _current_user(workspace)
            except DatabricksAPIError:
                owner = None
        task["new_cluster"] = _new_cluster_spec(
            spark_version=spark_version,
            node_type_id=node_type_id,
            num_workers=num_workers,
            policy_id=policy_id,
            data_security_mode=mode,
            single_user_name=owner,
            spark_conf=spark_conf,
        )
        compute = {
            "kind": "ephemeral",
            "spark_version": spark_version,
            "node_type_id": node_type_id,
            "num_workers": num_workers,
            "data_security_mode": mode,
        }
    elif sql_query_id:
        compute = {"kind": "warehouse", "warehouse_id": warehouse_id}
    else:
        raise JobError(
            "no compute specified: give existing_cluster_id, or "
            "spark_version+node_type_id, or a sql_query_id with warehouse_id"
        )

    chosen = [
        x for x in (notebook_path, spark_python_file, main_class_name, sql_query_id)
        if x
    ]
    if len(chosen) != 1:
        raise JobError(
            "specify exactly one task: notebook_path, spark_python_file, "
            "main_class_name (with a jar library), or sql_query_id"
        )

    if spark_python_file and spark_python_file.startswith("/Volumes/"):
        # OBSERVED LIVE (3 failed runs): a spark_python_task whose python_file
        # is a UC Volume path fails with "Cannot read the python file ... or the
        # identity used to run this job lacks the required permissions" even
        # when the caller OWNS the volume, holds explicit READ_VOLUME/
        # WRITE_VOLUME, can list the file over the Files API, and the cluster
        # runs as SINGLE_USER with that same identity.  The identical file at a
        # /Workspace/... path succeeds on the same cluster spec.  The message
        # blames permissions; the actual constraint is the path type.
        raise JobError(
            "spark_python_task cannot load a python_file from a UC Volume "
            f"({spark_python_file}). Import the file as a workspace file and "
            "pass a /Workspace/Users/<you>/<file>.py path instead. "
            "(Volume paths fail with a misleading permissions error even when "
            "you own the volume.) JAR libraries from /Volumes DO work."
        )

    if notebook_path:
        nb: Dict[str, Any] = {"notebook_path": notebook_path}
        if notebook_params:
            nb["base_parameters"] = dict(notebook_params)
        task["notebook_task"] = nb
    elif spark_python_file:
        py: Dict[str, Any] = {"python_file": spark_python_file}
        if python_params:
            py["parameters"] = list(python_params)
        task["spark_python_task"] = py
    elif main_class_name:
        if not (jars or maven):
            raise JobError(
                "main_class_name needs the class on the classpath: pass jars=[...] "
                "(a /Volumes/... path) or maven=[...]"
            )
        jar_task: Dict[str, Any] = {"main_class_name": main_class_name}
        if jar_params:
            jar_task["parameters"] = list(jar_params)
        task["spark_jar_task"] = jar_task
    else:
        if not warehouse_id:
            raise JobError("sql_query_id requires warehouse_id")
        # A sql_task references a SAVED query by id; the API rejects inline SQL
        # with "Missing required field: query_id" (observed live).  Ad-hoc SQL
        # belongs in databricks_query / databricks_fetch, not in a job run.
        task["sql_task"] = {
            "query": {"query_id": sql_query_id},
            "warehouse_id": warehouse_id,
        }

    libs = _libraries_block(jars=jars, whls=whls, pypi=pypi, maven=maven)
    if libs:
        if sql_query_id:
            raise JobError("a sql_task cannot carry libraries")
        task["libraries"] = libs

    payload: Dict[str, Any] = {
        "run_name": run_name,
        "tasks": [task],
        "timeout_seconds": int(timeout_seconds),
    }

    body = _request(workspace, "/api/2.2/jobs/runs/submit", payload)
    run_id = body.get("run_id")
    if not run_id:
        raise JobError(f"submit returned no run_id: {body}")
    out: Dict[str, Any] = {"run_id": run_id, "compute": compute}
    if compute["kind"] == "existing":
        out["billing_note"] = (
            "the reused cluster keeps billing after this run until it "
            "autoterminates or you stop it"
        )
    return out


def get_run(run_id: int | str, *, workspace: Optional[str] = None) -> Dict[str, Any]:
    """Run record, flattened to the fields that decide success or failure."""
    body = _request(
        workspace, f"/api/2.2/jobs/runs/get?run_id={_quoted(run_id)}", method="GET"
    )
    state = body.get("state") or {}
    tasks = []
    for t in body.get("tasks") or []:
        ts = t.get("state") or {}
        tasks.append({
            "task_key": t.get("task_key"),
            "life_cycle_state": ts.get("life_cycle_state"),
            "result_state": ts.get("result_state"),
            "state_message": ts.get("state_message"),
            "cluster_instance": (t.get("cluster_instance") or {}).get("cluster_id"),
            "run_id": t.get("run_id"),
        })
    return {
        "run_id": body.get("run_id"),
        "run_name": body.get("run_name"),
        "life_cycle_state": state.get("life_cycle_state"),
        "result_state": state.get("result_state"),
        "state_message": state.get("state_message"),
        "run_page_url": body.get("run_page_url"),
        "start_time": body.get("start_time"),
        "end_time": body.get("end_time"),
        "tasks": tasks,
    }


def wait_for_run(
    run_id: int | str,
    *,
    workspace: Optional[str] = None,
    timeout_seconds: float = 3600.0,
) -> Dict[str, Any]:
    """Poll until the run stops, then judge it on ``result_state``.

    A run that stops is not a run that worked: ``life_cycle_state=TERMINATED``
    with ``result_state=FAILED`` is the normal shape of "your Spark code threw".
    This returns the record either way and marks ``succeeded`` explicitly so no
    caller has to re-derive the distinction.
    """
    deadline = time.monotonic() + timeout_seconds
    record: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        record = get_run(run_id, workspace=workspace)
        life = str(record.get("life_cycle_state") or "")
        if life in _RUN_TERMINAL_LIFECYCLE:
            result = str(record.get("result_state") or "")
            record["succeeded"] = result == "SUCCESS"
            if not record["succeeded"]:
                record["failure_summary"] = (
                    record.get("state_message")
                    or "; ".join(
                        str(t.get("state_message"))
                        for t in record.get("tasks") or []
                        if t.get("state_message")
                    )
                    or f"run ended {life}/{result or 'no result_state'}"
                )
            return record
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise JobError(
        f"run {run_id} still {record.get('life_cycle_state')} after "
        f"{timeout_seconds:g}s (cancel it with jobs.cancel_run)"
    )


def run_output(run_id: int | str, *, workspace: Optional[str] = None) -> Dict[str, Any]:
    """Task output: notebook result, logs tail, and any error with its trace.

    For a multi-task run the Jobs API rejects ``runs/get-output`` on the parent
    run id and requires a *task* run id, so the caller is told which ids to use
    rather than getting a bare HTTP 400.
    """
    try:
        body = _request(
            workspace,
            f"/api/2.2/jobs/runs/get-output?run_id={_quoted(run_id)}",
            method="GET",
        )
    except DatabricksAPIError as exc:
        if "multi-task" in str(exc).lower() or "task run" in str(exc).lower():
            record = get_run(run_id, workspace=workspace)
            task_ids = [t.get("run_id") for t in record.get("tasks") or []]
            raise JobError(
                f"run {run_id} is a multi-task run; fetch output per task run id: "
                f"{task_ids}"
            ) from exc
        raise

    err = body.get("error")
    return {
        "run_id": run_id,
        "notebook_output": (body.get("notebook_output") or {}).get("result"),
        "truncated": bool((body.get("notebook_output") or {}).get("truncated")),
        "logs": body.get("logs"),
        "logs_truncated": bool(body.get("logs_truncated")),
        "error": err,
        "error_trace": body.get("error_trace"),
    }


def cancel_run(run_id: int | str, *, workspace: Optional[str] = None) -> Dict[str, Any]:
    """Cancel a running run (stops an ephemeral cluster's billing)."""
    _request(workspace, "/api/2.2/jobs/runs/cancel", {"run_id": int(run_id)})
    return {"run_id": run_id, "cancel_requested": True}


def list_runs(
    *,
    workspace: Optional[str] = None,
    limit: int = 20,
    active_only: bool = False,
) -> List[Dict[str, Any]]:
    """Recent runs, newest first.  ``active_only`` finds what is still billing."""
    path = f"/api/2.2/jobs/runs/list?limit={max(1, min(int(limit), 25))}"
    if active_only:
        path += "&active_only=true"
    body = _request(workspace, path, method="GET")
    out = []
    for r in body.get("runs") or []:
        state = r.get("state") or {}
        out.append({
            "run_id": r.get("run_id"),
            "run_name": r.get("run_name"),
            "life_cycle_state": state.get("life_cycle_state"),
            "result_state": state.get("result_state"),
            "start_time": r.get("start_time"),
            "run_page_url": r.get("run_page_url"),
        })
    return out


def submit_and_wait(
    *,
    workspace: Optional[str] = None,
    poll_timeout_seconds: float = 3600.0,
    **submit_kwargs: Any,
) -> Dict[str, Any]:
    """Submit a run, wait for it, and attach its output in one call.

    On failure the output (error + trace) is attached rather than raising,
    because a failed Spark job's stack trace IS the answer the caller wanted.
    An infrastructure problem — cluster never started, run wedged — still
    raises, since there is nothing to read.
    """
    submitted = submit_run(workspace=workspace, **submit_kwargs)
    run_id = submitted["run_id"]
    try:
        record = wait_for_run(
            run_id, workspace=workspace, timeout_seconds=poll_timeout_seconds
        )
    except (JobError, ClusterError):
        raise

    try:
        record["output"] = run_output(run_id, workspace=workspace)
    except JobError as exc:
        record["output_error"] = str(exc)
    record["compute"] = submitted["compute"]
    if "billing_note" in submitted:
        record["billing_note"] = submitted["billing_note"]
    return record
