"""``hermes databricks cluster|library|job`` — compute operations.

Kept in its own module because these commands spend money, and that warrants a
confirmation layer the read-only catalog/query commands do not need.

The guardrails here are deliberate and are the reason this is CLI-only rather
than a set of model tools:

* every billable mutation (create / start / restart / job submit on a new
  cluster) prompts for confirmation unless ``--yes`` is passed;
* ``create`` requires an explicit ``--policy`` and refuses an autotermination
  below 10 minutes;
* nothing reports success it did not observe — see ``compute.py``.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

from . import compute as _compute
from . import jobs as _jobs
from .api import DatabricksAPIError


def _confirm(prompt: str, assume_yes: bool) -> bool:
    """Ask before spending money.  Non-interactive stdin declines."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"✗ {prompt}\n  refusing in a non-interactive shell; pass --yes")
        return False
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _kv(payload: Dict[str, Any], keys: List[str]) -> str:
    """Aligned key/value block, per the cross-plugin display convention."""
    present = [(k, payload.get(k)) for k in keys if payload.get(k) is not None]
    if not present:
        return "(no fields)"
    width = max(len(k) for k, _ in present)
    return "\n".join(f"  {k.ljust(width)}  {v}" for k, v in present)


def _table(rows: List[Dict[str, Any]], columns: List[str]) -> str:
    if not rows:
        return "(none)"

    def cell(v: Any) -> str:
        text = "" if v is None else str(v)
        return text if len(text) <= 38 else text[:35] + "..."

    widths = {c: max(len(c), *(len(cell(r.get(c))) for r in rows)) for c in columns}
    head = "  ".join(c.ljust(widths[c]) for c in columns)
    rule = "  ".join("-" * widths[c] for c in columns)
    body = ["  ".join(cell(r.get(c)).ljust(widths[c]) for c in columns) for r in rows]
    return "\n".join([head, rule, *body])


# ─────────────────────────────────────────────────────────────────────────────
# argparse
# ─────────────────────────────────────────────────────────────────────────────

