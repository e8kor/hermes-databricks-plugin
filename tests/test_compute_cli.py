"""Compute CLI: the guardrail layer.

These tests exist because the guardrails ARE the justification for keeping
compute out of the model toolset. If a confirmation can be bypassed, or an
ambiguous compute request resolves by silent precedence instead of an error,
the CLI-only decision stops buying anything.

Nothing here touches a network: the api/compute/jobs seams are stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def ccli(plugin_pkg):
    import importlib

    return importlib.import_module("databricks_plugin.compute_cli")


def _submit_args(**over):
    """A minimally-valid `job submit` arg namespace."""
    base = dict(
        databricks_action="job", job_action="submit", workspace=None,
        name="x", cluster=None, spark_version=None, node_type=None,
        workers=0, policy=None, security_mode=None,
        notebook=None, python_file=None, main_class=None,
        sql_query_id=None, warehouse=None,
        jar=[], whl=[], pypi=[], maven=[], param=[],
        timeout=3600, no_wait=False, yes=True, json=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _cluster_args(**over):
    base = dict(
        databricks_action="cluster", cluster_action="create", workspace=None,
        name="x", spark_version="17.3.x-scala2.13", node_type="Standard_D4ds_v5",
        workers=0, autotermination=30, policy=None, security_mode=None,
        single_user=None, wait=False, yes=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ─── confirmation cannot be bypassed by accident ────────────────────────────

def test_confirm_refuses_in_a_non_interactive_shell(ccli, monkeypatch, capsys):
    """A cron job or a piped shell must not be able to start billable compute."""
    monkeypatch.setattr(ccli.sys.stdin, "isatty", lambda: False)
    assert ccli._confirm("Start cluster?", assume_yes=False) is False
    assert "--yes" in capsys.readouterr().out


def test_confirm_short_circuits_on_assume_yes_without_reading_stdin(ccli, monkeypatch):
    def explode():
        raise AssertionError("stdin must not be consulted when --yes is passed")

    monkeypatch.setattr(ccli.sys.stdin, "isatty", explode)
    assert ccli._confirm("Start cluster?", assume_yes=True) is True


@pytest.mark.parametrize("answer,expected", [
    ("y", True), ("yes", True), ("YES", True),
    ("n", False), ("no", False), ("", False), ("maybe", False),
])
def test_confirm_only_accepts_an_explicit_yes(ccli, monkeypatch, answer, expected):
    monkeypatch.setattr(ccli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p: answer)
    assert ccli._confirm("Start cluster?", assume_yes=False) is expected


# ─── cluster create guardrails ──────────────────────────────────────────────

def test_create_without_a_policy_refuses_before_any_api_call(ccli, monkeypatch):
    """A policy caps size and cost; creating without one is a surprise bill."""
    def explode(*a, **k):
        raise AssertionError("no API call may happen without a policy")

    monkeypatch.setattr(ccli._compute, "create_cluster", explode)
    assert ccli._handle_cluster(_cluster_args(policy=None)) == 1


def test_create_with_a_policy_reaches_the_api(ccli, monkeypatch):
    seen = {}

    def fake_create(**kwargs):
        seen.update(kwargs)
        return {"cluster_id": "c-1", "state": "PENDING"}

    monkeypatch.setattr(ccli._compute, "create_cluster", fake_create)
    assert ccli._handle_cluster(_cluster_args(policy="p-1")) == 0
    assert seen["policy_id"] == "p-1"
    assert seen["autotermination_minutes"] == 30


def test_declining_the_prompt_aborts_create(ccli, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("declined create must not call the API")

    monkeypatch.setattr(ccli._compute, "create_cluster", explode)
    monkeypatch.setattr(ccli, "_confirm", lambda *a, **k: False)
    assert ccli._handle_cluster(_cluster_args(policy="p-1", yes=False)) == 1


@pytest.mark.parametrize("verb", ["start", "restart"])
def test_declining_the_prompt_aborts_start_and_restart(ccli, monkeypatch, verb):
    def explode(*a, **k):
        raise AssertionError(f"declined {verb} must not call the API")

    monkeypatch.setattr(ccli._compute, "start_cluster", explode)
    monkeypatch.setattr(ccli._compute, "restart_cluster", explode)
    monkeypatch.setattr(ccli, "_confirm", lambda *a, **k: False)
    args = SimpleNamespace(
        databricks_action="cluster", cluster_action=verb, workspace=None,
        cluster_id="c-1", no_wait=False, yes=False,
    )
    assert ccli._handle_cluster(args) == 1


def test_stop_is_never_gated_on_confirmation(ccli, monkeypatch):
    """Stopping SAVES money — prompting for it would be an obstacle, not a guard."""
    called = {}

    def fake_stop(cluster_id, workspace=None, wait=False):
        called["id"] = cluster_id
        return {"cluster_id": cluster_id, "state": "TERMINATED"}

    monkeypatch.setattr(ccli._compute, "stop_cluster", fake_stop)

    def explode(*a, **k):
        raise AssertionError("stop must not ask for confirmation")

    monkeypatch.setattr(ccli, "_confirm", explode)
    args = SimpleNamespace(
        databricks_action="cluster", cluster_action="stop", workspace=None,
        cluster_id="c-1", no_wait=True, yes=False,
    )
    assert ccli._handle_cluster(args) == 0
    assert called["id"] == "c-1"


# ─── ambiguous compute must be an error, not a precedence rule ──────────────

def test_cluster_plus_ephemeral_flags_is_refused_locally(ccli, monkeypatch):
    """Regression: the CLI used to silently drop --spark-version and submit
    against --cluster, hiding a real mistake about where the code ran."""
    def explode(*a, **k):
        raise AssertionError("ambiguous compute must not reach the API")

    monkeypatch.setattr(ccli._jobs, "submit_and_wait", explode)
    monkeypatch.setattr(ccli._jobs, "submit_run", explode)
    args = _submit_args(
        cluster="c-1", spark_version="17.3.x-scala2.13",
        node_type="Standard_D4ds_v5", notebook="/n",
    )
    assert ccli._handle_job(args) == 1


def test_cluster_only_submit_is_allowed(ccli, monkeypatch):
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return {"run_id": 1, "succeeded": True, "compute": {"kind": "existing"}}

    monkeypatch.setattr(ccli._jobs, "submit_and_wait", fake)
    assert ccli._handle_job(_submit_args(cluster="c-1", notebook="/n")) == 0
    assert seen["existing_cluster_id"] == "c-1"
    assert "spark_version" not in seen


def test_ephemeral_submit_without_a_cluster_spec_is_refused(ccli, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("incomplete ephemeral spec must not reach the API")

    monkeypatch.setattr(ccli._jobs, "submit_and_wait", explode)
    assert ccli._handle_job(_submit_args(notebook="/n")) == 1


def test_declining_the_prompt_aborts_a_job_submit(ccli, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("declined submit must not call the API")

    monkeypatch.setattr(ccli._jobs, "submit_and_wait", explode)
    monkeypatch.setattr(ccli, "_confirm", lambda *a, **k: False)
    args = _submit_args(
        spark_version="17.3.x-scala2.13", node_type="Standard_D4ds_v5",
        notebook="/n", yes=False,
    )
    assert ccli._handle_job(args) == 1


# ─── exit codes must reflect the real verdict ───────────────────────────────

def test_a_failed_run_exits_non_zero(ccli, monkeypatch):
    """A finished-but-failed run must not look like success to a shell."""
    monkeypatch.setattr(
        ccli._jobs, "submit_and_wait",
        lambda **k: {"run_id": 1, "succeeded": False,
                     "failure_summary": "AnalysisException",
                     "compute": {"kind": "existing"}},
    )
    assert ccli._handle_job(_submit_args(cluster="c-1", notebook="/n")) == 1


def test_a_successful_run_exits_zero(ccli, monkeypatch):
    monkeypatch.setattr(
        ccli._jobs, "submit_and_wait",
        lambda **k: {"run_id": 1, "succeeded": True,
                     "compute": {"kind": "existing"}},
    )
    assert ccli._handle_job(_submit_args(cluster="c-1", notebook="/n")) == 0


def test_a_failed_library_status_exits_non_zero(ccli, monkeypatch):
    monkeypatch.setattr(
        ccli._compute, "library_status",
        lambda cid, workspace=None: [
            {"library": "/Volumes/a/b/c.jar", "status": "FAILED",
             "messages": ["resolution failed"]}
        ],
    )
    args = SimpleNamespace(
        databricks_action="library", library_action="status",
        workspace=None, cluster_id="c-1",
    )
    assert ccli._handle_library(args) == 1


def test_an_installed_library_status_exits_zero(ccli, monkeypatch):
    monkeypatch.setattr(
        ccli._compute, "library_status",
        lambda cid, workspace=None: [
            {"library": "/Volumes/a/b/c.jar", "status": "INSTALLED"}
        ],
    )
    args = SimpleNamespace(
        databricks_action="library", library_action="status",
        workspace=None, cluster_id="c-1",
    )
    assert ccli._handle_library(args) == 0


# ─── dispatch ───────────────────────────────────────────────────────────────

def test_non_compute_actions_are_passed_back_to_the_main_cli(ccli):
    """Returning None is how cli.py knows to keep dispatching."""
    for action in ("status", "catalogs", "query", "fetch", "setup", None):
        args = SimpleNamespace(databricks_action=action)
        assert ccli.handle_compute_cli(args) is None


def test_api_errors_are_reported_not_raised(ccli, monkeypatch, capsys):
    """A tool that raises out of the CLI prints a traceback at the user."""
    def boom(*a, **k):
        raise ccli.DatabricksAPIError("HTTP 403 forbidden")

    monkeypatch.setattr(ccli._compute, "list_clusters", boom)
    args = SimpleNamespace(
        databricks_action="cluster", cluster_action="list",
        workspace=None, mine=False,
    )
    assert ccli.handle_compute_cli(args) == 1
    assert "403" in capsys.readouterr().out


def test_running_clusters_are_flagged_in_the_listing(ccli, monkeypatch, capsys):
    """An idle RUNNING cluster is the quiet way money disappears."""
    monkeypatch.setattr(
        ccli._compute, "list_clusters",
        lambda workspace=None, mine_only=False: [
            {"cluster_id": "c-1", "name": "a", "state": "RUNNING"},
            {"cluster_id": "c-2", "name": "b", "state": "TERMINATED"},
        ],
    )
    args = SimpleNamespace(
        databricks_action="cluster", cluster_action="list",
        workspace=None, mine=False,
    )
    assert ccli.handle_compute_cli(args) == 0
    out = capsys.readouterr().out
    assert "billing" in out
    assert "c-1" in out
