"""Integration tests for disk limit functionality."""

import pytest

from rock.actions import Command
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.utils.docker import DockerUtil
from tests.integration.conftest import SKIP_IF_NO_DOCKER, SKIP_IF_NO_STORAGE_OPT


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@SKIP_IF_NO_STORAGE_OPT
@pytest.mark.asyncio
async def test_disk_limit_enforcement(admin_remote_server):
    """Test that disk limit is enforced when storage-opt is supported.

    This test is only run when storage-opt is supported (overlay2 + xfs + prjquota).

    Steps:
    1. Create sandbox with limit_disk="1g"
    2. Check sandbox status to verify limit_disk is set
    3. Try to create a 2GB file (should fail due to disk limit)
    4. Create a 100MB file (should succeed)
    """
    config = SandboxConfig(
        image="ubuntu:22.04",
        memory="2g",
        cpus=1.0,
        limit_disk="1g",  # Set 1GB disk limit
        base_url=f"{admin_remote_server.endpoint}:{admin_remote_server.port}",
        startup_timeout=60,
    )

    sandbox = Sandbox(config)
    await sandbox.start()

    try:
        # Check sandbox status
        status = await sandbox.get_status()
        print(f"Sandbox status: limit_disk={status.limit_disk}")

        # Verify limit_disk is set (this test only runs when storage-opt is supported)
        assert status.limit_disk == "1g", f"Expected limit_disk='1g', got {status.limit_disk}"
        print("✅ Disk limit is set to 1g")

        # Try to create a 2GB file (should fail due to 1GB limit)
        # Use fallocate to quickly allocate space without writing data
        result = await sandbox.execute(
            Command(
                command=[
                    "/bin/bash",
                    "-c",
                    "fallocate -l 2G /tmp/large_file.bin 2>&1 || echo 'EXPECTED_ERROR'",
                ]
            )
        )

        output = result.stdout + result.stderr
        print(f"fallocate output: {output}")
        print(f"fallocate exit_code: {result.exit_code}")

        # Expected error messages when disk limit is hit:
        # - "No space left on device"
        # - "fallocate failed"
        # - Or the command should fail with non-zero exit code
        error_occurred = (
            result.exit_code != 0
            or "No space left on device" in output
            or "fallocate failed" in output
            or "EXPECTED_ERROR" in output
        )

        assert error_occurred, (
            f"Expected disk space error when creating 2GB file with 1GB limit, "
            f"but got exit_code={result.exit_code}, output={output}"
        )

        print("✅ Disk limit enforcement verified: 2GB file creation failed as expected")

        # Verify we can create a small file (should succeed)
        small_file_result = await sandbox.execute(
            Command(command=["/bin/bash", "-c", "fallocate -l 100M /tmp/small_file.bin && echo 'SUCCESS'"])
        )

        small_output = small_file_result.stdout + small_file_result.stderr
        assert small_file_result.exit_code == 0, (
            f"Expected small file (100MB) creation to succeed, "
            f"but got exit_code={small_file_result.exit_code}"
        )
        assert "SUCCESS" in small_output
        print("✅ Small file (100MB) creation succeeded")

    finally:
        # Cleanup
        await sandbox.stop()


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_disk_limit_default_value(admin_remote_server):
    """Test that limit_disk defaults to '20g' when not specified."""
    config = SandboxConfig(
        image="ubuntu:22.04",
        memory="2g",
        cpus=1.0,
        # limit_disk not specified, should default to "20g"
        base_url=f"{admin_remote_server.endpoint}:{admin_remote_server.port}",
        startup_timeout=60,
    )

    sandbox = Sandbox(config)
    await sandbox.start()

    try:
        status = await sandbox.get_status()
        print(f"Sandbox status: limit_disk={status.limit_disk}")

        # Check if storage-opt is supported
        storage_opt_supported = DockerUtil.detect_storage_opt_support()

        if storage_opt_supported:
            # If supported, default should be "20g"
            assert status.limit_disk == "20g", (
                f"Expected default limit_disk='20g', got {status.limit_disk}"
            )
            print("✅ Default limit_disk is '20g'")
        else:
            # If not supported, should be None
            assert status.limit_disk is None, (
                f"Expected limit_disk=None when storage-opt not supported, "
                f"got {status.limit_disk}"
            )
            print("✅ Storage-opt not supported: limit_disk is None")

    finally:
        await sandbox.stop()


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_disk_limit_custom_value(admin_remote_server):
    """Test that custom limit_disk value is respected."""
    config = SandboxConfig(
        image="ubuntu:22.04",
        memory="2g",
        cpus=1.0,
        limit_disk="5g",  # Custom 5GB limit
        base_url=f"{admin_remote_server.endpoint}:{admin_remote_server.port}",
        startup_timeout=60,
    )

    sandbox = Sandbox(config)
    await sandbox.start()

    try:
        status = await sandbox.get_status()
        print(f"Sandbox status: limit_disk={status.limit_disk}")

        storage_opt_supported = DockerUtil.detect_storage_opt_support()

        if storage_opt_supported:
            assert status.limit_disk == "5g", f"Expected limit_disk='5g', got {status.limit_disk}"
            print("✅ Custom limit_disk='5g' is set")
        else:
            assert status.limit_disk is None
            print("✅ Storage-opt not supported: limit_disk is None")

    finally:
        await sandbox.stop()