def add_compute_parsers(actions) -> None:
    """Attach the cluster / library / job subcommand trees."""

    # ── COMPUTE ──────────────────────────────────────────────────────────
    cl = actions.add_parser("cluster", help="[COMPUTE] Manage clusters (billable)")
    cl_sub = cl.add_subparsers(dest="cluster_action")

    cl_list = cl_sub.add_parser("list", help="List clusters and their state")
    cl_list.add_argument("--mine", action="store_true", help="Only clusters I created")
    cl_list.add_argument("--workspace")

    cl_show = cl_sub.add_parser("show", help="Show one cluster in detail")
    cl_show.add_argument("cluster_id")
    cl_show.add_argument("--workspace")

    cl_new = cl_sub.add_parser("create", help="Create a cluster (starts billing)")
    cl_new.add_argument("name")
    cl_new.add_argument("--spark-version", required=True, help="e.g. 17.3.x-scala2.13")
    cl_new.add_argument("--node-type", required=True, help="e.g. Standard_D4ds_v5")
    cl_new.add_argument("--workers", type=int, default=0,
                        help="0 = single node (default)")
    cl_new.add_argument("--autotermination", type=int, default=30,
                        help="Minutes idle before auto-stop (min 10, default 30)")
    cl_new.add_argument("--policy", help="Cluster policy id (see: cluster policies)")
    cl_new.add_argument("--security-mode",
                        help="e.g. SINGLE_USER, USER_ISOLATION")
    cl_new.add_argument("--single-user", help="Principal for SINGLE_USER mode")
    cl_new.add_argument("--wait", action="store_true", help="Block until RUNNING")
    cl_new.add_argument("--yes", action="store_true", help="Skip confirmation")
    cl_new.add_argument("--workspace")

    for verb, helptext in (
        ("start", "Start a stopped cluster (starts billing)"),
        ("restart", "Restart a cluster (also reloads libraries)"),
        ("stop", "Terminate a cluster (stops billing)"),
    ):
        p = cl_sub.add_parser(verb, help=helptext)
        p.add_argument("cluster_id")
        p.add_argument("--no-wait", action="store_true",
                       help="Return immediately instead of polling")
        p.add_argument("--yes", action="store_true", help="Skip confirmation")
        p.add_argument("--workspace")

    cl_pol = cl_sub.add_parser("policies", help="List usable cluster policies")
    cl_pol.add_argument("--workspace")

    cl_nt = cl_sub.add_parser("node-types", help="List node types, cheapest first")
    cl_nt.add_argument("--limit", type=int, default=15)
    cl_nt.add_argument("--workspace")

    cl_sv = cl_sub.add_parser("versions", help="List DBR runtimes (LTS by default)")
    cl_sv.add_argument("--all", action="store_true", help="Include non-LTS")
    cl_sv.add_argument("--workspace")

    # ── LIBRARIES ────────────────────────────────────────────────────────
    lib = actions.add_parser("library", help="[LIBRARIES] Upload and install libraries")
    lib_sub = lib.add_subparsers(dest="library_action")

    lib_up = lib_sub.add_parser(
        "upload", help="Upload a local file to a UC Volume (byte-verified)"
    )
    lib_up.add_argument("local_path")
    lib_up.add_argument("remote_path", help="/Volumes/<catalog>/<schema>/<vol>/<file>")
    lib_up.add_argument("--workspace")

    lib_ls = lib_sub.add_parser("ls", help="List a UC Volume directory")
    lib_ls.add_argument("remote_dir")
    lib_ls.add_argument("--workspace")

    lib_rm = lib_sub.add_parser("rm", help="Delete a file from a UC Volume")
    lib_rm.add_argument("remote_path")
    lib_rm.add_argument("--yes", action="store_true")
    lib_rm.add_argument("--workspace")

    lib_vol = lib_sub.add_parser(
        "create-volume", help="Create a MANAGED volume you own (grants you WRITE)"
    )
    lib_vol.add_argument("catalog")
    lib_vol.add_argument("schema")
    lib_vol.add_argument("name")
    lib_vol.add_argument("--workspace")

    lib_in = lib_sub.add_parser(
        "install", help="Install a library on a RUNNING cluster and verify it"
    )
    lib_in.add_argument("cluster_id")
    lib_in.add_argument("--jar", help="/Volumes/... path to a JAR")
    lib_in.add_argument("--whl", help="/Volumes/... path to a wheel")
    lib_in.add_argument("--pypi", help="PyPI package spec")
    lib_in.add_argument("--maven", help="Maven coordinates")
    lib_in.add_argument("--no-wait", action="store_true",
                        help="Do not wait for the install verdict")
    lib_in.add_argument("--workspace")

    lib_st = lib_sub.add_parser("status", help="Per-library install state on a cluster")
    lib_st.add_argument("cluster_id")
    lib_st.add_argument("--workspace")

    lib_un = lib_sub.add_parser(
        "uninstall", help="Mark a library for removal (needs a restart)"
    )
    lib_un.add_argument("cluster_id")
    lib_un.add_argument("--jar")
    lib_un.add_argument("--whl")
    lib_un.add_argument("--pypi")
    lib_un.add_argument("--maven")
    lib_un.add_argument("--workspace")

    # ── JOBS ─────────────────────────────────────────────────────────────
    job = actions.add_parser("job", help="[JOBS] Run Spark code and read the result")
    job_sub = job.add_subparsers(dest="job_action")

    js = job_sub.add_parser(
        "submit",
        help="Submit a one-off run (ephemeral cluster by default) and wait",
    )
    js.add_argument("--name", default="hermes-run", help="Run name")
    js.add_argument("--cluster", help="Reuse this warm cluster instead of ephemeral")
    js.add_argument("--spark-version", help="Ephemeral: DBR version")
    js.add_argument("--node-type", help="Ephemeral: node type")
    js.add_argument("--workers", type=int, default=0, help="Ephemeral: 0 = single node")
    js.add_argument("--policy", help="Ephemeral: cluster policy id")
    js.add_argument("--security-mode", help="Ephemeral: data security mode")
    js.add_argument("--notebook", help="Task: workspace notebook path")
    js.add_argument("--python-file", help="Task: /Volumes/... .py file")
    js.add_argument("--main-class", help="Task: JVM main class (needs --jar)")
    js.add_argument("--sql-query-id", help="Task: a SAVED query id (runs on a warehouse)")
    js.add_argument("--warehouse", help="Warehouse id for --sql-query-id")
    js.add_argument("--jar", action="append", default=[],
                    help="Library JAR (/Volumes/...); repeatable")
    js.add_argument("--whl", action="append", default=[], help="Library wheel")
    js.add_argument("--pypi", action="append", default=[], help="PyPI package")
    js.add_argument("--maven", action="append", default=[], help="Maven coordinates")
    js.add_argument("--param", action="append", default=[],
                    help="Task parameter; repeatable")
    js.add_argument("--timeout", type=int, default=3600, help="Run timeout seconds")
    js.add_argument("--no-wait", action="store_true", help="Submit and return the id")
    js.add_argument("--yes", action="store_true", help="Skip confirmation")
    js.add_argument("--json", action="store_true")
    js.add_argument("--workspace")

    jg = job_sub.add_parser("show", help="Show a run's state and verdict")
    jg.add_argument("run_id")
    jg.add_argument("--json", action="store_true")
    jg.add_argument("--workspace")

    jo = job_sub.add_parser("output", help="Show a run's output, logs and stack trace")
    jo.add_argument("run_id")
    jo.add_argument("--json", action="store_true")
    jo.add_argument("--workspace")

    jl = job_sub.add_parser("list", help="Recent runs")
    jl.add_argument("--limit", type=int, default=20)
    jl.add_argument("--active", action="store_true", help="Only runs still billing")
    jl.add_argument("--workspace")

    jc = job_sub.add_parser("cancel", help="Cancel a run (stops ephemeral billing)")
    jc.add_argument("run_id")
    jc.add_argument("--workspace")


