"""Cluster lifecycle, library installation, and UC Volume file upload.

This module deliberately reports *observed* state rather than API acceptance,
because the Databricks compute APIs are asynchronous in ways that make the
naive wrapper lie:

* ``clusters/start`` returns 200 while the cluster sits in ``PENDING`` for
  minutes.  A wrapper that prints "started" is wrong for ~5 minutes.
* ``libraries/install`` returns 200 **immediately**, before anything is
  downloaded.  The library then moves through ``PENDING`` → ``INSTALLING`` →
  ``INSTALLED`` or ``FAILED``, and the failure lands only in
  ``libraries/cluster-status``.  So "install" without a status read is a
  coin flip reported as success.

Every function here therefore either polls to a terminal state or returns the
raw observed state and says so.  Nothing claims completion it did not see.

Uploads use the Files API (``PUT /api/2.0/fs/files/<path>``) rather than the
legacy DBFS put, and verify the remote byte count against the local one: a
truncated upload otherwise produces a JAR that fails much later on an executor
with an opaque class-loading error.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from .api import DatabricksAPIError, _request
from .config import get_token, resolve_workspace

# A cluster reports one of these; only the first two are useful to act on.
_CLUSTER_RUNNING = "RUNNING"
_CLUSTER_TERMINATED = "TERMINATED"
_CLUSTER_TERMINAL_STATES = {_CLUSTER_RUNNING, _CLUSTER_TERMINATED, "ERROR"}
_CLUSTER_PENDING_STATES = {"PENDING", "RESTARTING", "RESIZING", "TERMINATING"}

# Library install is async; these are the states cluster-status reports.
_LIB_TERMINAL_STATES = {"INSTALLED", "FAILED", "SKIPPED"}

_POLL_INTERVAL_SECONDS = 10.0
_HTTP_TIMEOUT_SECONDS = 600

# Guardrail: a cluster with no autotermination bills until someone notices.
# The plugin refuses to create one, rather than trusting the caller.
_MIN_AUTOTERMINATION_MINUTES = 10
_DEFAULT_AUTOTERMINATION_MINUTES = 30


class ClusterError(DatabricksAPIError):
    """A cluster operation failed or ended in an unusable state."""


class LibraryError(DatabricksAPIError):
    """A library failed to install on a cluster."""


# ─────────────────────────────────────────────────────────────────────────────
# Clusters
# ─────────────────────────────────────────────────────────────────────────────

def list_clusters(
    *, workspace: Optional[str] = None, mine_only: bool = False
) -> List[Dict[str, Any]]:
    """List clusters with the fields that matter for spend and usability."""
    body = _request(workspace, "/api/2.1/clusters/list", method="GET")
    me = current_user(workspace=workspace) if mine_only else None
    out: List[Dict[str, Any]] = []
    for c in body.get("clusters") or []:
        creator = c.get("creator_user_name")
        if mine_only and creator != me:
            continue
        out.append({
            "cluster_id": c.get("cluster_id"),
            "name": c.get("cluster_name"),
            "state": c.get("state"),
            "spark_version": c.get("spark_version"),
            "node_type_id": c.get("node_type_id"),
            "num_workers": c.get("num_workers"),
            "autotermination_minutes": c.get("autotermination_minutes"),
            "data_security_mode": c.get("data_security_mode"),
            "creator": creator,
            "policy_id": c.get("policy_id"),
        })
    return out


def current_user(*, workspace: Optional[str] = None) -> str:
    """The authenticated principal's user name."""
    body = _request(workspace, "/api/2.0/preview/scim/v2/Me", method="GET")
    return str(body.get("userName") or "")


