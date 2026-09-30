"""老线索迁移：把没有“评价等级”的矿化线索按踏勘时间补齐。

新流程上线前登记的线索只有“待踏勘/踏勘中/评价中/已评价”这档状态，没有评价等级。
为了让历史数据也能进状态梯级板、能与现场复核做冲突比对，这里做一次性迁移：

- 只补 ``评价等级`` 缺失的线索，已有等级（含此前迁移写入的）一律不覆盖；
- 按“踏勘日期从早到晚”分档：较早的一批补 ``一类``，居中补 ``二类``，较晚补 ``三类``；
- 踏勘日期缺失或无法解析的，归入最晚一档（``三类``），不阻断迁移；
- 同步写入 ``等级来源=踏勘时间迁移``、``迁移版本=1``，便于和“现场复核”区分。
"""
from __future__ import annotations

from datetime import date

from app.store import store

MODULE = "mineral"
MIGRATION_NAME = "mineral_grade_backfill_v1"
MIGRATION_VERSION = 1

# 按踏勘时间排序后的分档等级，索引取 min(index // 每份数量, 2)。
GRADE_BUCKETS = ["一类", "二类", "三类"]


def _parse_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def assign_grades(rows: list[dict]) -> int:
    """纯函数：就地给缺等级的行补等级，返回补齐条数。不读写全局状态，方便单测。"""
    targets = [row for row in rows if not str(row.get("评价等级") or "").strip()]
    if not targets:
        return 0

    # 踏勘日期早的排前面；解析不出日期的放到最后，随后归入最晚一档。
    def sort_key(row: dict) -> tuple[bool, date, int]:
        parsed = _parse_date(row.get("踏勘日期"))
        return (parsed is None, parsed or date.max, int(row.get("id", 0)))

    ordered = sorted(targets, key=sort_key)
    bucket_size = max(1, (len(ordered) + len(GRADE_BUCKETS) - 1) // len(GRADE_BUCKETS))
    for index, row in enumerate(ordered):
        grade = GRADE_BUCKETS[min(index // bucket_size, len(GRADE_BUCKETS) - 1)]
        row["评价等级"] = grade
        row["等级来源"] = "踏勘时间迁移"
        row["迁移版本"] = MIGRATION_VERSION
    return len(ordered)


def migrate() -> dict[str, object]:
    """执行一次老线索补齐；重复调用安全，只真正生效一次。"""
    result: {"backfilled": 0}

    def _job() -> None:
        result["backfilled"] = assign_grades(store.rows(MODULE))

    result = {"backfilled": 0}
    ran = store.run_once(MIGRATION_NAME, _job)
    result["ran"] = ran
    return result
