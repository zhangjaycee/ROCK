"""Integration test: ImageBuilder builds image in builder sandbox and push to registry; verify in sandbox.
"""

import logging
import os
import tempfile
from pathlib import Path

import pytest

from rock.actions import CreateBashSessionRequest
from rock.sdk.builder.image_builder import ImageBuilder
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from tests.integration.conftest import SKIP_IF_NO_DOCKER

logger = logging.getLogger(__name__)

TEST_FILE_CONTENT = "hello from test file"
# Base image for Dockerfile: set ROCK_TEST_BASE_IMAGE if Docker Hub is unreachable
ROCK_TEST_BASE_IMAGE = os.environ.get("ROCK_TEST_BASE_IMAGE", "python:3.11")
BUILT_IMAGE_NAME = "rock-builder-test:local"


@SKIP_IF_NO_DOCKER
@pytest.mark.need_admin_and_network
@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_env_dir_image_build_and_push(admin_remote_server, local_registry):
    """ImageBuilder.build() builds image inside builder sandbox and pushes to local registry."""
    if os.environ.get("ROCK_SKIP_BUILDER_DOCKERD_TEST"):
        pytest.skip("ROCK_SKIP_BUILDER_DOCKERD_TEST is set (builder image may not become alive in this env)")
    registry_url, registry_username, registry_password = local_registry
    base_url = f"{admin_remote_server.endpoint}:{admin_remote_server.port}"
    image_tag = f"{registry_url}/{BUILT_IMAGE_NAME}"

    with tempfile.TemporaryDirectory(prefix="rock_env_dir_local_") as tmp:
        env_dir = Path(tmp)
        (env_dir / "test.txt").write_text(TEST_FILE_CONTENT)
        (env_dir / "Dockerfile").write_text(
            f"FROM {ROCK_TEST_BASE_IMAGE}\nCOPY test.txt /test.txt\nRUN echo ok\n"
        )

        builder = ImageBuilder()
        result = await builder.build(
            instance_record={"env_dir": str(env_dir), "image_tag": image_tag},
            base_url=base_url,
            registry_username=registry_username,
            registry_password=registry_password,
        )
        assert result == image_tag
        logger.info("ImageBuilder local build succeeded: %s", result)


@SKIP_IF_NO_DOCKER
@pytest.mark.need_admin_and_network
@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_verify_env_dir_image_sandbox(admin_remote_server, local_registry):
    """Verify sandbox started from the image built in test_image_builder contains /test.txt.

    Assumes test_image_builder has been run first (same session) so the image exists in
    the shared local_registry.
    """
    if os.environ.get("ROCK_SKIP_BUILDER_DOCKERD_TEST"):
        pytest.skip("ROCK_SKIP_BUILDER_DOCKERD_TEST is set")
    registry_url, username, password = local_registry
    image_tag = f"{registry_url}/{BUILT_IMAGE_NAME}"
    base_url = f"{admin_remote_server.endpoint}:{admin_remote_server.port}"

    config = SandboxConfig(
        base_url=base_url,
        image=image_tag,
        memory="4g",
        cpus=2.0,
        startup_timeout=300,
        registry_username=username,
        registry_password=password,
    )
    sandbox = Sandbox(config)
    try:
        await sandbox.start()
        assert sandbox.sandbox_id
        await sandbox.create_session(CreateBashSessionRequest(session="default"))
        result = await sandbox.arun(cmd="cat /test.txt", session="default")
        assert result.exit_code == 0, result.failure_reason or result.output
        assert TEST_FILE_CONTENT in (result.output or "").strip()
        logger.info("test_verify_image: sandbox has /test.txt with expected content")
    finally:
        try:
            await sandbox.stop()
        except Exception as e:
            logger.warning("Failed to stop sandbox: %s", e)