def get_cluster(cluster_id: str, *, workspace: Optional[str] = None) -> Dict[str, Any]:
    """Full cluster record, including the termination reason when relevant."""
    body = _request(
        workspace,
        f"/api/2.1/clusters/get?cluster_id={urllib.parse.quote(cluster_id, safe='')}",
        method="GET",
    )
    out = {
        "cluster_id": body.get("cluster_id"),
        "name": body.get("cluster_name"),
        "state": body.get("state"),
        "state_message": body.get("state_message"),
        "spark_version": body.get("spark_version"),
        "node_type_id": body.get("node_type_id"),
        "num_workers": body.get("num_workers"),
        "autoscale": body.get("autoscale"),
        "autotermination_minutes": body.get("autotermination_minutes"),
        "data_security_mode": body.get("data_security_mode"),
        "single_user_name": body.get("single_user_name"),
        "policy_id": body.get("policy_id"),
        "creator": body.get("creator_user_name"),
        "spark_conf": body.get("spark_conf") or {},
    }
    reason = body.get("termination_reason") or {}
    if reason:
        out["termination_reason"] = {
            "code": reason.get("code"),
            "type": reason.get("type"),
            "parameters": reason.get("parameters") or {},
        }
    return out


def cluster_state(cluster_id: str, *, workspace: Optional[str] = None) -> str:
    """Just the state string, for polling loops."""
    return str(get_cluster(cluster_id, workspace=workspace).get("state") or "")


def wait_for_cluster(
    cluster_id: str,
    *,
    workspace: Optional[str] = None,
    target: str = _CLUSTER_RUNNING,
    timeout_seconds: float = 1800.0,
) -> Dict[str, Any]:
    """Poll until the cluster reaches ``target`` or a terminal state.

    Returns the final cluster record.  Raises :class:`ClusterError` when the
    cluster settles into a state other than ``target`` — notably ``TERMINATED``
    while waiting for ``RUNNING``, which is how a failed spot acquisition or a
    policy rejection actually manifests.
    """
    deadline = time.monotonic() + timeout_seconds
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = get_cluster(cluster_id, workspace=workspace)
        state = str(last.get("state") or "")
        if state == target:
            return last
        if state in _CLUSTER_TERMINAL_STATES:
            reason = last.get("termination_reason") or {}
            detail = reason.get("code") or last.get("state_message") or "no reason given"
            raise ClusterError(
                f"cluster {cluster_id} reached {state} while waiting for {target} "
                f"({detail})"
            )
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise ClusterError(
        f"cluster {cluster_id} still {last.get('state')} after {timeout_seconds:g}s "
        f"waiting for {target}"
    )


def create_cluster(
    *,
    name: str,
    spark_version: str,
    node_type_id: str,
    workspace: Optional[str] = None,
    num_workers: int = 0,
    autotermination_minutes: int = _DEFAULT_AUTOTERMINATION_MINUTES,
    policy_id: Optional[str] = None,
    data_security_mode: Optional[str] = None,
    single_user_name: Optional[str] = None,
    spark_conf: Optional[Dict[str, str]] = None,
    wait: bool = False,
) -> Dict[str, Any]:
    """Create a cluster, refusing configurations that bill indefinitely.

    ``autotermination_minutes`` is mandatory in practice: the API happily
    accepts 0 (never terminate), which is the single most expensive mistake
    available here, so it is rejected below rather than passed through.

    ``num_workers=0`` creates a single-node cluster, which needs two specific
    spark_conf keys — omitting them yields a cluster that starts and then fails
    every job with a "no workers" scheduling error.
    """
    if autotermination_minutes < _MIN_AUTOTERMINATION_MINUTES:
        raise ClusterError(
            f"autotermination_minutes must be >= {_MIN_AUTOTERMINATION_MINUTES} "
            "(0 means 'never terminate' and bills until manually stopped)"
        )

    conf: Dict[str, str] = dict(spark_conf or {})
    payload: Dict[str, Any] = {
        "cluster_name": name,
        "spark_version": spark_version,
        "node_type_id": node_type_id,
        "autotermination_minutes": int(autotermination_minutes),
    }

    if num_workers <= 0:
        # Single-node requires both keys plus the marker tag; without them the
        # driver refuses to schedule tasks.
        conf.setdefault("spark.databricks.cluster.profile", "singleNode")
        conf.setdefault("spark.master", "local[*]")
        payload["num_workers"] = 0
        payload["custom_tags"] = {"ResourceClass": "SingleNode"}
    else:
        payload["num_workers"] = int(num_workers)

    if conf:
        payload["spark_conf"] = conf
    if policy_id:
        payload["policy_id"] = policy_id
    if data_security_mode:
        payload["data_security_mode"] = data_security_mode
    if single_user_name:
        payload["single_user_name"] = single_user_name

    body = _request(workspace, "/api/2.1/clusters/create", payload)
    cluster_id = str(body.get("cluster_id") or "")
    if not cluster_id:
        raise ClusterError(f"create returned no cluster_id: {body}")

    if wait:
        return wait_for_cluster(cluster_id, workspace=workspace)
    return get_cluster(cluster_id, workspace=workspace)


