"""Documentation must not drift from the code.

Every assertion here compares two things that live in the repo, so it fails when
they diverge rather than freezing a snapshot of either. That distinction matters:
asserting "there are 21 commands" would break on every routine addition and cover
nothing, while asserting "every command is documented" stays true as the surface
grows and fails exactly when someone forgets a doc.

These checks exist because the review that produced them found four real drifts:
`AGENTS.md` still described an 8-module layout, `docs/troubleshooting.md` had no
compute coverage, `docs/tools.md` did not say why compute has no tools, and
`--single-user` was implemented but undocumented.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin"
DOCS = REPO / "docs"


def _markdown_files() -> list[Path]:
    return [p for p in REPO.rglob("*.md") if ".git" not in p.parts]


def _all_docs_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _markdown_files())


def _command_tree(plugin_pkg) -> dict[str, argparse.ArgumentParser]:
    """Build the real argparse tree the CLI registers, then flatten it."""
    import importlib

    cli = importlib.import_module("databricks_plugin.cli")
    top = argparse.ArgumentParser(prog="hermes")
    databricks = top.add_subparsers(dest="cmd").add_parser("databricks")
    cli.setup_parser(databricks)

    def walk(parser, prefix=""):
        found = {}
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    full = f"{prefix}{name}".strip()
                    found[full] = sub
                    found.update(walk(sub, prefix=f"{full} "))
        return found

    return walk(databricks)


def _leaf_commands(tree):
    """Commands with no further subcommands — the ones a user actually types."""
    return {
        name: parser
        for name, parser in tree.items()
        if not any(
            isinstance(a, argparse._SubParsersAction) for a in parser._actions
        )
    }


# ─── every command and flag is documented ───────────────────────────────────

def test_every_leaf_command_appears_in_the_docs(plugin_pkg):
    tree = _command_tree(plugin_pkg)
    text = _all_docs_text()
    missing = sorted(name for name in _leaf_commands(tree) if name not in text)
    assert not missing, f"undocumented commands: {missing}"


def test_every_command_line_flag_appears_in_the_docs(plugin_pkg):
    tree = _command_tree(plugin_pkg)
    text = _all_docs_text()
    missing = []
    for name, parser in sorted(_leaf_commands(tree).items()):
        for action in parser._actions:
            for opt in action.option_strings:
                if opt.startswith("--") and opt != "--help" and opt not in text:
                    missing.append(f"{name} {opt}")
    assert not missing, f"undocumented flags: {missing}"


# ─── the footprint invariant that justifies the CLI-only design ─────────────

def test_plugin_yaml_declares_exactly_the_schemas_that_exist(plugin_pkg):
    """provides_tools and schemas.py must agree — a drift hides a tool."""
    declared = set(
        re.findall(
            r"^\s*-\s*(databricks_\w+)",
            (PLUGIN / "plugin.yaml").read_text(encoding="utf-8"),
            re.M,
        )
    )
    defined = set(
        re.findall(
            r'"name":\s*"(databricks_\w+)"',
            (PLUGIN / "schemas.py").read_text(encoding="utf-8"),
        )
    )
    assert declared == defined, f"symmetric difference: {declared ^ defined}"


def test_compute_modules_define_no_tool_schemas(plugin_pkg):
    """Compute is CLI-only because it spends money.

    A schema appearing in compute.py / jobs.py / compute_cli.py would mean
    someone wired billable compute into the model's toolset — the one thing the
    design forbids. This asserts the property, not a tool count.
    """
    for module in ("compute.py", "jobs.py", "compute_cli.py"):
        source = (PLUGIN / module).read_text(encoding="utf-8")
        assert "register_tool" not in source, f"{module} registers a tool"


# ─── bundled skills stay wired and conformant ──────────────────────────────

def test_every_skill_on_disk_is_registered_and_vice_versa(plugin_pkg):
    """A skill missing from _SKILLS fails silently at runtime — it just isn't there."""
    init = (PLUGIN / "__init__.py").read_text(encoding="utf-8")
    block = init.split("_SKILLS = (")[1].split(")")[0]
    listed = set(re.findall(r'"([\w-]+)"', block))
    on_disk = {p.parent.name for p in PLUGIN.glob("skills/*/SKILL.md")}
    assert listed == on_disk, f"symmetric difference: {listed ^ on_disk}"


@pytest.mark.parametrize(
    "skill_md", sorted(PLUGIN.glob("skills/*/SKILL.md")), ids=lambda p: p.parent.name
)
def test_skill_description_fits_the_authoring_standard(skill_md):
    """Hermes standard: one sentence, <= 60 chars, ends with a period.

    Long descriptions dilute the model's attention when several skills load.
    """
    text = skill_md.read_text(encoding="utf-8")
    match = re.search(r'^description:\s*"?(.*?)"?\s*$', text, re.M)
    assert match, f"{skill_md.parent.name} has no description"
    desc = match.group(1)
    assert len(desc) <= 60, f"{len(desc)} chars: {desc!r}"
    assert desc.endswith("."), f"must end with a period: {desc!r}"


@pytest.mark.parametrize(
    "skill_md", sorted(PLUGIN.glob("skills/*/SKILL.md")), ids=lambda p: p.parent.name
)
def test_skill_name_matches_its_directory(skill_md):
    text = skill_md.read_text(encoding="utf-8")
    match = re.search(r"^name:\s*(\S+)\s*$", text, re.M)
    assert match, f"{skill_md.parent.name} has no name"
    assert match.group(1) == skill_md.parent.name


# ─── link hygiene ──────────────────────────────────────────────────────────

def _github_slug(heading: str) -> str:
    """GitHub's anchor algorithm.

    Note that ``\\w`` keeps underscores: collapsing them (as a naive slugifier
    does) reports false failures on headings like ``## databricks_status``.
    """
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"\s+", "-", slug).strip("-")


def test_every_relative_link_and_anchor_resolves():
    anchors = {
        p.resolve(): {
            _github_slug(h)
            for h in re.findall(r"^#{1,6}\s+(.*)$", p.read_text(encoding="utf-8"), re.M)
        }
        for p in _markdown_files()
    }

    broken = []
    for path in _markdown_files():
        for match in re.finditer(
            r"\[([^\]]*)\]\(([^)]+)\)", path.read_text(encoding="utf-8")
        ):
            target = match.group(2).strip()
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            file_part, _, fragment = target.partition("#")
            resolved = (
                (path.parent / file_part).resolve() if file_part else path.resolve()
            )
            if file_part and not resolved.exists():
                broken.append(f"{path.name} -> {target} (missing file)")
                continue
            known = anchors.get(resolved)
            if fragment and known is not None and fragment.lower() not in known:
                broken.append(f"{path.name} -> {target} (bad anchor)")

    assert not broken, "broken links:\n  " + "\n  ".join(broken)


def test_docs_referenced_by_the_readme_all_exist():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    referenced = set(re.findall(r"\((docs/[\w./-]+\.md)\)", readme))
    assert referenced, "README should link into docs/"
    missing = sorted(r for r in referenced if not (REPO / r).exists())
    assert not missing, f"README links to missing docs: {missing}"


def test_community_health_files_are_present():
    """GitHub's community profile checklist — these drive the health score."""
    for relative in (
        "README.md",
        "LICENSE",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        "CHANGELOG.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/workflows/ci.yml",
    ):
        assert (REPO / relative).exists(), f"missing {relative}"
