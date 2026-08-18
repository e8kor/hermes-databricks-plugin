"""Behaviour contracts for the Unity Catalog layer.

These cover the two bugs that shipped in 1.0.0 and were found by running the
plugin against a live workspace: silently-partial paginated listings, and an
empty ``columns`` list being indistinguishable from "this table has no columns".
No network access — the HTTP boundary is stubbed with real response shapes.
"""

from __future__ import annotations

import pytest


def _table(name, columns=None, kind="TABLE"):
    """A Unity Catalog table payload, shaped like the real API's."""
    return {
        "name": name,
        "full_name": f"samples.demo.{name}",
        "table_type": "MANAGED",
        "securable_kind": kind,
        "columns": [
            {"name": c, "type_text": "string", "type_name": "STRING"}
            for c in (columns or [])
        ],
    }


class TestPagination:
    """A paginated listing must return every page, not just the first."""

    def test_tables_follow_every_page(self, api, monkeypatch):
        pages = [
            {"tables": [_table("a"), _table("b")], "next_page_token": "t1"},
            {"tables": [_table("c")], "next_page_token": "t2"},
            {"tables": [_table("d")]},
        ]
        calls = []

        def fake_request(workspace, path, payload=None, method="POST"):
            calls.append(path)
            return pages[len(calls) - 1]

        monkeypatch.setattr(api, "_request", fake_request)
        result = api.list_catalog(catalog="samples", schema="demo")

        assert result["count"] == 4
        assert [t["name"] for t in result["tables"]] == ["a", "b", "c", "d"]
        # Later requests must carry the token the server handed back.
        assert "page_token=t1" in calls[1]
        assert "page_token=t2" in calls[2]

    def test_catalogs_and_schemas_paginate_too(self, api, monkeypatch):
        pages = [
            {"catalogs": [{"name": "one"}], "next_page_token": "n"},
            {"catalogs": [{"name": "two"}]},
        ]
        monkeypatch.setattr(
            api,
            "_request",
            lambda ws, path, payload=None, method="POST": pages.pop(0),
        )
        assert api.list_catalog()["catalogs"] == ["one", "two"]

    def test_repeated_token_does_not_loop_forever(self, api, monkeypatch):
        """A server echoing the same token must not hang the caller."""
        monkeypatch.setattr(
            api,
            "_request",
            lambda ws, path, payload=None, method="POST": {
                "schemas": [{"name": "s"}],
                "next_page_token": "same",
            },
        )
        result = api.list_catalog(catalog="samples")
        assert result["schemas"] == ["s", "s"]

    def test_page_token_is_url_encoded(self, api, monkeypatch):
        """Tokens are opaque and may contain characters that need escaping."""
        pages = [
            {"catalogs": [{"name": "a"}], "next_page_token": "a b/c+d"},
            {"catalogs": [{"name": "b"}]},
        ]
        seen = []

        def fake_request(workspace, path, payload=None, method="POST"):
            seen.append(path)
            return pages[len(seen) - 1]

        monkeypatch.setattr(api, "_request", fake_request)
        api.list_catalog()
        assert " " not in seen[1]
        assert "a%20b%2Fc%2Bd" in seen[1]

    def test_catalog_and_schema_names_are_escaped(self, api, monkeypatch):
        """A name is a query-string value, so nothing in it is 'safe'."""
        seen = []

        def fake_request(workspace, path, payload=None, method="POST"):
            seen.append(path)
            return {"tables": []}

        monkeypatch.setattr(api, "_request", fake_request)
        api.list_catalog(catalog="cat/one", schema="sch two&x")

        assert "catalog_name=cat%2Fone" in seen[0]
        assert "schema_name=sch%20two%26x" in seen[0]
        # The '&' in a name must not be readable as a parameter separator.
        assert seen[0].count("&") == 1


class TestColumnVisibility:
    """An empty column list is reported as unknown, never as "none"."""

    def test_missing_columns_are_counted_and_flagged(self, api, monkeypatch):
        monkeypatch.setattr(
            api,
            "_request",
            lambda ws, path, payload=None, method="POST": {
                "tables": [
                    _table("with_cols", ["x", "y"]),
                    _table("without_cols", [], kind="TABLE_DELTASHARING"),
                ]
            },
        )
        result = api.list_catalog(catalog="samples", schema="demo")

        by_name = {t["name"]: t for t in result["tables"]}
        assert by_name["with_cols"]["column_count"] == 2
        assert by_name["without_cols"]["column_count"] == 0
        # The caller must be told this is missing metadata, not an empty table.
        assert result["columns_unavailable_for"] == 1
        assert "note" in result

    def test_no_note_when_every_table_reports_columns(self, api, monkeypatch):
        monkeypatch.setattr(
            api,
            "_request",
            lambda ws, path, payload=None, method="POST": {
                "tables": [_table("a", ["x"]), _table("b", ["y"])]
            },
        )
        result = api.list_catalog(catalog="samples", schema="demo")
        assert "columns_unavailable_for" not in result
        assert "note" not in result