def start_cluster(
    cluster_id: str,
    *,
    workspace: Optional[str] = None,
    wait: bool = True,
    timeout_seconds: float = 1800.0,
) -> Dict[str, Any]:
    """Start a terminated cluster.

    An already-RUNNING cluster is a no-op rather than an error, and a PENDING
    one is waited on: the API rejects ``start`` on both with a 400 that reads
    like a real failure.
    """
    state = cluster_state(cluster_id, workspace=workspace)
    if state == _CLUSTER_RUNNING:
        record = get_cluster(cluster_id, workspace=workspace)
        record["already_running"] = True
        return record
    if state not in _CLUSTER_PENDING_STATES:
        _request(workspace, "/api/2.1/clusters/start", {"cluster_id": cluster_id})

    if not wait:
        return get_cluster(cluster_id, workspace=workspace)
    return wait_for_cluster(
        cluster_id, workspace=workspace, timeout_seconds=timeout_seconds
    )


def restart_cluster(
    cluster_id: str,
    *,
    workspace: Optional[str] = None,
    wait: bool = True,
    timeout_seconds: float = 1800.0,
) -> Dict[str, Any]:
    """Restart a cluster, which is also how a re-installed library is reloaded.

    A TERMINATED cluster cannot be restarted (the API 400s), so this falls
    through to a start — the caller's intent is "give me this cluster running
    with fresh JVMs" either way.
    """
    state = cluster_state(cluster_id, workspace=workspace)
    if state == _CLUSTER_TERMINATED:
        return start_cluster(
            cluster_id, workspace=workspace, wait=wait,
            timeout_seconds=timeout_seconds,
        )
    _request(workspace, "/api/2.1/clusters/restart", {"cluster_id": cluster_id})
    if not wait:
        return get_cluster(cluster_id, workspace=workspace)
    return wait_for_cluster(
        cluster_id, workspace=workspace, timeout_seconds=timeout_seconds
    )


def stop_cluster(
    cluster_id: str,
    *,
    workspace: Optional[str] = None,
    wait: bool = False,
    timeout_seconds: float = 900.0,
) -> Dict[str, Any]:
    """Terminate a cluster (stops billing).  Idempotent when already stopped."""
    state = cluster_state(cluster_id, workspace=workspace)
    if state == _CLUSTER_TERMINATED:
        record = get_cluster(cluster_id, workspace=workspace)
        record["already_terminated"] = True
        return record
    _request(workspace, "/api/2.1/clusters/delete", {"cluster_id": cluster_id})
    if not wait:
        return get_cluster(cluster_id, workspace=workspace)
    return wait_for_cluster(
        cluster_id, workspace=workspace, target=_CLUSTER_TERMINATED,
        timeout_seconds=timeout_seconds,
    )