# ─────────────────────────────────────────────────────────────────────────────
# dispatch
# ─────────────────────────────────────────────────────────────────────────────

_CLUSTER_COLUMNS = [
    "cluster_id", "name", "state", "spark_version", "node_type_id",
    "autotermination_minutes", "creator",
]
_SHOW_KEYS = [
    "cluster_id", "name", "state", "state_message", "spark_version",
    "node_type_id", "num_workers", "autotermination_minutes",
    "data_security_mode", "single_user_name", "policy_id", "creator",
]
_RUN_KEYS = [
    "run_id", "run_name", "life_cycle_state", "result_state", "succeeded",
    "failure_summary", "run_page_url", "billing_note",
]


def _handle_cluster(args) -> int:
    action = getattr(args, "cluster_action", None)
    ws = getattr(args, "workspace", None)

    if action == "list":
        rows = _compute.list_clusters(workspace=ws, mine_only=bool(args.mine))
        print(_table(rows, _CLUSTER_COLUMNS))
        running = [r for r in rows if r.get("state") == "RUNNING"]
        if running:
            print(f"\n⚠ {len(running)} cluster(s) RUNNING and billing: "
                  + ", ".join(str(r.get("cluster_id")) for r in running))
        return 0

    if action == "show":
        print(_kv(_compute.get_cluster(args.cluster_id, workspace=ws), _SHOW_KEYS))
        return 0

    if action == "policies":
        print(_table(_compute.list_cluster_policies(workspace=ws),
                     ["policy_id", "name"]))
        return 0

    if action == "node-types":
        print(_table(_compute.list_node_types(workspace=ws, max_results=args.limit),
                     ["node_type_id", "num_cores", "memory_mb"]))
        return 0

    if action == "versions":
        rows = _compute.list_spark_versions(workspace=ws, lts_only=not args.all)
        print(_table(rows, ["key", "name"]))
        return 0

    if action == "create":
        if not args.policy:
            print("✗ --policy is required. A policy caps size and cost; creating "
                  "without one is how a surprise bill happens.")
            print("  list them with: hermes databricks cluster policies")
            return 1
        shape = "single-node" if args.workers <= 0 else f"{args.workers} workers"
        if not _confirm(
            f"Create cluster {args.name!r} ({shape}, {args.node_type}, "
            f"auto-stop {args.autotermination}m)? This starts billing.",
            args.yes,
        ):
            print("aborted")
            return 1
        record = _compute.create_cluster(
            name=args.name,
            spark_version=args.spark_version,
            node_type_id=args.node_type,
            workspace=ws,
            num_workers=args.workers,
            autotermination_minutes=args.autotermination,
            policy_id=args.policy,
            data_security_mode=args.security_mode,
            single_user_name=args.single_user,
            wait=bool(args.wait),
        )
        print(_kv(record, _SHOW_KEYS))
        if not args.wait:
            print("\n  creating — poll with: hermes databricks cluster show "
                  f"{record.get('cluster_id')}")
        return 0

    if action in {"start", "restart", "stop"}:
        wait = not args.no_wait
        if action in {"start", "restart"} and not _confirm(
            f"{action.capitalize()} cluster {args.cluster_id}? This starts billing.",
            args.yes,
        ):
            print("aborted")
            return 1
        fn = {
            "start": _compute.start_cluster,
            "restart": _compute.restart_cluster,
            "stop": _compute.stop_cluster,
        }[action]
        # stop() defaults to not waiting; start/restart default to waiting.
        record = fn(args.cluster_id, workspace=ws, wait=wait)
        print(_kv(record, _SHOW_KEYS))
        if record.get("already_running"):
            print("\n  (already RUNNING — no action taken)")
        if record.get("already_terminated"):
            print("\n  (already TERMINATED — no action taken)")
        return 0

    print("Unknown cluster action. Run: hermes databricks cluster --help")
    return 1