class TestDescribeTable:
    """Single-table describe prefers UC, falls back to SQL, and says which."""

    def test_uses_unity_catalog_when_it_answers(self, api, monkeypatch):
        monkeypatch.setattr(
            api,
            "_request",
            lambda ws, path, payload=None, method="POST": _table("t", ["a", "b"]),
        )
        monkeypatch.setattr(
            api,
            "execute_statement",
            lambda *a, **k: pytest.fail("must not spend a warehouse round-trip"),
        )
        result = api.list_catalog(catalog="samples", schema="demo", table="t")

        assert result["columns_source"] == "unity-catalog"
        assert [c["name"] for c in result["columns"]] == ["a", "b"]
        assert result["column_count"] == 2

    def test_falls_back_to_describe_table(self, api, monkeypatch):
        """The DELTASHARING case: UC returns nothing, DESCRIBE returns the schema."""
        monkeypatch.setattr(
            api,
            "_request",
            lambda ws, path, payload=None, method="POST": _table(
                "t", [], kind="TABLE_DELTASHARING"
            ),
        )
        monkeypatch.setattr(api, "execute_statement", lambda *a, **k: {"body": True})
        monkeypatch.setattr(
            api,
            "iter_all_rows",
            lambda body, **k: iter(
                [["c_custkey", "bigint", None], ["c_name", "string", None]]
            ),
        )
        result = api.list_catalog(catalog="samples", schema="demo", table="t")

        assert result["columns_source"] == "describe-table"
        assert [c["name"] for c in result["columns"]] == ["c_custkey", "c_name"]
        assert result["columns"][0]["type"] == "bigint"

    def test_describe_stops_at_the_detail_sections(self, api, monkeypatch):
        """DESCRIBE appends '# Partition Information' etc — not columns."""
        monkeypatch.setattr(
            api, "_request", lambda ws, path, payload=None, method="POST": _table("t")
        )
        monkeypatch.setattr(api, "execute_statement", lambda *a, **k: {})
        monkeypatch.setattr(
            api,
            "iter_all_rows",
            lambda body, **k: iter(
                [
                    ["real_col", "int", None],
                    ["", "", ""],
                    ["# Partition Information", "", ""],
                    ["not_a_col", "int", None],
                ]
            ),
        )
        result = api.list_catalog(catalog="samples", schema="demo", table="t")
        assert [c["name"] for c in result["columns"]] == ["real_col"]

    def test_survives_unity_catalog_error(self, api, monkeypatch):
        """A 403 on the metadata GET must not block the SQL fallback."""

        def boom(*a, **k):
            raise api.DatabricksAPIError("HTTP 403 PERMISSION_DENIED")

        monkeypatch.setattr(api, "_request", boom)
        monkeypatch.setattr(api, "execute_statement", lambda *a, **k: {})
        monkeypatch.setattr(
            api, "iter_all_rows", lambda body, **k: iter([["c", "int", None]])
        )
        result = api.list_catalog(catalog="samples", schema="demo", table="t")
        assert result["columns_source"] == "describe-table"
        assert result["full_name"] == "samples.demo.t"


class TestTruncationSignal:
    """The platform's silent-row-drop flag must never be swallowed."""

    def test_truncated_manifest_is_surfaced(self, api):
        assert api.is_truncated({"manifest": {"truncated": True}}) is True
        assert api.is_truncated({"manifest": {}}) is False
        assert api.is_truncated({}) is False

    def test_result_stats_reports_truncation_with_counts(self, api):
        stats = api.result_stats({
            "statement_id": "abc",
            "manifest": {
                "truncated": True,
                "total_row_count": 9166,
                "schema": {"columns": [{"name": "a"}, {"name": "b"}]},
            },
        })
        assert stats["truncated"] is True
        assert stats["total_rows"] == 9166
        assert stats["columns"] == ["a", "b"]

    def test_hard_inline_overflow_is_reclassified(self, api):
        """A FAILED-with-inline-limit is actionable, not a generic failure."""
        with pytest.raises(api.InlineLimitExceeded):
            api._check_succeeded({
                "status": {
                    "state": "FAILED",
                    "error": {"message": "Inline byte limit exceeded for statement"},
                }
            })

    def test_other_failures_stay_generic(self, api):
        with pytest.raises(api.DatabricksAPIError) as excinfo:
            api._check_succeeded({
                "status": {"state": "FAILED", "error": {"message": "syntax error"}}
            })
        assert not isinstance(excinfo.value, api.InlineLimitExceeded)


class TestRowNormalization:
    """Both row shapes the platform emits must decode identically."""

    def test_plain_arrays(self, api):
        assert api.normalize_rows([["a", "b"], ["c", "d"]]) == [["a", "b"], ["c", "d"]]

    def test_typed_value_objects(self, api):
        rows = api.normalize_rows([
            {"values": [{"string_value": "x"}, {"is_null": True}]}
        ])
        assert rows == [["x", None]]

    def test_empty_input(self, api):
        assert api.normalize_rows(None) == []
        assert api.normalize_rows([]) == []
