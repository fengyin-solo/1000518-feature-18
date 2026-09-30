"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

在示例内存实现之上补了三件矿产评价“状态梯级板”需要的基础设施：

- ``lock``：一把可重入的全局锁，所有写操作串行化，保证并发流转只有一个能成功；
- ``transaction``：事务上下文，进入时给整库做快照，业务规则抛异常就整体回滚，
  归档、撤销、重开要么全部生效，要么当没发生；
- ``run_once``：一次性迁移钩子，老线索补齐评价等级的迁移只执行一次。
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 只统计种子里的业务模块；矿产评价新增的台账/复核等内部表不进运营看板。
        self._business_modules = set(SEED_ROWS)
        self._lock = threading.RLock()
        self._meta: dict[str, bool] = {}

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def business_names(self) -> list[str]:
        """看板口径：只包含种子数据里的 18 个业务模块。"""
        return sorted(name for name in self._tables if name in self._business_modules)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, *modules: str) -> int:
        """在若干张表之间取一个全局不重复的自增 id。

        线索重开会把旧版本挪进历史表、新版本另起一行，跨表统一发号才能保证
        “初始线索行 / 后继线索行”这些引用不会撞 id。
        """
        max_id = 0
        for module in modules:
            for row in self.rows(module):
                max_id = max(max_id, int(row.get("id", 0)))
        return max_id + 1

    def run_once(self, name: str, job: Callable[[], None]) -> bool:
        """具名一次性任务：返回 True 表示本次真的执行了，False 表示历史上已执行过。"""
        with self._lock:
            if self._meta.get(name):
                return False
            job()
            self._meta[name] = True
            return True

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """整库快照事务：正常退出提交，中途异常回滚到进入前的样子。

        内存库没有真正的 WAL，这里用深拷贝快照实现“全成或全不成”。锁是可重入的，
        服务层在事务里再取锁也不会自锁。
        """
        self._lock.acquire()
        snapshot = deepcopy(self._tables)
        try:
            yield
        except BaseException:
            self._tables = snapshot
            raise
        finally:
            self._lock.release()

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.business_names():
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
