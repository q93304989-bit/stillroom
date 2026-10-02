"""只读路径的快照边界：并发写入不得让自检误报数据损坏。

背景：`verify_consistency()` 要读两样东西 —— 缓存的 `seq`/`state` 和事件流。
如果这两次读落在**不同时点**，正常的并发写入就会被判成「缓存与真相分叉」，
把一个健康系统报成数据损坏。修法是让整段自检跑在同一个读快照里。

这里的两个用例分别覆盖两条路径：
- `test_verify_consistency_tolerates_a_commit_between_reads`：另一连接两次读之间提交
  （等价于另一个进程在跑），确定性复现，不依赖调度。
- `test_verify_consistency_is_stable_under_concurrent_appends`：真多线程压力，只断言
  「不误报」，用来兜住未来把 `_read` 拆掉的重构。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from runtime import ExecutionRepository
from runtime.errors import RepositoryError
from validator.errors import ErrorCode


def _bind(repo: ExecutionRepository, request_id: str) -> str:
    return repo.bind_request(
        request_id=request_id,
        workflow_id="article_generation",
        workflow_version=1,
        input_snapshot={"prompt": "画一只猫"},
    ).execution_id


def test_verify_consistency_tolerates_a_commit_between_reads(tmp_path: Path) -> None:
    """另一个连接在自检中途提交一个事件，自检必须仍看到**一致的那一份**数据。

    把 `get()` 换成「取完快照后立刻让另一个连接写一笔」——这正是并发下
    「先读 seq、再读事件」会踩到的窗口。若整段自检共用同一个读快照，
    第二次读仍会看到写入前的世界，于是 seq 与事件数天然自洽。
    """
    db = tmp_path / "protocol.db"
    reader = ExecutionRepository(db)
    writer = ExecutionRepository(db)
    try:
        execution_id = _bind(reader, "req_snapshot")

        real_get = reader.get
        injected = {"done": False}

        def get_then_let_the_other_connection_write(target: str):
            record = real_get(target)
            if not injected["done"]:
                injected["done"] = True
                # 模拟「另一个进程」在两次读之间提交：engine_started
                writer.append_event(target, "engine_started")
            return record

        reader.get = get_then_let_the_other_connection_write  # type: ignore[method-assign]

        # 修复前：record.seq=0 而事件数=1 → 误报 INVALID_TRANSITION
        record = reader.verify_consistency(execution_id)
        assert record.seq == 0
        assert injected["done"] is True

        # 写入方确实落地了，只是没被这次快照看见
        assert writer.get(execution_id).seq == 1
    finally:
        reader.close()
        writer.close()


def test_verify_consistency_is_stable_under_concurrent_appends(tmp_path: Path) -> None:
    """一边推进状态机、一边反复自检：只允许「看不到新事件」，不允许报损坏。"""
    db = tmp_path / "protocol.db"
    reader = ExecutionRepository(db)
    writer = ExecutionRepository(db)
    try:
        execution_id = _bind(reader, "req_stress")

        stop = threading.Event()
        failures: list[BaseException] = []
        lock = threading.Lock()
        checks = {"n": 0}

        def check_loop() -> None:
            while not stop.is_set():
                try:
                    reader.verify_consistency(execution_id)
                    reader.replay_state(execution_id)
                    with lock:
                        checks["n"] += 1
                except BaseException as exc:  # noqa: BLE001 - 原样带出去
                    with lock:
                        failures.append(exc)
                    return

        watcher = threading.Thread(target=check_loop)
        watcher.start()
        try:
            # ENGINE_STEP_ENDED 在 RUNNING 上是自环，可以堆任意多事件，
            # 事件窗口足够密，才压得出撕裂读。
            writer.append_event(execution_id, "engine_started")
            for _ in range(300):
                writer.append_event(execution_id, "engine_step_ended")
        finally:
            stop.set()
            watcher.join(timeout=15)

        assert failures == []
        assert checks["n"] > 0
        assert writer.verify_consistency(execution_id).seq == 301
    finally:
        reader.close()
        writer.close()


def test_two_reads_in_one_operation_share_a_snapshot(tmp_path: Path) -> None:
    """`get_by_request()` 的「查绑定 + 取执行」也必须在同一快照里。"""
    db = tmp_path / "protocol.db"
    repo = ExecutionRepository(db)
    try:
        execution_id = _bind(repo, "req_pair")
        assert repo.get_by_request("req_pair").execution_id == execution_id
        assert repo.get_by_request("nope") is None
    finally:
        repo.close()


def test_read_snapshot_does_not_swallow_corruption(tmp_path: Path) -> None:
    """快照边界不能把「真的坏了」一起兜住 —— 绕过 API 改缓存列仍必须炸。"""
    db = tmp_path / "protocol.db"
    repo = ExecutionRepository(db)
    try:
        execution_id = _bind(repo, "req_corrupt")
        repo.append_event(execution_id, "engine_started")
        repo.verify_consistency(execution_id)

        repo._conn.execute(
            "UPDATE executions SET state = ? WHERE execution_id = ?",
            ("COMPLETED", execution_id),
        )
        repo._conn.commit()

        with pytest.raises(RepositoryError) as exc:
            repo.verify_consistency(execution_id)
        assert exc.value.code == ErrorCode.INVALID_TRANSITION.value
    finally:
        repo.close()
