"""Contracts for CLI-profile-driven configuration.

The point of the profile layer is that connection identity lives in the
Databricks CLI, so these assert the resolution rules rather than any particular
config file content: derived values fill gaps, explicit values always win, and a
misconfiguration fails loudly instead of quietly querying the wrong workspace.
"""

from __future__ import annotations

import pytest

HOST = "https://adb-123.18.azuredatabricks.net"


@pytest.fixture
def fake_profiles(config_mod, profiles_mod, monkeypatch):
    """Install a controllable set of CLI profiles."""

    def install(profiles, default=None):
        monkeypatch.setattr(profiles_mod, "list_profiles", lambda **k: list(profiles))
        monkeypatch.setattr(
            profiles_mod,
            "get_profile",
            lambda name: next(
                (p for p in profiles if p.get("name") == name), None
            ),
        )
        monkeypatch.setattr(profiles_mod, "default_profile_name", lambda: default)

    return install


@pytest.fixture
def hermes_config(config_mod, monkeypatch):
    """Pretend the Hermes config has a given ``databricks:`` section."""

    def install(section):
        monkeypatch.setattr(config_mod, "_load_raw_config", lambda: section or {})

    return install


class TestZeroConfig:
    """A logged-in CLI should need no Hermes config at all."""

    def test_default_profile_is_adopted(
        self, config_mod, hermes_config, fake_profiles
    ):
        hermes_config({})
        fake_profiles(
            [{"name": "prod", "host": HOST, "default": True}], default="prod"
        )

        workspaces = config_mod.list_workspaces()
        assert list(workspaces) == ["cli-default"]

        name, entry = config_mod.resolve_workspace()
        assert name == "cli-default"
        assert entry["host"] == HOST
        assert entry["auth"]["key_cmd"] == 'databricks auth token -p "prod"'
        assert config_mod.is_configured() is True

    def test_no_profiles_means_unconfigured(
        self, config_mod, hermes_config, fake_profiles
    ):
        hermes_config({})
        fake_profiles([], default=None)

        assert config_mod.list_workspaces() == {}
        assert config_mod.is_configured() is False
        with pytest.raises(config_mod.DatabricksConfigError) as excinfo:
            config_mod.resolve_workspace()
        # The error must tell the user how to fix it.
        assert "auth login" in str(excinfo.value)


class TestProfileReference:
    """Naming a profile derives host and auth from it."""

    def test_host_and_auth_are_derived(
        self, config_mod, hermes_config, fake_profiles
    ):
        hermes_config({"workspaces": {"main": {"profile": "prod"}}})
        fake_profiles([{"name": "prod", "host": HOST}])

        _, entry = config_mod.resolve_workspace("main")
        assert entry["host"] == HOST
        assert entry["auth"]["key_cmd"] == 'databricks auth token -p "prod"'
        assert config_mod.auth_kind("main") == "cli-profile:prod"

    def test_explicit_host_wins_over_profile(
        self, config_mod, hermes_config, fake_profiles
    ):
        override = "https://other.databricks.com"
        hermes_config({"workspaces": {"main": {"profile": "prod", "host": override}}})
        fake_profiles([{"name": "prod", "host": HOST}])

        _, entry = config_mod.resolve_workspace("main")
        assert entry["host"] == override

    def test_explicit_auth_wins_over_profile(
        self, config_mod, hermes_config, fake_profiles
    ):
        """A profile may be named purely for its host."""
        hermes_config({
            "workspaces": {
                "main": {"profile": "prod", "auth": {"token_env": "MY_TOKEN"}}
            }
        })
        fake_profiles([{"name": "prod", "host": HOST}])

        _, entry = config_mod.resolve_workspace("main")
        assert entry["host"] == HOST
        assert "key_cmd" not in entry["auth"]
        assert config_mod.auth_kind("main") == "token_env"

    def test_unknown_profile_fails_loudly(
        self, config_mod, hermes_config, fake_profiles
    ):
        """Never silently fall back to a different workspace."""
        hermes_config({"workspaces": {"main": {"profile": "gone"}}})
        fake_profiles([{"name": "prod", "host": HOST}])

        with pytest.raises(config_mod.DatabricksConfigError) as excinfo:
            config_mod.resolve_workspace("main")
        message = str(excinfo.value)
        assert "gone" in message
        assert "prod" in message  # tells you what IS available
        assert config_mod.is_configured() is False

    def test_profile_without_host_needs_explicit_host(
        self, config_mod, hermes_config, fake_profiles
    ):
        hermes_config({"workspaces": {"main": {"profile": "hostless"}}})
        fake_profiles([{"name": "hostless", "host": None}])

        with pytest.raises(config_mod.DatabricksConfigError) as excinfo:
            config_mod.resolve_workspace("main")
        assert "host" in str(excinfo.value)