def _handle_library(args) -> int:
    action = getattr(args, "library_action", None)
    ws = getattr(args, "workspace", None)

    if action == "upload":
        record = _compute.upload_file(args.local_path, args.remote_path, workspace=ws)
        print(_kv(record, ["local_path", "remote_path", "bytes", "verified"]))
        print("\n  install it with: hermes databricks library install <cluster_id> "
              f"--jar {record['remote_path']}")
        return 0

    if action == "ls":
        rows = _compute.list_volume_dir(args.remote_dir, workspace=ws)
        print(_table(rows, ["name", "bytes", "is_directory"]))
        return 0

    if action == "rm":
        if not _confirm(f"Delete {args.remote_path}?", args.yes):
            print("aborted")
            return 1
        print(_kv(_compute.delete_remote_file(args.remote_path, workspace=ws),
                  ["remote_path", "deleted"]))
        return 0

    if action == "create-volume":
        record = _compute.create_volume(
            args.catalog, args.schema, args.name, workspace=ws
        )
        print(_kv(record, ["full_name", "owner", "volume_path"]))
        return 0

    if action == "install":
        record = _compute.install_library(
            args.cluster_id,
            workspace=ws,
            jar=args.jar, whl=args.whl, pypi=args.pypi, maven=args.maven,
            wait=not args.no_wait,
        )
        print(_kv(record, ["cluster_id", "library", "status", "note"]))
        return 0

    if action == "status":
        rows = _compute.library_status(args.cluster_id, workspace=ws)
        print(_table(rows, ["library", "status"]))
        failed = [r for r in rows if r.get("status") == "FAILED"]
        for r in failed:
            print(f"\n✗ {r.get('library')}: "
                  + "; ".join(str(m) for m in (r.get("messages") or [])))
        return 1 if failed else 0

    if action == "uninstall":
        print(_kv(
            _compute.uninstall_library(
                args.cluster_id, workspace=ws,
                jar=args.jar, whl=args.whl, pypi=args.pypi, maven=args.maven,
            ),
            ["cluster_id", "library", "marked_for_removal", "note"],
        ))
        return 0

    print("Unknown library action. Run: hermes databricks library --help")
    return 1


def _render_run(record: Dict[str, Any]) -> str:
    lines = [_kv(record, _RUN_KEYS)]
    tasks = record.get("tasks") or []
    if tasks:
        lines.append("\nTasks:")
        lines.append(_table(
            tasks, ["task_key", "life_cycle_state", "result_state", "run_id"]
        ))
    out = record.get("output") or {}
    if out.get("error"):
        lines.append(f"\n✗ error: {out['error']}")
        if out.get("error_trace"):
            trace = str(out["error_trace"])
            lines.append("  trace (first 2000 chars):")
            lines.append("  " + trace[:2000].replace("\n", "\n  "))
    elif out.get("notebook_output") is not None:
        lines.append(f"\nresult: {out['notebook_output']}")
    if out.get("logs"):
        logs = str(out["logs"])
        lines.append("\nlogs (tail):")
        lines.append("  " + logs[-1500:].replace("\n", "\n  "))
    return "\n".join(lines)


