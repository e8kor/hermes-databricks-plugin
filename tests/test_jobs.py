"""Job runs: submission shapes, and the succeed-vs-finish distinction.

The load-bearing test here is that a run which *finished* is not a run that
*worked* — `life_cycle_state=TERMINATED` with `result_state=FAILED` is the
normal shape of "your Spark code threw", and it must never be reported as
success.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def jobs(plugin_pkg):
    import importlib

    return importlib.import_module("databricks_plugin.jobs")


def _run_body(life, result=None, **extra):
    body = {
        "run_id": 42,
        "run_name": "test",
        "state": {"life_cycle_state": life, "result_state": result},
        "run_page_url": "https://h/#job/1/run/42",
        "tasks": [
            {
                "task_key": "main",
                "run_id": 43,
                "state": {"life_cycle_state": life, "result_state": result},
            }
        ],
    }
    body.update(extra)
    return body


# ─── compute selection: ambiguity must be an error, not a silent preference ──

def test_submit_rejects_both_compute_shapes_at_once(jobs):
    """Silently preferring one would hide where the code ran and what it cost."""
    with pytest.raises(jobs.JobError, match="not both"):
        jobs.submit_run(
            run_name="x",
            existing_cluster_id="c-1",
            spark_version="17.3.x-scala2.13",
            node_type_id="Standard_D4ds_v5",
        )


def test_submit_rejects_no_compute_at_all(jobs):
    with pytest.raises(jobs.JobError, match="no compute specified"):
        jobs.submit_run(run_name="x", notebook_path="/n")


def test_ephemeral_submission_builds_a_new_cluster_block(jobs, monkeypatch):
    captured = {}

    def fake_request(workspace, path, payload=None, method="POST"):
        captured.update(payload or {})
        return {"run_id": 42}

    monkeypatch.setattr(jobs, "_request", fake_request)
    out = jobs.submit_run(
        run_name="x", spark_version="17.3.x-scala2.13",
        node_type_id="Standard_D4ds_v5", notebook_path="/n",
    )
    task = captured["tasks"][0]
    assert "new_cluster" in task
    assert "existing_cluster_id" not in task
    assert out["compute"]["kind"] == "ephemeral"
    # An ephemeral cluster dies with the run, so no billing warning is needed.
    assert "billing_note" not in out


def test_existing_cluster_submission_warns_about_continued_billing(jobs, monkeypatch):
    monkeypatch.setattr(jobs, "_request", lambda *a, **k: {"run_id": 42})
    out = jobs.submit_run(
        run_name="x", existing_cluster_id="c-1", notebook_path="/n"
    )
    assert out["compute"]["kind"] == "existing"
    assert "billing" in out["billing_note"]


def test_ephemeral_run_defaults_to_a_uc_capable_access_mode(jobs, monkeypatch):
    """Regression: observed live.

    An ephemeral job cluster created with no data_security_mode gets NO Unity
    Catalog credential, and a task reading /Volumes/... then fails with
    "Cannot read the python file ... lacks the required permissions" — which
    reads like a missing file rather than an access-mode problem.
    """
    captured = {}

    def fake_request(workspace, path, payload=None, method="POST"):
        if "scim" in path:
            return {"userName": "me@example.com"}
        captured.update(payload or {})
        return {"run_id": 42}

    monkeypatch.setattr(jobs, "_request", fake_request)
    out = jobs.submit_run(
        run_name="x", spark_version="17.3.x-scala2.13",
        node_type_id="Standard_D4ds_v5",
        spark_python_file="/Workspace/Users/me/job.py",
    )
    cluster = captured["tasks"][0]["new_cluster"]
    assert cluster["data_security_mode"] == "SINGLE_USER"
    # SINGLE_USER needs an owner or the cluster has no identity to act as.
    assert cluster["single_user_name"] == "me@example.com"
    assert out["compute"]["data_security_mode"] == "SINGLE_USER"


def test_explicit_security_mode_is_not_overridden(jobs, monkeypatch):
    captured = {}

    def fake_request(workspace, path, payload=None, method="POST"):
        captured.update(payload or {})
        return {"run_id": 42}

    monkeypatch.setattr(jobs, "_request", fake_request)
    jobs.submit_run(
        run_name="x", spark_version="17.3.x-scala2.13",
        node_type_id="Standard_D4ds_v5", data_security_mode="USER_ISOLATION",
        notebook_path="/n",
    )
    cluster = captured["tasks"][0]["new_cluster"]
    assert cluster["data_security_mode"] == "USER_ISOLATION"
    # USER_ISOLATION is not single-user, so no owner should be attached.
    assert "single_user_name" not in cluster


def test_ephemeral_single_node_run_gets_the_required_spark_conf(jobs, monkeypatch):
    captured = {}

    def fake_request(workspace, path, payload=None, method="POST"):
        captured.update(payload or {})
        return {"run_id": 42}

    monkeypatch.setattr(jobs, "_request", fake_request)
    jobs.submit_run(
        run_name="x", spark_version="17.3.x-scala2.13",
        node_type_id="Standard_D4ds_v5", num_workers=0, notebook_path="/n",
    )
    cluster = captured["tasks"][0]["new_cluster"]
    assert cluster["spark_conf"]["spark.master"] == "local[*]"
    assert cluster["custom_tags"]["ResourceClass"] == "SingleNode"


# ─── task selection ─────────────────────────────────────────────────────────

def test_submit_requires_exactly_one_task_type(jobs):
    with pytest.raises(jobs.JobError, match="exactly one task"):
        jobs.submit_run(
            run_name="x", existing_cluster_id="c-1",
            notebook_path="/n", spark_python_file="/p.py",
        )


def test_submit_requires_a_task_at_all(jobs):
    with pytest.raises(jobs.JobError, match="exactly one task"):
        jobs.submit_run(run_name="x", existing_cluster_id="c-1")


def test_jar_task_without_a_jar_on_the_classpath_is_rejected(jobs):
    """main_class_name with no jar/maven would fail on the cluster, not here."""
    with pytest.raises(jobs.JobError, match="classpath"):
        jobs.submit_run(
            run_name="x", existing_cluster_id="c-1", main_class_name="com.x.Main"
        )


def test_jar_task_accepts_a_volume_jar(jobs, monkeypatch):
    captured = {}

    def fake_request(workspace, path, payload=None, method="POST"):
        captured.update(payload or {})
        return {"run_id": 42}

    monkeypatch.setattr(jobs, "_request", fake_request)
    jobs.submit_run(
        run_name="x", existing_cluster_id="c-1", main_class_name="com.x.Main",
        jars=["/Volumes/a/b/c/lib.jar"], jar_params=["--flag", "v"],
    )
    task = captured["tasks"][0]
    assert task["spark_jar_task"]["main_class_name"] == "com.x.Main"
    assert task["spark_jar_task"]["parameters"] == ["--flag", "v"]
    assert task["libraries"] == [{"jar": "/Volumes/a/b/c/lib.jar"}]


def test_volume_python_file_is_rejected_with_the_real_cause(jobs):
    """Regression: proven live across 3 failed runs + 1 successful control.

    A spark_python_task with a /Volumes/... python_file fails with a
    permissions message even when the caller owns the volume and holds explicit
    READ_VOLUME. The same file at /Workspace/... succeeds on an identical
    cluster spec, so the constraint is the path type, not access.
    """
    with pytest.raises(jobs.JobError, match="/Workspace/"):
        jobs.submit_run(
            run_name="x", existing_cluster_id="c-1",
            spark_python_file="/Volumes/a/b/c/job.py",
        )


def test_workspace_python_file_is_accepted(jobs, monkeypatch):
    captured = {}

    def fake_request(workspace, path, payload=None, method="POST"):
        captured.update(payload or {})
        return {"run_id": 42}

    monkeypatch.setattr(jobs, "_request", fake_request)
    jobs.submit_run(
        run_name="x", existing_cluster_id="c-1",
        spark_python_file="/Workspace/Users/me/job.py",
    )
    task = captured["tasks"][0]
    assert task["spark_python_task"]["python_file"] == "/Workspace/Users/me/job.py"


def test_volume_jars_are_still_allowed(jobs, monkeypatch):
    """Only python_file is path-restricted; JAR libraries from /Volumes work."""
    captured = {}

    def fake_request(workspace, path, payload=None, method="POST"):
        captured.update(payload or {})
        return {"run_id": 42}

    monkeypatch.setattr(jobs, "_request", fake_request)
    jobs.submit_run(
        run_name="x", existing_cluster_id="c-1", main_class_name="com.x.Main",
        jars=["/Volumes/a/b/c/lib.jar"],
    )
    assert captured["tasks"][0]["libraries"] == [{"jar": "/Volumes/a/b/c/lib.jar"}]


def test_sql_task_requires_a_warehouse(jobs):
    with pytest.raises(jobs.JobError, match="warehouse_id"):
        jobs.submit_run(run_name="x", sql_query_id="q-1")


def test_sql_task_cannot_carry_libraries(jobs):
    with pytest.raises(jobs.JobError, match="cannot carry libraries"):
        jobs.submit_run(
            run_name="x", sql_query_id="q-1", warehouse_id="w-1",
            jars=["/Volumes/a/b/c.jar"],
        )


def test_sql_task_sends_query_id_not_inline_sql(jobs, monkeypatch):
    """Observed live: inline SQL is rejected with 'Missing required field: query_id'."""
    captured = {}

    def fake_request(workspace, path, payload=None, method="POST"):
        captured.update(payload or {})
        return {"run_id": 42}

    monkeypatch.setattr(jobs, "_request", fake_request)
    jobs.submit_run(run_name="x", sql_query_id="q-1", warehouse_id="w-1")
    query = captured["tasks"][0]["sql_task"]["query"]
    assert query == {"query_id": "q-1"}
    assert "query" not in query


def test_library_block_covers_every_kind(jobs):
    libs = jobs._libraries_block(
        jars=["j"], whls=["w"], pypi=["p"], maven=["m"]
    )
    assert {"jar": "j"} in libs
    assert {"whl": "w"} in libs
    assert {"pypi": {"package": "p"}} in libs
    assert {"maven": {"coordinates": "m"}} in libs


# ─── the succeed-vs-finish distinction ──────────────────────────────────────

def test_a_terminated_but_failed_run_is_not_reported_as_success(jobs, monkeypatch):
    """This is the whole point: finishing is not succeeding."""
    monkeypatch.setattr(
        jobs, "_request",
        lambda *a, **k: _run_body(
            "TERMINATED", "FAILED",
            state={"life_cycle_state": "TERMINATED", "result_state": "FAILED",
                   "state_message": "AnalysisException: table not found"},
        ),
    )
    record = jobs.wait_for_run(42, timeout_seconds=5)
    assert record["succeeded"] is False
    assert "AnalysisException" in record["failure_summary"]


def test_a_successful_run_is_reported_as_such(jobs, monkeypatch):
    monkeypatch.setattr(jobs, "_request",
                        lambda *a, **k: _run_body("TERMINATED", "SUCCESS"))
    record = jobs.wait_for_run(42, timeout_seconds=5)
    assert record["succeeded"] is True
    assert "failure_summary" not in record


@pytest.mark.parametrize("life", ["SKIPPED", "INTERNAL_ERROR"])
def test_other_terminal_lifecycles_are_failures_with_a_summary(
    jobs, monkeypatch, life
):
    monkeypatch.setattr(jobs, "_request", lambda *a, **k: _run_body(life))
    record = jobs.wait_for_run(42, timeout_seconds=5)
    assert record["succeeded"] is False
    assert record["failure_summary"]


def test_wait_raises_when_a_run_never_finishes(jobs, monkeypatch):
    monkeypatch.setattr(jobs, "_request", lambda *a, **k: _run_body("RUNNING"))
    monkeypatch.setattr(jobs.time, "sleep", lambda _s: None)
    with pytest.raises(jobs.JobError, match="still RUNNING"):
        jobs.wait_for_run(42, timeout_seconds=0.01)


def test_get_run_flattens_per_task_state(jobs, monkeypatch):
    monkeypatch.setattr(jobs, "_request",
                        lambda *a, **k: _run_body("TERMINATED", "FAILED"))
    record = jobs.get_run(42)
    assert record["tasks"][0]["task_key"] == "main"
    assert record["tasks"][0]["result_state"] == "FAILED"
    assert record["tasks"][0]["run_id"] == 43


# ─── output ─────────────────────────────────────────────────────────────────

def test_multitask_output_error_names_the_task_run_ids_to_use(jobs, monkeypatch):
    """The API 400s on a parent run id; a bare HTTP error is useless here."""
    def fake_request(workspace, path, payload=None, method="POST"):
        if "get-output" in path:
            raise jobs.DatabricksAPIError(
                "failed: HTTP 400. Retrieving the output of a multi-task run "
                "is not supported"
            )
        return _run_body("TERMINATED", "SUCCESS")

    monkeypatch.setattr(jobs, "_request", fake_request)
    with pytest.raises(jobs.JobError, match=r"43"):
        jobs.run_output(42)


def test_output_surfaces_the_error_trace(jobs, monkeypatch):
    monkeypatch.setattr(
        jobs, "_request",
        lambda *a, **k: {
            "error": "java.lang.NoClassDefFoundError",
            "error_trace": "at com.x.Main.main(Main.scala:10)",
            "logs": "stdout tail",
        },
    )
    out = jobs.run_output(42)
    assert "NoClassDefFoundError" in out["error"]
    assert "Main.scala" in out["error_trace"]


def test_submit_and_wait_attaches_output_on_failure_instead_of_raising(
    jobs, monkeypatch
):
    """A failed job's stack trace IS the answer the caller wanted."""
    def fake_request(workspace, path, payload=None, method="POST"):
        if path.endswith("/submit"):
            return {"run_id": 42}
        if "get-output" in path:
            return {"error": "boom", "error_trace": "trace here"}
        return _run_body("TERMINATED", "FAILED")

    monkeypatch.setattr(jobs, "_request", fake_request)
    record = jobs.submit_and_wait(
        run_name="x", existing_cluster_id="c-1", notebook_path="/n",
    )
    assert record["succeeded"] is False
    assert record["output"]["error"] == "boom"
    assert record["compute"]["kind"] == "existing"


def test_list_runs_can_filter_to_still_billing(jobs, monkeypatch):
    seen = {}

    def fake_request(workspace, path, payload=None, method="POST"):
        seen["path"] = path
        return {"runs": [{"run_id": 42, "state": {"life_cycle_state": "RUNNING"}}]}

    monkeypatch.setattr(jobs, "_request", fake_request)
    jobs.list_runs(active_only=True)
    assert "active_only=true" in seen["path"]


def test_list_runs_limit_is_clamped_to_the_api_maximum(jobs, monkeypatch):
    seen = {}

    def fake_request(workspace, path, payload=None, method="POST"):
        seen["path"] = path
        return {"runs": []}

    monkeypatch.setattr(jobs, "_request", fake_request)
    jobs.list_runs(limit=9999)
    assert "limit=25" in seen["path"]