class TestExplicitConfig:
    """The pre-profile config shape must keep working."""

    def test_host_plus_key_cmd(self, config_mod, hermes_config, fake_profiles):
        hermes_config({
            "workspaces": {
                "main": {"host": HOST, "auth": {"key_cmd": "print-my-token"}}
            }
        })
        fake_profiles([])

        _, entry = config_mod.resolve_workspace("main")
        assert entry["host"] == HOST
        assert entry["auth"]["key_cmd"] == "print-my-token"
        assert config_mod.auth_kind("main") == "key_cmd"
        assert config_mod.is_configured() is True

    def test_host_without_auth_is_not_configured(
        self, config_mod, hermes_config, fake_profiles
    ):
        hermes_config({"workspaces": {"main": {"host": HOST}}})
        fake_profiles([])
        assert config_mod.is_configured() is False
        assert config_mod.auth_kind("main") == "none"

    def test_trailing_slash_is_stripped(
        self, config_mod, hermes_config, fake_profiles
    ):
        hermes_config({
            "workspaces": {"main": {"host": HOST + "/", "auth": {"key_cmd": "x"}}}
        })
        fake_profiles([])
        _, entry = config_mod.resolve_workspace("main")
        assert entry["host"] == HOST


class TestWorkspaceSelection:
    def test_explicit_default_is_honoured(
        self, config_mod, hermes_config, fake_profiles
    ):
        hermes_config({
            "default_workspace": "prod",
            "workspaces": {
                "dev": {"host": HOST, "auth": {"key_cmd": "x"}},
                "prod": {"host": HOST, "auth": {"key_cmd": "y"}},
            },
        })
        fake_profiles([])
        assert config_mod.default_workspace_name() == "prod"
        assert config_mod.resolve_workspace()[0] == "prod"

    def test_ambiguous_selection_refuses_to_guess(
        self, config_mod, hermes_config, fake_profiles
    ):
        hermes_config({
            "workspaces": {
                "dev": {"host": HOST, "auth": {"key_cmd": "x"}},
                "prod": {"host": HOST, "auth": {"key_cmd": "y"}},
            }
        })
        fake_profiles([])
        assert config_mod.default_workspace_name() is None
        with pytest.raises(config_mod.DatabricksConfigError):
            config_mod.resolve_workspace()

    def test_sole_workspace_is_the_default(
        self, config_mod, hermes_config, fake_profiles
    ):
        hermes_config({"workspaces": {"only": {"host": HOST, "auth": {"key_cmd": "x"}}}})
        fake_profiles([])
        assert config_mod.default_workspace_name() == "only"

    def test_unknown_workspace_name_fails(
        self, config_mod, hermes_config, fake_profiles
    ):
        hermes_config({"workspaces": {"main": {"host": HOST, "auth": {"key_cmd": "x"}}}})
        fake_profiles([])
        with pytest.raises(config_mod.DatabricksConfigError):
            config_mod.resolve_workspace("nope")


class TestTokenParsing:
    """A minting helper may print JSON or a bare token."""

    def test_json_with_expiry(self, config_mod):
        token, ttl = config_mod._parse_mint_output(
            '{"access_token": "abc123", "expires_in": 3600}'
        )
        assert token == "abc123"
        assert ttl == 3600.0

    def test_bare_token_takes_first_line_only(self, config_mod):
        token, ttl = config_mod._parse_mint_output("tok\nnoise\nmore")
        assert token == "tok"
        assert ttl is None

    def test_empty_output_is_an_error(self, config_mod):
        with pytest.raises(config_mod.DatabricksAuthError):
            config_mod._parse_mint_output("   ")

    def test_json_without_access_token_is_an_error(self, config_mod):
        with pytest.raises(config_mod.DatabricksAuthError):
            config_mod._parse_mint_output('{"expires_in": 60}')

    def test_unparseable_json_is_an_error(self, config_mod):
        with pytest.raises(config_mod.DatabricksAuthError):
            config_mod._parse_mint_output("{not json")


class TestProfileDefaulting:
    """Picking a profile for the user must be safe, or not happen."""

    def test_env_override_wins(self, profiles_mod, monkeypatch):
        monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "from-env")
        assert profiles_mod.default_profile_name() == "from-env"

    def test_cli_default_flag_is_used(self, profiles_mod, monkeypatch):
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        monkeypatch.setattr(
            profiles_mod,
            "list_profiles",
            lambda **k: [
                {"name": "a", "host": HOST},
                {"name": "b", "host": HOST, "default": True},
            ],
        )
        assert profiles_mod.default_profile_name() == "b"

    def test_sole_workspace_profile_is_used(self, profiles_mod, monkeypatch):
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        monkeypatch.setattr(
            profiles_mod, "list_profiles", lambda **k: [{"name": "only", "host": HOST}]
        )
        assert profiles_mod.default_profile_name() == "only"

    def test_account_profiles_are_not_chosen(self, profiles_mod, monkeypatch):
        """Account-console hosts have no SQL warehouses."""
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        monkeypatch.setattr(
            profiles_mod,
            "list_profiles",
            lambda **k: [
                {"name": "acct", "host": "https://accounts.azuredatabricks.net"},
                {"name": "ws", "host": HOST},
            ],
        )
        assert profiles_mod.default_profile_name() == "ws"

    def test_ambiguous_profiles_return_none(self, profiles_mod, monkeypatch):
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        monkeypatch.setattr(
            profiles_mod,
            "list_profiles",
            lambda **k: [{"name": "a", "host": HOST}, {"name": "b", "host": HOST}],
        )
        assert profiles_mod.default_profile_name() is None

    def test_key_cmd_quotes_the_profile_name(self, profiles_mod):
        assert (
            profiles_mod.key_cmd_for_profile("has space")
            == 'databricks auth token -p "has space"'
        )

    def test_key_cmd_rejects_empty(self, profiles_mod):
        with pytest.raises(ValueError):
            profiles_mod.key_cmd_for_profile("  ")