def delete_cluster_permanently(
    cluster_id: str, *, workspace: Optional[str] = None
) -> Dict[str, Any]:
    """Permanently remove a cluster definition.  Not recoverable."""
    _request(
        workspace, "/api/2.1/clusters/permanent-delete", {"cluster_id": cluster_id}
    )
    return {"cluster_id": cluster_id, "deleted": True}


def list_node_types(
    *, workspace: Optional[str] = None, max_results: int = 15
) -> List[Dict[str, Any]]:
    """Non-deprecated node types, smallest first (cheapest usable default)."""
    body = _request(workspace, "/api/2.1/clusters/list-node-types", method="GET")
    types = [
        {
            "node_type_id": n.get("node_type_id"),
            "num_cores": n.get("num_cores"),
            "memory_mb": n.get("memory_mb"),
        }
        for n in (body.get("node_types") or [])
        if not n.get("is_deprecated") and n.get("num_cores")
    ]
    types.sort(key=lambda n: (n.get("num_cores") or 0, n.get("memory_mb") or 0))
    return types[: max(1, max_results)]


def list_spark_versions(
    *, workspace: Optional[str] = None, lts_only: bool = True
) -> List[Dict[str, Any]]:
    """Available DBR runtimes.  LTS-only by default — pin, don't chase latest."""
    body = _request(workspace, "/api/2.1/clusters/spark-versions", method="GET")
    out = []
    for v in body.get("versions") or []:
        label = str(v.get("name") or "")
        if lts_only and "LTS" not in label:
            continue
        out.append({"key": v.get("key"), "name": label})
    return out


def list_cluster_policies(*, workspace: Optional[str] = None) -> List[Dict[str, Any]]:
    """Cluster policies the caller may use."""
    body = _request(workspace, "/api/2.0/policies/clusters/list", method="GET")
    return [
        {"policy_id": p.get("policy_id"), "name": p.get("name")}
        for p in (body.get("policies") or [])
    ]


# ─────────────────────────────────────────────────────────────────────────────
# UC Volume / workspace file upload
# ─────────────────────────────────────────────────────────────────────────────

