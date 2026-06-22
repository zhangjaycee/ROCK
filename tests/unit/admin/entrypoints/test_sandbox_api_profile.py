"""Tests for _apply_runtime_env_profile in sandbox_api.py."""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.admin.entrypoints import sandbox_api
from rock.deployments.config import DockerDeploymentConfig


def _make_config(image: str = "python:3.11", **overrides) -> DockerDeploymentConfig:
    return DockerDeploymentConfig(image=image, **overrides)


def _setup_sandbox_manager(
    *,
    yaml_profiles: dict | None = None,
    nacos_config: dict | None = None,
):
    mock_manager = MagicMock()
    mock_manager.rock_config.runtime.runtime_env_profiles = yaml_profiles or {}

    if nacos_config is not None:
        nacos = MagicMock()
        nacos.get_config = AsyncMock(return_value=nacos_config)
        mock_manager.rock_config.nacos_provider = nacos
    else:
        mock_manager.rock_config.nacos_provider = None

    sandbox_api.sandbox_manager = mock_manager
    return mock_manager


_MYAPP_PROFILE = {
    "images": ["myapp:*", "myapp-*"],
    "volume_mounts": [],
    "rocklet_start_cmd": "rocklet --port {proxy_port}",
    "startup_timeout": 300,
}


class TestApplyRuntimeEnvProfile:
    # --- matching ---

    @pytest.mark.asyncio
    async def test_no_profiles_no_op(self):
        _setup_sandbox_manager()
        config = _make_config(image="python:3.11")
        await sandbox_api._apply_runtime_env_profile(config)
        assert config.runtime_env_profile is None

    @pytest.mark.asyncio
    async def test_image_matches_yaml_profile(self):
        _setup_sandbox_manager(yaml_profiles={"myapp": _MYAPP_PROFILE})
        config = _make_config(image="myapp:v1")
        await sandbox_api._apply_runtime_env_profile(config)
        assert config.runtime_env_profile is not None
        assert config.runtime_env_profile["name"] == "myapp"

    @pytest.mark.asyncio
    async def test_image_no_match_profile_stays_none(self):
        _setup_sandbox_manager(yaml_profiles={"myapp": _MYAPP_PROFILE})
        config = _make_config(image="python:3.11")
        await sandbox_api._apply_runtime_env_profile(config)
        assert config.runtime_env_profile is None

    # --- startup_timeout from profile ---

    @pytest.mark.asyncio
    async def test_profile_timeout_applied_when_sdk_did_not_set(self):
        """Profile startup_timeout (300) fills in when config.startup_timeout is None."""
        _setup_sandbox_manager(yaml_profiles={"myapp": _MYAPP_PROFILE})
        config = _make_config(image="myapp:v1")  # startup_timeout=None
        await sandbox_api._apply_runtime_env_profile(config)
        assert config.startup_timeout == 300

    @pytest.mark.asyncio
    async def test_profile_timeout_does_not_override_sdk_value(self):
        """SDK-supplied startup_timeout wins over profile value."""
        _setup_sandbox_manager(yaml_profiles={"myapp": _MYAPP_PROFILE})
        config = _make_config(image="myapp:v1", startup_timeout=600)
        await sandbox_api._apply_runtime_env_profile(config)
        assert config.startup_timeout == 600  # SDK wins

    # --- YAML + Nacos merge ---

    @pytest.mark.asyncio
    async def test_nacos_adds_new_profile(self):
        """A profile present only in Nacos is reachable after merge."""
        _setup_sandbox_manager(
            yaml_profiles={"myapp": _MYAPP_PROFILE},
            nacos_config={
                "runtime_env_profiles": {
                    "gpu_env": {"images": ["gpu_env:*"], "rocklet_start_cmd": "rocklet --port {proxy_port}"},
                }
            },
        )
        config = _make_config(image="gpu_env:latest")
        await sandbox_api._apply_runtime_env_profile(config)
        assert config.runtime_env_profile is not None
        assert config.runtime_env_profile["name"] == "gpu_env"

    @pytest.mark.asyncio
    async def test_nacos_overrides_yaml_same_name_whole_profile(self):
        """Nacos profile with the same name replaces the YAML profile wholesale."""
        nacos_myapp = {"images": ["nacos-myapp:*"], "rocklet_start_cmd": "rocklet --port {proxy_port}"}
        _setup_sandbox_manager(
            yaml_profiles={"myapp": _MYAPP_PROFILE},
            nacos_config={"runtime_env_profiles": {"myapp": nacos_myapp}},
        )
        # YAML myapp matched "myapp:*"; after Nacos override it only matches "nacos-myapp:*"
        config = _make_config(image="myapp:v1")
        await sandbox_api._apply_runtime_env_profile(config)
        assert config.runtime_env_profile is None

        config2 = _make_config(image="nacos-myapp:latest")
        await sandbox_api._apply_runtime_env_profile(config2)
        assert config2.runtime_env_profile is not None
        assert config2.runtime_env_profile["name"] == "myapp"

    @pytest.mark.asyncio
    async def test_yaml_profile_still_reachable_after_nacos_adds_different_name(self):
        """Nacos adding a new profile does not remove the YAML profile."""
        _setup_sandbox_manager(
            yaml_profiles={"myapp": _MYAPP_PROFILE},
            nacos_config={"runtime_env_profiles": {"gpu_env": {"images": ["gpu_env:*"]}}},
        )
        config = _make_config(image="myapp:v1")
        await sandbox_api._apply_runtime_env_profile(config)
        assert config.runtime_env_profile is not None
        assert config.runtime_env_profile["name"] == "myapp"

    # --- side effects ---

    @pytest.mark.asyncio
    async def test_node_labels_passed_through_in_profile(self):
        profile = {**_MYAPP_PROFILE, "node_labels": ["kvm"]}
        _setup_sandbox_manager(yaml_profiles={"myapp": profile})
        config = _make_config(image="myapp:v1")
        await sandbox_api._apply_runtime_env_profile(config)
        assert config.runtime_env_profile["node_labels"] == ["kvm"]
        assert config.actor_resource is None

    @pytest.mark.asyncio
    async def test_host_env_passthrough_written_to_extended_params(self, monkeypatch):
        profile = {**_MYAPP_PROFILE, "host_env_passthrough": ["MY_SECRET"]}
        _setup_sandbox_manager(yaml_profiles={"myapp": profile})
        monkeypatch.setenv("MY_SECRET", "abc123")
        config = _make_config(image="myapp:v1")
        await sandbox_api._apply_runtime_env_profile(config)
        assert config.extended_params.get("MY_SECRET") == "abc123"

    @pytest.mark.asyncio
    async def test_host_env_passthrough_absent_var_not_added(self, monkeypatch):
        profile = {**_MYAPP_PROFILE, "host_env_passthrough": ["MISSING_VAR"]}
        _setup_sandbox_manager(yaml_profiles={"myapp": profile})
        monkeypatch.delenv("MISSING_VAR", raising=False)
        config = _make_config(image="myapp:v1")
        await sandbox_api._apply_runtime_env_profile(config)
        assert "MISSING_VAR" not in config.extended_params
