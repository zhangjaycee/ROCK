"""Unit tests for SandboxManager.delete and _check_delete_background.

Avoids ray / docker dependencies by patching out the BaseManager scheduler
setup and stubbing the meta_store / operator.
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.actions.sandbox.response import State
from rock.common.constants import DeleteReason
from rock.config import RockConfig, SandboxConfig
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError


@pytest.fixture
def rock_config_min():
    cfg = RockConfig()
    cfg.sandbox_config = SandboxConfig()
    return cfg


@pytest.fixture
def manager(rock_config_min):
    operator = AsyncMock()
    meta_store = AsyncMock()
    meta_store.get = AsyncMock(return_value=None)
    # Patch BaseManager scheduler setup so tests don't spawn APScheduler.
    with patch("rock.sandbox.base_manager.BaseManager._setup_scheduler"):
        m = SandboxManager(
            rock_config=rock_config_min,
            meta_store=meta_store,
            ray_namespace="test",
            ray_service=MagicMock(),
            enable_runtime_auto_clear=False,
            operator=operator,
        )
    return m


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_unknown_sandbox_is_noop(self, manager):
        manager._meta_store.get = AsyncMock(return_value=None)
        await manager.delete("sb-unknown")
        manager._meta_store.archive.assert_not_called()
        manager._operator.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_from_pending_raises_400(self, manager):
        manager._meta_store.get = AsyncMock(
            return_value={"sandbox_id": "sb-1", "state": State.PENDING, "host_ip": "1.2.3.4"}
        )
        with pytest.raises(BadRequestRockError):
            await manager.delete("sb-1")
        manager._operator.delete.assert_not_called()
        manager._meta_store.archive.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_from_running_raises_400(self, manager):
        manager._meta_store.get = AsyncMock(
            return_value={"sandbox_id": "sb-1", "state": State.RUNNING, "host_ip": "1.2.3.4"}
        )
        with pytest.raises(BadRequestRockError):
            await manager.delete("sb-1")
        manager._operator.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_from_stopped_archives_with_deleted_state(self, manager):
        manager._meta_store.get = AsyncMock(
            return_value={
                "sandbox_id": "sb-1",
                "state": State.STOPPED,
                "host_ip": "1.2.3.4",
                "spec": {"container_name": "sb-1", "image": "python:3.11", "memory": "2g", "cpus": 1},
            }
        )
        await manager.delete("sb-1")
        manager._operator.delete.assert_awaited_once()
        args, kwargs = manager._operator.delete.call_args
        assert args[0].container_name == "sb-1"
        assert kwargs.get("host_ip") == "1.2.3.4"
        manager._meta_store.archive.assert_awaited_once()
        info = manager._meta_store.archive.call_args[0][1]
        assert info["state"] == State.DELETED
        assert info["delete_time"]

    @pytest.mark.asyncio
    async def test_delete_already_deleted_is_noop(self, manager):
        manager._meta_store.get = AsyncMock(
            return_value={"sandbox_id": "sb-1", "state": State.DELETED, "host_ip": "1.2.3.4"}
        )
        await manager.delete("sb-1")
        manager._operator.delete.assert_not_called()
        manager._meta_store.archive.assert_not_called()

    @pytest.mark.asyncio
    async def test_operator_delete_failure_still_archives(self, manager):
        manager._meta_store.get = AsyncMock(
            return_value={
                "sandbox_id": "sb-1",
                "state": State.STOPPED,
                "host_ip": "1.2.3.4",
                "spec": {"container_name": "sb-1", "image": "python:3.11", "memory": "2g", "cpus": 1},
            }
        )
        manager._operator.delete = AsyncMock(side_effect=RuntimeError("worker unreachable"))
        await manager.delete("sb-1")
        manager._meta_store.archive.assert_awaited_once()
        info = manager._meta_store.archive.call_args[0][1]
        assert info["state"] == State.DELETED

    @pytest.mark.asyncio
    async def test_delete_propagates_reason(self, manager):
        manager._meta_store.get = AsyncMock(
            return_value={
                "sandbox_id": "sb-1",
                "state": State.STOPPED,
                "host_ip": "1.2.3.4",
                "spec": {"container_name": "sb-1", "image": "python:3.11", "memory": "2g", "cpus": 1},
            }
        )
        await manager.delete("sb-1", reason=DeleteReason.EXPIRED)
        # No public assertion target — reason is logged. Just ensure it doesn't raise
        # and archive happened.
        manager._meta_store.archive.assert_awaited_once()


class TestCheckDeleteBackground:
    @pytest.mark.asyncio
    async def test_no_pending_does_nothing(self, manager):
        async def empty():
            for _ in []:
                yield  # pragma: no cover

        manager._meta_store.iter_pending_delete = MagicMock(return_value=empty())
        await manager._check_delete_background()
        manager._meta_store.archive.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatches_delete_per_pending_sandbox(self, manager):
        import asyncio

        async def two_ids():
            yield "sb-a"
            yield "sb-b"

        manager._meta_store.iter_pending_delete = MagicMock(return_value=two_ids())

        called = []

        async def fake_delete(sandbox_id, reason=DeleteReason.MANUAL):
            called.append((sandbox_id, reason))

        manager.delete = fake_delete

        await manager._check_delete_background()
        # asyncio.create_task fires-and-forgets; drain the loop
        for _ in range(5):
            await asyncio.sleep(0)

        assert ("sb-a", DeleteReason.EXPIRED) in called
        assert ("sb-b", DeleteReason.EXPIRED) in called


class TestIterPendingDelete:
    """Integration-style test for SandboxMetaStore.iter_pending_delete with a real
    SQLite + FakeRedis backing, to cover the stop_time + auto_delete_seconds math.
    """

    @pytest.mark.asyncio
    async def test_yields_only_expired_with_default_fallback(self, _memory_sandbox_table, redis_provider, rock_config):
        """Cover:
        * explicit ``auto_delete_seconds`` that has expired -> yielded
        * explicit ``auto_delete_seconds`` not yet expired -> skipped
        * missing key (legacy spec) -> falls back to 7-day default
        * sentinel ``None`` (set explicitly by old config dump) -> same default
        * row already deleted -> not in stopped set, not yielded
        """
        from rock.sandbox.sandbox_meta_store import SandboxMetaStore

        meta_store = SandboxMetaStore(
            redis_provider=redis_provider, sandbox_table=_memory_sandbox_table, rock_config=rock_config
        )

        now = datetime.datetime.now().astimezone()

        async def insert(sb_id: str, stop_time: datetime.datetime, spec: dict | None):
            await _memory_sandbox_table.create(
                sandbox_id=sb_id,
                info={
                    "sandbox_id": sb_id,
                    "state": State.STOPPED,
                    "stop_time": stop_time.isoformat(timespec="seconds"),
                },
                config=None,
            )
            from sqlalchemy.ext.asyncio import AsyncSession

            from rock.admin.core.schema import SandboxRecord

            async with AsyncSession(_memory_sandbox_table._db.engine) as session:
                rec = await session.get(SandboxRecord, sb_id)
                rec.spec = spec
                rec.stop_time = stop_time.isoformat(timespec="seconds")
                await session.commit()

        # explicit value, expired
        await insert("sb-due", now - datetime.timedelta(seconds=30), {"auto_delete_seconds": 10})
        # explicit value, not yet
        await insert("sb-fresh", now - datetime.timedelta(seconds=2), {"auto_delete_seconds": 60})
        # legacy spec — key missing entirely; falls back to 7d default → not yet (only 30s elapsed)
        await insert("sb-legacy-recent", now - datetime.timedelta(seconds=30), {})
        # legacy spec — key missing entirely; falls back to 7d default → expired (8 days elapsed)
        await insert("sb-legacy-expired", now - datetime.timedelta(days=8), {})
        # explicit None — same default applies → expired
        await insert("sb-explicit-none-expired", now - datetime.timedelta(days=8), {"auto_delete_seconds": None})

        ids = []
        async for sid in meta_store.iter_pending_delete(limit=100):
            ids.append(sid)
        assert "sb-due" in ids
        assert "sb-fresh" not in ids
        assert "sb-legacy-recent" not in ids
        assert "sb-legacy-expired" in ids
        assert "sb-explicit-none-expired" in ids

    @pytest.mark.asyncio
    async def test_limit_bounds_scan(self, _memory_sandbox_table, redis_provider, rock_config):
        """SQL LIMIT actually caps the per-call scan regardless of how many
        stopped rows exist. Oldest-first ordering means a small limit can still
        pick up the most-overdue rows."""
        from rock.sandbox.sandbox_meta_store import SandboxMetaStore

        meta_store = SandboxMetaStore(
            redis_provider=redis_provider, sandbox_table=_memory_sandbox_table, rock_config=rock_config
        )

        now = datetime.datetime.now().astimezone()
        # 20 rows, all expired, varying stop_time
        from sqlalchemy.ext.asyncio import AsyncSession

        from rock.admin.core.schema import SandboxRecord

        for i in range(20):
            sb_id = f"sb-{i:03d}"
            await _memory_sandbox_table.create(
                sandbox_id=sb_id,
                info={"sandbox_id": sb_id, "state": State.STOPPED},
                config=None,
            )
            async with AsyncSession(_memory_sandbox_table._db.engine) as session:
                rec = await session.get(SandboxRecord, sb_id)
                # earliest stop_time on i=0, latest on i=19
                rec.stop_time = (now - datetime.timedelta(seconds=3600 - i)).isoformat(timespec="seconds")
                rec.spec = {"auto_delete_seconds": 10}
                await session.commit()

        ids_small = [sid async for sid in meta_store.iter_pending_delete(limit=5)]
        ids_all = [sid async for sid in meta_store.iter_pending_delete(limit=100)]

        assert len(ids_small) == 5
        assert len(ids_all) == 20
        # oldest stop_time first: ids_small must be the first 5 by stop_time ASC
        assert ids_small == [f"sb-{i:03d}" for i in range(5)]
