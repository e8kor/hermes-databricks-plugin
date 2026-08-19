"""Compute layer: clusters, libraries, volume upload.

Every test stubs the HTTP seam (``_request`` / ``urlopen``) so nothing here
touches a network.  The assertions are about *behaviour contracts* — what the
module refuses, what it polls for, and what it reports — not about payload
snapshots.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def compute(plugin_pkg):
    import importlib

    return importlib.import_module("databricks_plugin.compute")


def _cluster(state, **extra):
    body = {
        "cluster_id": "c-1",
        "cluster_name": "test",
        "state": state,
        "spark_version": "17.3.x-scala2.13",
        "node_type_id": "Standard_D4ds_v5",
        "autotermination_minutes": 30,
    }
    body.update(extra)
    return body


# ─── guardrails: the module must refuse configurations that bill forever ─────

@pytest.mark.parametrize("minutes", [0, 1, 9, -5])
def test_create_cluster_refuses_short_or_absent_autotermination(compute, minutes):
    """0 means 'never terminate' — the most expensive mistake in this API."""
    with pytest.raises(compute.ClusterError, match="autotermination_minutes"):
        compute.create_cluster(
            name="x", spark_version="17.3.x-scala2.13",
            node_type_id="Standard_D4ds_v5", autotermination_minutes=minutes,
        )


def test_single_node_cluster_gets_the_three_required_settings(compute, monkeypatch):
    """Without these a single-node cluster starts and then fails every task."""
    captured = {}

    def fake_request(workspace, path, payload=None, method="POST"):
        if path.endswith("/create"):
            captured.update(payload or {})
            return {"cluster_id": "c-1"}
        return _cluster("PENDING")

    monkeypatch.setattr(compute, "_request", fake_request)
    compute.create_cluster(
        name="x", spark_version="17.3.x-scala2.13",
        node_type_id="Standard_D4ds_v5", num_workers=0,
    )
    conf = captured.get("spark_conf") or {}
    assert conf.get("spark.databricks.cluster.profile") == "singleNode"
    assert conf.get("spark.master") == "local[*]"
    assert (captured.get("custom_tags") or {}).get("ResourceClass") == "SingleNode"
    assert captured.get("num_workers") == 0


def test_multi_worker_cluster_does_not_get_single_node_settings(compute, monkeypatch):
    captured = {}

    def fake_request(workspace, path, payload=None, method="POST"):
        if path.endswith("/create"):
            captured.update(payload or {})
            return {"cluster_id": "c-1"}
        return _cluster("PENDING")

    monkeypatch.setattr(compute, "_request", fake_request)
    compute.create_cluster(
        name="x", spark_version="17.3.x-scala2.13",
        node_type_id="Standard_D4ds_v5", num_workers=4,
    )
    assert captured.get("num_workers") == 4
    assert "custom_tags" not in captured
    assert "spark.master" not in (captured.get("spark_conf") or {})


# ─── state machine: idempotence and the restart-vs-start distinction ────────

def test_start_on_running_cluster_is_a_noop_not_an_api_call(compute, monkeypatch):
    """The API 400s on start-when-running; that must not look like a failure."""
    calls = []

    def fake_request(workspace, path, payload=None, method="POST"):
        calls.append(path)
        return _cluster("RUNNING")

    monkeypatch.setattr(compute, "_request", fake_request)
    record = compute.start_cluster("c-1")
    assert record["already_running"] is True
    assert not any("/start" in p for p in calls)


def test_stop_on_terminated_cluster_is_a_noop(compute, monkeypatch):
    calls = []

    def fake_request(workspace, path, payload=None, method="POST"):
        calls.append(path)
        return _cluster("TERMINATED")

    monkeypatch.setattr(compute, "_request", fake_request)
    record = compute.stop_cluster("c-1")
    assert record["already_terminated"] is True
    assert not any("/delete" in p for p in calls)


def test_restart_of_a_terminated_cluster_falls_through_to_start(compute, monkeypatch):
    """A TERMINATED cluster cannot be restarted — the API rejects it."""
    calls = []
    states = iter(["TERMINATED", "TERMINATED", "RUNNING"])

    def fake_request(workspace, path, payload=None, method="POST"):
        calls.append(path)
        if path.endswith("/start") or path.endswith("/restart"):
            return {}
        return _cluster(next(states, "RUNNING"))

    monkeypatch.setattr(compute, "_request", fake_request)
    compute.restart_cluster("c-1", wait=False)
    assert any("/start" in p for p in calls)
    assert not any("/restart" in p for p in calls)


def test_wait_for_cluster_raises_when_it_terminates_instead_of_running(
    compute, monkeypatch
):
    """A failed spot acquisition or policy rejection surfaces exactly this way."""
    monkeypatch.setattr(
        compute, "_request",
        lambda *a, **k: _cluster(
            "TERMINATED", termination_reason={"code": "CLOUD_PROVIDER_LAUNCH_FAILURE"}
        ),
    )
    with pytest.raises(compute.ClusterError, match="CLOUD_PROVIDER_LAUNCH_FAILURE"):
        compute.wait_for_cluster("c-1", timeout_seconds=1)


# ─── libraries: the async-install trap ──────────────────────────────────────

def test_install_refuses_a_cluster_that_is_not_running(compute, monkeypatch):
    """An install onto a stopped cluster is accepted by the API and then ignored."""
    monkeypatch.setattr(compute, "_request", lambda *a, **k: _cluster("TERMINATED"))
    with pytest.raises(compute.LibraryError, match="start it before installing"):
        compute.install_library("c-1", jar="/Volumes/a/b/c.jar")


def test_install_raises_on_a_failed_install_rather_than_reporting_success(
    compute, monkeypatch
):
    """libraries/install returns 200 before installing; FAILED lands in status."""
    def fake_request(workspace, path, payload=None, method="POST"):
        if "cluster-status" in path:
            return {
                "library_statuses": [
                    {
                        "library": {"jar": "/Volumes/a/b/c.jar"},
                        "status": "FAILED",
                        "messages": ["Library resolution failed"],
                    }
                ]
            }
        if path.endswith("/install"):
            return {}
        return _cluster("RUNNING")

    monkeypatch.setattr(compute, "_request", fake_request)
    monkeypatch.setattr(compute.time, "sleep", lambda _s: None)
    with pytest.raises(compute.LibraryError, match="Library resolution failed"):
        compute.install_library("c-1", jar="/Volumes/a/b/c.jar", timeout_seconds=5)


def test_install_returns_installed_on_the_happy_path(compute, monkeypatch):
    def fake_request(workspace, path, payload=None, method="POST"):
        if "cluster-status" in path:
            return {
                "library_statuses": [
                    {"library": {"jar": "/Volumes/a/b/c.jar"}, "status": "INSTALLED"}
                ]
            }
        if path.endswith("/install"):
            return {}
        return _cluster("RUNNING")

    monkeypatch.setattr(compute, "_request", fake_request)
    result = compute.install_library("c-1", jar="/Volumes/a/b/c.jar")
    assert result["status"] == "INSTALLED"
    assert result["library"] == "/Volumes/a/b/c.jar"


def test_no_wait_install_says_it_has_not_verified_anything(compute, monkeypatch):
    monkeypatch.setattr(
        compute, "_request",
        lambda w, p, payload=None, method="POST": (
            {} if p.endswith("/install") else _cluster("RUNNING")
        ),
    )
    result = compute.install_library("c-1", jar="/Volumes/a/b/c.jar", wait=False)
    assert result["status"] == "PENDING"
    assert "poll" in result["note"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"jar": "a", "whl": "b"},
        {"pypi": "x", "maven": "y"},
    ],
)
def test_library_spec_requires_exactly_one_kind(compute, kwargs):
    with pytest.raises(compute.LibraryError, match="exactly one"):
        compute._library_spec(**kwargs)


def test_uninstall_says_a_restart_is_required(compute, monkeypatch):
    """The running JVM keeps the library; uninstall alone is not enough."""
    monkeypatch.setattr(compute, "_request", lambda *a, **k: {})
    result = compute.uninstall_library("c-1", jar="/Volumes/a/b/c.jar")
    assert result["marked_for_removal"] is True
    assert "restart" in result["note"]


# ─── upload: byte verification and the Files-API field names ────────────────

def test_upload_rejects_a_size_mismatch_as_corrupt(compute, monkeypatch, tmp_path):
    """A truncated JAR is accepted by the API and fails much later on executors."""
    src = tmp_path / "lib.jar"
    src.write_bytes(b"x" * 1000)

    monkeypatch.setattr(compute, "resolve_workspace",
                        lambda w: ("main", {"host": "https://h"}))
    monkeypatch.setattr(compute, "get_token", lambda w: "tok")
    monkeypatch.setattr(compute, "file_size", lambda p, workspace=None: 999)

    class FakeResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(compute.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResponse())

    with pytest.raises(compute.DatabricksAPIError, match="size mismatch"):
        compute.upload_file(src, "/Volumes/a/b/c/lib.jar")


def test_upload_reports_verified_when_sizes_match(compute, monkeypatch, tmp_path):
    src = tmp_path / "lib.jar"
    src.write_bytes(b"x" * 1000)

    monkeypatch.setattr(compute, "resolve_workspace",
                        lambda w: ("main", {"host": "https://h"}))
    monkeypatch.setattr(compute, "get_token", lambda w: "tok")
    monkeypatch.setattr(compute, "file_size", lambda p, workspace=None: 1000)

    class FakeResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(compute.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResponse())

    result = compute.upload_file(src, "/Volumes/a/b/c/lib.jar")
    assert result["verified"] is True
    assert result["bytes"] == 1000
    assert result["remote_path"] == "/Volumes/a/b/c/lib.jar"


def test_upload_missing_local_file_fails_before_any_http(compute):
    with pytest.raises(compute.DatabricksAPIError, match="local file not found"):
        compute.upload_file("/nope/does-not-exist.jar", "/Volumes/a/b/c/x.jar")


def test_volume_listing_reads_files_api_field_names(compute, monkeypatch):
    """The Files API returns name/file_size, NOT the legacy DBFS path/size."""
    monkeypatch.setattr(
        compute, "_request",
        lambda *a, **k: {
            "contents": [
                {"name": "lib.jar", "path": "/Volumes/a/b/c/lib.jar",
                 "file_size": 630437, "is_directory": False}
            ]
        },
    )
    items = compute.list_volume_dir("/Volumes/a/b/c")
    assert items[0]["name"] == "lib.jar"
    assert items[0]["bytes"] == 630437
    assert items[0]["is_directory"] is False


def test_upload_path_segments_are_quoted_individually(compute, monkeypatch, tmp_path):
    """Slashes are real separators here; a space in a filename must be escaped."""
    src = tmp_path / "lib.jar"
    src.write_bytes(b"x")
    seen = {}

    monkeypatch.setattr(compute, "resolve_workspace",
                        lambda w: ("main", {"host": "https://h"}))
    monkeypatch.setattr(compute, "get_token", lambda w: "tok")
    monkeypatch.setattr(compute, "file_size", lambda p, workspace=None: 1)

    class FakeResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return FakeResponse()

    monkeypatch.setattr(compute.urllib.request, "urlopen", fake_urlopen)
    compute.upload_file(src, "/Volumes/cat/sch/vol/my lib.jar")
    assert "my%20lib.jar" in seen["url"]
    # Real path separators survive; only the segment contents are escaped.
    assert "/Volumes/cat/sch/vol/" in seen["url"]


def test_403_upload_explains_the_unity_catalog_privilege_trap(
    compute, monkeypatch, tmp_path
):
    """Workspace-admin does not imply WRITE VOLUME — observed live."""
    src = tmp_path / "lib.jar"
    src.write_bytes(b"x")

    monkeypatch.setattr(compute, "resolve_workspace",
                        lambda w: ("main", {"host": "https://h"}))
    monkeypatch.setattr(compute, "get_token", lambda w: "tok")

    def fake_urlopen(req, timeout=None):
        raise compute.urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {}, None
        )

    monkeypatch.setattr(compute.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(compute.DatabricksAPIError, match="Unity Catalog privileges"):
        compute.upload_file(src, "/Volumes/a/b/c/lib.jar")


# ─── listings ───────────────────────────────────────────────────────────────

def test_node_types_are_cheapest_first_and_exclude_deprecated(compute, monkeypatch):
    monkeypatch.setattr(
        compute, "_request",
        lambda *a, **k: {
            "node_types": [
                {"node_type_id": "big", "num_cores": 16, "memory_mb": 65536},
                {"node_type_id": "old", "num_cores": 2, "memory_mb": 8192,
                 "is_deprecated": True},
                {"node_type_id": "small", "num_cores": 2, "memory_mb": 8192},
            ]
        },
    )
    types = compute.list_node_types()
    assert [t["node_type_id"] for t in types] == ["small", "big"]


def test_spark_versions_filters_to_lts_by_default(compute, monkeypatch):
    monkeypatch.setattr(
        compute, "_request",
        lambda *a, **k: {
            "versions": [
                {"key": "17.3.x-scala2.13", "name": "17.3 LTS (Spark 4.0.0)"},
                {"key": "18.x-scala2.13", "name": "18.0 (Spark 4.1.0)"},
            ]
        },
    )
    assert [v["key"] for v in compute.list_spark_versions()] == ["17.3.x-scala2.13"]
    assert len(compute.list_spark_versions(lts_only=False)) == 2


def test_cluster_listing_can_filter_to_my_own_clusters(compute, monkeypatch):
    def fake_request(workspace, path, payload=None, method="POST"):
        if "scim" in path:
            return {"userName": "me@example.com"}
        return {
            "clusters": [
                _cluster("TERMINATED", cluster_id="mine",
                         creator_user_name="me@example.com"),
                _cluster("TERMINATED", cluster_id="theirs",
                         creator_user_name="other@example.com"),
            ]
        }

    monkeypatch.setattr(compute, "_request", fake_request)
    assert [c["cluster_id"] for c in compute.list_clusters(mine_only=True)] == ["mine"]
    assert len(compute.list_clusters()) == 2
