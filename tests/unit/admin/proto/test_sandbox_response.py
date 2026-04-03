"""
Unit tests for admin proto response models — limit_disk_rootfs and limit_disk_log fields.

Tests cover:
- SandboxStartResponse.limit_disk_rootfs / limit_disk_log fields
- SandboxStatusResponse.limit_disk_rootfs / limit_disk_log fields
- SandboxStatusResponse.from_sandbox_info() extraction of both fields
"""

from rock.admin.proto.response import SandboxStartResponse, SandboxStatusResponse


# ---- SandboxStartResponse tests ----


class TestSandboxStartResponseDiskLimit:
    def test_limit_disk_rootfs_default_is_none(self):
        response = SandboxStartResponse()
        assert response.limit_disk_rootfs is None

    def test_limit_disk_log_default_is_none(self):
        response = SandboxStartResponse()
        assert response.limit_disk_log is None

    def test_limit_disk_rootfs_set_value(self):
        response = SandboxStartResponse(limit_disk_rootfs="20g")
        assert response.limit_disk_rootfs == "20g"

    def test_limit_disk_log_set_value(self):
        response = SandboxStartResponse(limit_disk_log="5g")
        assert response.limit_disk_log == "5g"

    def test_all_fields_with_both_limits(self):
        response = SandboxStartResponse(
            sandbox_id="test-sandbox",
            host_ip="10.0.0.1",
            cpus=4.0,
            memory="16g",
            limit_disk_rootfs="50g",
            limit_disk_log="5g",
        )
        assert response.sandbox_id == "test-sandbox"
        assert response.limit_disk_rootfs == "50g"
        assert response.limit_disk_log == "5g"
        assert response.cpus == 4.0
        assert response.memory == "16g"


# ---- SandboxStatusResponse tests ----


class TestSandboxStatusResponseDiskLimit:
    def test_limit_disk_rootfs_default_is_none(self):
        response = SandboxStatusResponse()
        assert response.limit_disk_rootfs is None

    def test_limit_disk_log_default_is_none(self):
        response = SandboxStatusResponse()
        assert response.limit_disk_log is None

    def test_limit_disk_rootfs_set_value(self):
        response = SandboxStatusResponse(limit_disk_rootfs="20g")
        assert response.limit_disk_rootfs == "20g"

    def test_limit_disk_log_set_value(self):
        response = SandboxStatusResponse(limit_disk_log="5g")
        assert response.limit_disk_log == "5g"

    def test_from_sandbox_info_with_both_limits(self):
        """from_sandbox_info() should extract both limit fields from SandboxInfo dict."""
        sandbox_info = {
            "sandbox_id": "test-sandbox",
            "phases": {},
            "port_mapping": {},
            "host_ip": "10.0.0.1",
            "cpus": 2.0,
            "memory": "8g",
            "limit_disk_rootfs": "30g",
            "limit_disk_log": "5g",
        }
        response = SandboxStatusResponse.from_sandbox_info(sandbox_info)
        assert response.limit_disk_rootfs == "30g"
        assert response.limit_disk_log == "5g"
        assert response.cpus == 2.0
        assert response.memory == "8g"

    def test_from_sandbox_info_without_limits(self):
        """from_sandbox_info() should yield None for both when absent."""
        sandbox_info = {
            "sandbox_id": "test-sandbox",
            "phases": {},
            "port_mapping": {},
            "cpus": 2.0,
            "memory": "8g",
        }
        response = SandboxStatusResponse.from_sandbox_info(sandbox_info)
        assert response.limit_disk_rootfs is None
        assert response.limit_disk_log is None

    def test_from_sandbox_info_with_none_limits(self):
        """from_sandbox_info() should surface None when fields are explicitly None."""
        sandbox_info = {
            "sandbox_id": "test-sandbox",
            "phases": {},
            "port_mapping": {},
            "limit_disk_rootfs": None,
            "limit_disk_log": None,
        }
        response = SandboxStatusResponse.from_sandbox_info(sandbox_info)
        assert response.limit_disk_rootfs is None
        assert response.limit_disk_log is None

    def test_from_sandbox_info_partial_limits(self):
        """from_sandbox_info() handles one field set, one absent."""
        sandbox_info = {
            "sandbox_id": "test-sandbox",
            "phases": {},
            "port_mapping": {},
            "limit_disk_rootfs": "50g",
        }
        response = SandboxStatusResponse.from_sandbox_info(sandbox_info)
        assert response.limit_disk_rootfs == "50g"
        assert response.limit_disk_log is None


# ---- actions/sandbox/response.SandboxStatusResponse tests ----


class TestActionsSandboxStatusResponseDiskLimit:
    def test_actions_status_response_both_limits(self):
        """rock.actions.sandbox.response.SandboxStatusResponse should have both limit fields."""
        from rock.actions.sandbox.response import SandboxStatusResponse as ActionStatusResponse

        response = ActionStatusResponse(limit_disk_rootfs="20g", limit_disk_log="5g")
        assert response.limit_disk_rootfs == "20g"
        assert response.limit_disk_log == "5g"

    def test_actions_status_response_defaults_none(self):
        from rock.actions.sandbox.response import SandboxStatusResponse as ActionStatusResponse

        response = ActionStatusResponse()
        assert response.limit_disk_rootfs is None
        assert response.limit_disk_log is None