def upload_file(
    local_path: str | Path,
    remote_path: str,
    *,
    workspace: Optional[str] = None,
    overwrite: bool = True,
) -> Dict[str, Any]:
    """Upload a local file to a UC Volume path and VERIFY the byte count.

    ``remote_path`` is a Volume path such as
    ``/Volumes/<catalog>/<schema>/<volume>/lib.jar``.

    The size check is not ceremony: a partial upload produces a JAR that the
    Files API accepts happily and that then fails on an executor much later
    with an opaque class-loading error.  Comparing bytes here turns that into
    an immediate, legible failure.
    """
    src = Path(local_path).expanduser()
    if not src.is_file():
        raise DatabricksAPIError(f"local file not found: {src}")
    local_size = src.stat().st_size

    remote = "/" + remote_path.strip("/")
    name, entry = resolve_workspace(workspace)
    token = get_token(name)

    # Each path segment is quoted individually: the slashes are real path
    # separators here, unlike query-string values.
    quoted = "/".join(
        urllib.parse.quote(seg, safe="") for seg in remote.strip("/").split("/")
    )
    url = f"{entry['host']}/api/2.0/fs/files/{quoted}"
    if overwrite:
        url += "?overwrite=true"

    with open(src, "rb") as fh:
        data = fh.read()
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
            "User-Agent": "hermes-databricks-plugin/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = (exc.read() or b"").decode("utf-8", "replace")[:400]
        except Exception:
            pass
        hint = ""
        if exc.code in (401, 403):
            hint = (
                " — workspace-admin does not imply Unity Catalog privileges; "
                "you need WRITE VOLUME on the target volume, or use a volume "
                "you own"
            )
        raise DatabricksAPIError(
            f"upload to {remote} failed: HTTP {exc.code}{hint}. {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DatabricksAPIError(f"upload unreachable: {exc.reason}") from exc

    remote_size = file_size(remote, workspace=workspace)
    if remote_size is not None and remote_size != local_size:
        raise DatabricksAPIError(
            f"upload size mismatch for {remote}: local {local_size} bytes, "
            f"remote {remote_size} bytes — treat the remote file as corrupt"
        )
    return {
        "local_path": str(src),
        "remote_path": remote,
        "bytes": local_size,
        "verified": remote_size == local_size,
        "http_status": status,
    }


def list_volume_dir(
    remote_dir: str, *, workspace: Optional[str] = None
) -> List[Dict[str, Any]]:
    """List a UC Volume directory.

    Note the field names: the Files API returns ``name``/``file_size``, NOT the
    legacy DBFS ``path``/``size``.  Reading the wrong keys yields a listing of
    ``None`` entries that looks like an empty directory.
    """
    remote = "/" + remote_dir.strip("/")
    quoted = "/".join(
        urllib.parse.quote(seg, safe="") for seg in remote.strip("/").split("/")
    )
    body = _request(workspace, f"/api/2.0/fs/directories/{quoted}", method="GET")
    return [
        {
            "name": c.get("name"),
            "path": c.get("path"),
            "bytes": c.get("file_size"),
            "is_directory": bool(c.get("is_directory")),
        }
        for c in (body.get("contents") or [])
    ]


def file_size(remote_path: str, *, workspace: Optional[str] = None) -> Optional[int]:
    """Byte count of a remote file, or None when it cannot be determined."""
    remote = "/" + remote_path.strip("/")
    parent, _, leaf = remote.rpartition("/")
    try:
        for item in list_volume_dir(parent or "/", workspace=workspace):
            if item.get("name") == leaf:
                size = item.get("bytes")
                return int(size) if size is not None else None
    except DatabricksAPIError:
        return None
    return None


def delete_remote_file(
    remote_path: str, *, workspace: Optional[str] = None
) -> Dict[str, Any]:
    """Delete a file from a UC Volume."""
    remote = "/" + remote_path.strip("/")
    quoted = "/".join(
        urllib.parse.quote(seg, safe="") for seg in remote.strip("/").split("/")
    )
    _request(workspace, f"/api/2.0/fs/files/{quoted}", method="DELETE")
    return {"remote_path": remote, "deleted": True}


def create_volume(
    catalog: str,
    schema: str,
    name: str,
    *,
    workspace: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a MANAGED UC Volume you own (so you have WRITE VOLUME on it)."""
    body = _request(
        workspace,
        "/api/2.1/unity-catalog/volumes",
        {
            "catalog_name": catalog,
            "schema_name": schema,
            "name": name,
            "volume_type": "MANAGED",
        },
    )
    return {
        "full_name": body.get("full_name"),
        "owner": body.get("owner"),
        "volume_path": f"/Volumes/{catalog}/{schema}/{name}",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cluster libraries
# ─────────────────────────────────────────────────────────────────────────────

def _library_spec(
    *,
    jar: Optional[str] = None,
    whl: Optional[str] = None,
    pypi: Optional[str] = None,
    maven: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the one-key library spec the API expects."""
    given = [x for x in (jar, whl, pypi, maven) if x]
    if len(given) != 1:
        raise LibraryError("specify exactly one of jar, whl, pypi, maven")
    if jar:
        return {"jar": jar}
    if whl:
        return {"whl": whl}
    if pypi:
        return {"pypi": {"package": pypi}}
    return {"maven": {"coordinates": maven}}


def library_status(
    cluster_id: str, *, workspace: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Per-library install state on a cluster.

    This is the ONLY place a failed install shows up — ``libraries/install``
    itself returns 200 before anything has been downloaded.
    """
    body = _request(
        workspace,
        "/api/2.0/libraries/cluster-status?cluster_id="
        f"{urllib.parse.quote(cluster_id, safe='')}",
        method="GET",
    )
    out = []
    for st in body.get("library_statuses") or []:
        lib = st.get("library") or {}
        ref = (
            lib.get("jar")
            or lib.get("whl")
            or (lib.get("pypi") or {}).get("package")
            or (lib.get("maven") or {}).get("coordinates")
            or str(lib)
        )
        out.append({
            "library": ref,
            "status": st.get("status"),
            "messages": st.get("messages") or [],
            "is_library_for_all_clusters": bool(
                st.get("is_library_for_all_clusters")
            ),
        })
    return out


def install_library(
    cluster_id: str,
    *,
    workspace: Optional[str] = None,
    jar: Optional[str] = None,
    whl: Optional[str] = None,
    pypi: Optional[str] = None,
    maven: Optional[str] = None,
    wait: bool = True,
    timeout_seconds: float = 900.0,
) -> Dict[str, Any]:
    """Install a library on a cluster and, by default, wait for the real verdict.

    The cluster must be RUNNING: installing onto a TERMINATED cluster is
    accepted by the API and then silently does nothing until the next start.

    ``wait=True`` polls ``cluster-status`` until the library reports a terminal
    state, because ``install`` returning 200 means only "request queued".
    """
    spec = _library_spec(jar=jar, whl=whl, pypi=pypi, maven=maven)
    ref = (
        spec.get("jar")
        or spec.get("whl")
        or (spec.get("pypi") or {}).get("package")
        or (spec.get("maven") or {}).get("coordinates")
    )

    state = cluster_state(cluster_id, workspace=workspace)
    if state != _CLUSTER_RUNNING:
        raise LibraryError(
            f"cluster {cluster_id} is {state}; start it before installing "
            "libraries (an install onto a stopped cluster is accepted and then "
            "ignored until the next start)"
        )

    _request(
        workspace,
        "/api/2.0/libraries/install",
        {"cluster_id": cluster_id, "libraries": [spec]},
    )

    if not wait:
        return {
            "cluster_id": cluster_id,
            "library": ref,
            "status": "PENDING",
            "note": "install queued; poll library_status for the real verdict",
        }

    deadline = time.monotonic() + timeout_seconds
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        for st in library_status(cluster_id, workspace=workspace):
            if st.get("library") == ref:
                last = st
                break
        status = str(last.get("status") or "")
        if status in _LIB_TERMINAL_STATES:
            if status == "FAILED":
                msgs = "; ".join(str(m) for m in (last.get("messages") or []))
                raise LibraryError(
                    f"library {ref} FAILED to install on {cluster_id}: "
                    f"{msgs or 'no message'}"
                )
            return {"cluster_id": cluster_id, "library": ref, "status": status}
        time.sleep(_POLL_INTERVAL_SECONDS)

    raise LibraryError(
        f"library {ref} still {last.get('status') or 'unknown'} after "
        f"{timeout_seconds:g}s on {cluster_id}"
    )


def uninstall_library(
    cluster_id: str,
    *,
    workspace: Optional[str] = None,
    jar: Optional[str] = None,
    whl: Optional[str] = None,
    pypi: Optional[str] = None,
    maven: Optional[str] = None,
) -> Dict[str, Any]:
    """Mark a library for removal.  Takes effect on the next cluster restart.

    The API does not remove it from the running JVM, so the returned payload
    says so rather than implying the library is gone.
    """
    spec = _library_spec(jar=jar, whl=whl, pypi=pypi, maven=maven)
    _request(
        workspace,
        "/api/2.0/libraries/uninstall",
        {"cluster_id": cluster_id, "libraries": [spec]},
    )
    return {
        "cluster_id": cluster_id,
        "library": (
            spec.get("jar")
            or spec.get("whl")
            or (spec.get("pypi") or {}).get("package")
            or (spec.get("maven") or {}).get("coordinates")
        ),
        "marked_for_removal": True,
        "note": "restart the cluster for the removal to take effect",
    }
