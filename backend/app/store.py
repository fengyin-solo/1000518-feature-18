"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

矿产评价模块在此基础上额外提供两样东西：

- ``transaction``：一段业务写入要么整体提交、要么整体回滚，归档/撤销/重开都走它；
- ``lock_for``：按业务对象加锁，并发流转同一线索时只允许一个成功。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 矿产评价主表之外的配套表：不参与运营概览的业务模块计数。
MINERAL_SIDE_TABLES = (
    "mineral_ledger",
    "mineral_deviation",
    "mineral_todo",
    "mineral_archive",
)
# 矿产评价的配套表：主表 + 台账 + 偏离点图清单 + 验证待办 + 归档版本
MINERAL_TABLES = (
    "mineral",
    *MINERAL_SIDE_TABLES,
)


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for name in MINERAL_TABLES:
            self._tables.setdefault(name, [])
        # 矿产评价写操作按线索编号串行化，保证并发流转只有一个能落库。
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def business_module_names(self) -> list[str]:
        """业务模块名（矿产评价配套清单不算独立模块）。"""
        return [name for name in self.module_names() if name not in MINERAL_SIDE_TABLES]

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def lock_for(self, key: str) -> threading.RLock:
        """取得某个业务对象（矿化线索编号）对应的可重入锁。"""
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock

    @contextmanager
    def transaction(self, tables: tuple[str, ...] = MINERAL_TABLES) -> Iterator[None]:
        """提交点：进入时给相关表拍快照，业务体抛错则整体回滚。

        用法::

            with store.transaction():
                ...  # 对 mineral 及其配套表的所有写入

        业务体内抛出 ``TransactionError``（或任意异常）时，表数据恢复到进入前的
        状态，调用方能拿到原始错误信息并对外说明。
        """
        snapshot = {name: copy.deepcopy(self.rows(name)) for name in tables}
        try:
            yield
        except Exception:
            for name in tables:
                self._tables[name] = snapshot[name]
            raise

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.business_module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