def _handle_job(args) -> int:
    action = getattr(args, "job_action", None)
    ws = getattr(args, "workspace", None)

    if action == "submit":
        # The api layer rejects both compute shapes at once, but the CLI must
        # not quietly drop one before it gets there: passing --cluster together
        # with --spark-version/--node-type means the caller is confused about
        # WHERE the code runs and what it costs, which is exactly the mistake
        # worth surfacing rather than resolving by precedence.
        if args.cluster and (args.spark_version or args.node_type):
            print("✗ pass either --cluster <id> (reuse a warm cluster) or "
                  "--spark-version + --node-type (a new ephemeral cluster), "
                  "not both — they mean different compute and different cost")
            return 1

        ephemeral = not args.cluster
        if ephemeral and not args.sql_query_id:
            if not (args.spark_version and args.node_type):
                print("✗ ephemeral runs need --spark-version and --node-type "
                      "(or pass --cluster <id> to reuse a warm cluster, or "
                      "--sql-query-id to run a saved query on a warehouse)")
                print("  see: hermes databricks cluster versions | node-types")
                return 1
            target = (f"a NEW ephemeral cluster ({args.node_type}, "
                      f"{'single-node' if args.workers <= 0 else f'{args.workers} workers'})")
        elif args.cluster:
            target = f"existing cluster {args.cluster}"
        else:
            target = "a SQL warehouse"

        if not _confirm(f"Submit run {args.name!r} on {target}? This costs money.",
                        args.yes):
            print("aborted")
            return 1

        kwargs: Dict[str, Any] = {
            "run_name": args.name,
            "workspace": ws,
            "jars": args.jar or None,
            "whls": args.whl or None,
            "pypi": args.pypi or None,
            "maven": args.maven or None,
            "timeout_seconds": args.timeout,
        }
        if args.cluster:
            kwargs["existing_cluster_id"] = args.cluster
        elif not args.sql_query_id:
            kwargs.update({
                "spark_version": args.spark_version,
                "node_type_id": args.node_type,
                "num_workers": args.workers,
                "policy_id": args.policy,
                "data_security_mode": args.security_mode,
            })
        if args.notebook:
            kwargs["notebook_path"] = args.notebook
        elif args.python_file:
            kwargs["spark_python_file"] = args.python_file
            kwargs["python_params"] = args.param or None
        elif args.main_class:
            kwargs["main_class_name"] = args.main_class
            kwargs["jar_params"] = args.param or None
        elif args.sql_query_id:
            kwargs["sql_query_id"] = args.sql_query_id
            kwargs["warehouse_id"] = args.warehouse

        if args.no_wait:
            submitted = _jobs.submit_run(**kwargs)
            print(_kv(submitted, ["run_id", "billing_note"]))
            print(f"\n  poll with: hermes databricks job show {submitted['run_id']}")
            return 0

        record = _jobs.submit_and_wait(poll_timeout_seconds=args.timeout + 600,
                                       **kwargs)
        if args.json:
            print(json.dumps(record, indent=2, default=str))
        else:
            print(_render_run(record))
        return 0 if record.get("succeeded") else 1

    if action == "show":
        record = _jobs.get_run(args.run_id, workspace=ws)
        if args.json:
            print(json.dumps(record, indent=2, default=str))
        else:
            print(_render_run(record))
        return 0

    if action == "output":
        record = _jobs.run_output(args.run_id, workspace=ws)
        if args.json:
            print(json.dumps(record, indent=2, default=str))
        else:
            print(_render_run({"run_id": args.run_id, "output": record}))
        return 0

    if action == "list":
        rows = _jobs.list_runs(workspace=ws, limit=args.limit,
                               active_only=bool(args.active))
        print(_table(rows, ["run_id", "run_name", "life_cycle_state",
                            "result_state", "start_time"]))
        return 0

    if action == "cancel":
        print(_kv(_jobs.cancel_run(args.run_id, workspace=ws),
                  ["run_id", "cancel_requested"]))
        return 0

    print("Unknown job action. Run: hermes databricks job --help")
    return 1


def handle_compute_cli(args) -> Optional[int]:
    """Dispatch a compute action, or return None when it is not one of ours."""
    action = getattr(args, "databricks_action", None)
    handler = {
        "cluster": _handle_cluster,
        "library": _handle_library,
        "job": _handle_job,
    }.get(str(action))
    if handler is None:
        return None
    try:
        return handler(args)
    except DatabricksAPIError as exc:
        print(f"✗ {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n⚠ interrupted — a submitted run may still be executing.")
        print("  check: hermes databricks job list --active")
        return 130
