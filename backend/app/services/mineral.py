"""矿产评价业务规则：状态梯级板、修订版本、三表同步与归档冻结都收在这里。

状态梯级板（线索编号一旦确定就锁定，版本只能逐级向前）::

    待踏勘 ──安排踏勘──▶ 踏勘中 ──开始评价──▶ 评价中 ──提交结论──▶ 已评价

- 跳级提交会被拦下并说明原因；同一级重复提交也不允许（再次提交不得重复生效）。
- “已评价”是当前版本的终点：要改只能“重开”，重开生成一个新的修订版本，
  旧版本整体留档，新结论不能覆盖、回退旧结论。
- 提交结论时，评价结论同步写入：矿产评价台账、偏离点图清单、验证待办。
- “归档”把当前已评价版本连同台账/偏离点/待办快照冻结，之后该线索只能读历史，
  任何写动作（含再次同步新值）都被拦下，归档后读不到新值。
- 矿种、评价等级与最近一次现场复核冲突时，以复核为准；历史评价按归档版本留档。
- 归档、撤销、重开都在单个事务里提交，中途异常整体回滚。
- 并发放置同一把全局锁 + 版本号（rev_token）乐观校验，流转只有一个能成功；
  幂等键相同的重复提交直接返回首次结果，不会重复生效。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services import mineral_migration
from app.store import store

MODULE = "mineral"
ARCHIVE_MODULE = "mineral_archive"
HISTORY_MODULE = "mineral_history"
LEDGER_MODULE = "mineral_ledger"
DEVIATION_MODULE = "mineral_deviation"
TODO_MODULE = "mineral_todo"
REVIEW_MODULE = "mineral_review"

REQUIRED_FIELDS = ["线索编号", "勘探区", "矿种"]

STATUS_ORDER = ["待踏勘", "踏勘中", "评价中", "已评价"]
EVALUATED = STATUS_ORDER[-1]
FORWARD_ACTIONS = {"安排踏勘": "踏勘中", "开始评价": "评价中", "提交结论": "已评价"}
GRADES = ["一类", "二类", "三类"]

# 偏离点只收“评价不乐观/见矿期望低”的结论；等级越低优先级越高。
_DEVIATION_WORDS = ("无矿", "未见矿", "矿化弱", "品位低", "否定", "负异常")
_TODO_PRIORITY = {"一类": "高", "二类": "中", "三类": "低"}

# 动作别名：允许路由/前端用更直白的说法调用同一套梯级板。
ACTION_ALIASES = {
    **FORWARD_ACTIONS,
    "撤销": "撤销",
    "归档": "归档",
    "重开": "重开",
    "现场复核": "现场复核",
}


class MineralConflict(RuntimeError):
    """业务规则被违反：梯级跳级、终态改写、归档写入等。路由层转成 ok=False。"""


class MineralRevisionConflict(MineralConflict):
    """并发版本冲突：expectedToken 与库内不一致，本次流转未生效。"""


class MineralService:
    def __init__(self) -> None:
        # 幂等键 -> 首次结果。键随进程内存在；命中即复用，绝不二次生效。
        self._idempotency: dict[str, dict[str, Any]] = {}
        mineral_migration.migrate()

    # ------------------------------------------------------------------ 读取

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        include_archived: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [dict(row) for row in store.rows(MODULE)]
        if include_archived:
            rows.extend(dict(row) for row in store.rows(ARCHIVE_MODULE))
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("线索编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> tuple[dict[str, Any] | None, str | None]:
        """返回 (线索, 错误说明)。归档线索读出的是冻结快照，活动表读不到时给原因。"""
        active = store.find(MODULE, entry_id)
        if active is not None:
            return active, None
        archived = store.find(ARCHIVE_MODULE, entry_id)
        if archived is not None:
            return archived, None
        history = store.find(HISTORY_MODULE, entry_id)
        if history is not None:
            current = self._find_current(history["线索编号"])
            if current is not None:
                return None, (
                    f"线索 {history['线索编号']} 的修订版本 {history['修订版本']} 已被后续版本替代，"
                    f"请查看当前版本 {current['修订版本']}（id={current['id']}）"
                )
            return None, f"线索 {history['线索编号']} 的该版本已经归档，只能读归档版本"
        return None, f"矿化线索 {entry_id} 不存在或已归档"

    def list_ledger(self, *, status: str | None = None, only_current: bool = True) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows(LEDGER_MODULE)]
        if status:
            # 显式给了生效状态就按状态过滤（含已归档/已被修订），不再被现行口径截掉。
            rows = [row for row in rows if row.get("生效状态") == status]
        elif only_current:
            rows = [row for row in rows if row.get("生效状态") == "现行"]
        return rows

    def list_deviations(self, *, status: str | None = None, only_current: bool = True) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows(DEVIATION_MODULE)]
        if status:
            rows = [row for row in rows if row.get("生效状态") == status]
        elif only_current:
            rows = [row for row in rows if row.get("生效状态") == "现行"]
        return rows

    def list_todos(self, *, status: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows(TODO_MODULE)]
        if status:
            rows = [row for row in rows if row.get("待办状态") == status]
        return rows

    def list_reviews(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows(REVIEW_MODULE)]
        if entry_id is not None:
            rows = [row for row in rows if int(row.get("线索id", 0)) == entry_id]
        return rows

    def list_archive(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(ARCHIVE_MODULE)]

    def list_history(self, code: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows(HISTORY_MODULE)]
        if code:
            rows = [row for row in rows if row.get("线索编号") == code]
        return rows

    # ------------------------------------------------------------------ 登记

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        code = str(values["线索编号"]).strip()
        try:
            with store.transaction():
                if self._find_current(code) is not None or self._find_archived_code(code) is not None:
                    raise MineralConflict(f"线索编号 {code} 已存在，线索编号确定后不可重复登记")
                entry = {"id": store.next_id(MODULE, ARCHIVE_MODULE, HISTORY_MODULE)}
                entry.update({field: str(values[field]).strip() for field in REQUIRED_FIELDS})
                entry.update({
                    "矿化类型": str(values.get("矿化类型") or "").strip(),
                    "发现方式": str(values.get("发现方式") or "").strip(),
                    "踏勘日期": str(values.get("踏勘日期") or "").strip(),
                    "评价结论": "",
                    "评价等级": str(values.get("评价等级") or "").strip(),
                    "等级来源": "登记",
                    "status": STATUS_ORDER[0],
                    "线索状态": STATUS_ORDER[0],
                    "修订版本": 1,
                    "rev_token": 1,
                    "已归档": False,
                    "pending": True,
                    "abnormal": False,
                })
                store.rows(MODULE).append(entry)
                return dict(entry), []
        except MineralConflict as exc:
            return None, [str(exc)]

    # ------------------------------------------------------------------ 动作入口

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
        *,
        expected_token: int | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """统一动作入口。成功返回 (最新线索, 说明)；规则冲突返回 (None, 原因)。

        所有写动作都在单个事务内提交；业务异常触发整库回滚。相同幂等键只生效一次。
        幂等键的判定与登记也在同一把全局锁内，避免同键并发时两个请求都看不到缓存。
        """
        normalized = ACTION_ALIASES.get(action)
        with store.lock:
            if idempotency_key:
                cached = self._idempotency.get(idempotency_key)
                if cached is not None:
                    # 命中首次结果：无论并发还是重试，都不会再改任何数据。
                    return cached.get("entry"), str(cached.get("message"))

            try:
                with store.transaction():
                    if normalized is None:
                        raise MineralConflict(f"动作「{action}」不属于矿产评价可执行范围")
                    entry, message = self._dispatch(entry_id, normalized, values or {}, expected_token)
                    result_entry = dict(entry) if entry else None
            except MineralConflict as exc:
                # 规则冲突不写幂等缓存：被拦下的提交允许换条件后用同键重试。
                return None, str(exc)

            if idempotency_key:
                self._idempotency[idempotency_key] = {"entry": result_entry, "message": message}
            return result_entry, message

    def _dispatch(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any],
        expected_token: int | None,
    ) -> tuple[dict[str, Any], str]:
        if action in FORWARD_ACTIONS.values():
            return self._forward(entry_id, action, values, expected_token)
        if action == "撤销":
            return self._undo(entry_id, expected_token)
        if action == "归档":
            return self._archive(entry_id, expected_token)
        if action == "重开":
            return self._reopen(entry_id, values, expected_token)
        if action == "现场复核":
            return self._field_review(entry_id, values, expected_token)
        raise MineralConflict(f"动作「{action}」未实现")

    # ------------------------------------------------------------- 逐级流转

    def _forward(
        self,
        entry_id: int,
        target: str,
        values: dict[str, Any],
        expected_token: int | None,
    ) -> tuple[dict[str, Any], str]:
        entry = self._require_active(entry_id)
        self._check_token(entry, expected_token)
        current = entry["status"]
        if target == current:
            raise MineralConflict(f"线索已处于「{current}」，该动作已提交过，重复提交不会重复生效")
        current_index = STATUS_ORDER.index(current)
        target_index = STATUS_ORDER.index(target)
        if target_index < current_index:
            raise MineralConflict(
                f"状态梯级板只能向前：当前「{current}」不能再走到「{target}」，"
                "已评价线索如需修改请走「重开」"
            )
        if target_index != current_index + 1:
            raise MineralConflict(
                f"不允许从「{current}」直接跳到「{target}」：状态梯级板只能逐级提交，"
                f"请先走到「{STATUS_ORDER[current_index + 1]}」"
            )

        notes: list[str] = []
        if target == EVALUATED:
            conclusion = str(values.get("评价结论") or "").strip()
            grade = str(values.get("评价等级") or "").strip()
            if not conclusion:
                raise MineralConflict("提交结论必须填写「评价结论」，否则不能进入已评价")
            if grade and grade not in GRADES:
                raise MineralConflict(f"评价等级只能是 {'/'.join(GRADES)}，收到「{grade}」")

            # 矿种与等级冲突时，以最近一次现场复核为准。
            review = self._latest_review(entry_id)
            auth_grade = review["评价等级"] if review and review.get("评价等级") else (grade or entry.get("评价等级") or "")
            auth_mineral = review["矿种"] if review and review.get("矿种") else entry["矿种"]
            if grade and review and review.get("评价等级") and grade != review["评价等级"]:
                notes.append(f"提交等级「{grade}」与最近现场复核「{review['评价等级']}」冲突，以复核为准")
                grade = review["评价等级"]
            if not auth_grade:
                raise MineralConflict("提交结论必须确定「评价等级」，或已有现场复核给出等级")
            if entry["矿种"] != auth_mineral:
                notes.append(f"矿种与最近现场复核冲突，以复核「{auth_mineral}」为准")

            entry["status"] = target
            entry["线索状态"] = target
            entry["评价结论"] = conclusion
            entry["评价等级"] = auth_grade
            entry["等级来源"] = "现场复核" if (review and review.get("评价等级")) else entry.get("等级来源") or "提交"
            entry["矿种"] = auth_mineral
            entry["评价时间"] = self._now()
            entry["冲突说明"] = "；".join(notes)
            self._bump(entry)
            self._sync_evaluated(entry)
            message = f"线索 {entry['线索编号']} 已提交结论并同步台账/偏离点/验证待办"
            if notes:
                message += "；" + "；".join(notes)
            return entry, message

        # 安排踏勘 / 开始评价
        entry["status"] = target
        entry["线索状态"] = target
        if target == "踏勘中" and not entry.get("踏勘日期"):
            entry["踏勘日期"] = self._today()
        self._bump(entry)
        return entry, f"矿化线索 {entry['线索编号']} 已进入{target}"

    # ------------------------------------------------------------- 撤销

    def _undo(self, entry_id: int, expected_token: int | None) -> tuple[dict[str, Any], str]:
        entry = self._require_active(entry_id)
        self._check_token(entry, expected_token)
        current = entry["status"]
        if current == STATUS_ORDER[0]:
            raise MineralConflict("线索已在梯级板起点「待踏勘」，没有可撤销的上一步")
        if current == EVALUATED:
            raise MineralConflict(
                "「已评价」是梯级板终点，不能撤销回退旧结论；如需修订请走「重开」生成新版本"
            )
        prev = STATUS_ORDER[STATUS_ORDER.index(current) - 1]
        entry["status"] = prev
        entry["线索状态"] = prev
        self._bump(entry)
        return entry, f"矿化线索 {entry['线索编号']} 已撤销，回退到「{prev}」"

    # ------------------------------------------------------------- 重开（修订版本）

    def _reopen(
        self, entry_id: int, values: dict[str, Any], expected_token: int | None
    ) -> tuple[dict[str, Any], str]:
        entry = self._require_active(entry_id)
        self._check_token(entry, expected_token)
        if entry["status"] != EVALUATED:
            raise MineralConflict(f"只有「已评价」线索才能重开，当前为「{entry['status']}」，请继续逐级流转")

        reason = str(values.get("重开原因") or "").strip()
        if not reason:
            raise MineralConflict("重开已评价线索必须填写「重开原因」")

        code = entry["线索编号"]
        # 1) 旧版本整体留档：活动行搬历史，旧同步行标“已被修订”。
        archived_version = dict(entry)
        archived_version["归档时间"] = self._now()
        archived_version["重开原因"] = reason
        store.rows(HISTORY_MODULE).append(archived_version)
        store.rows(MODULE).remove(entry)
        self._supersede_sync(code, archived_version["修订版本"])

        # 2) 新修订版本从梯级板起点重新走；编号不变、版本号 +1，绝不回到旧结论。
        new_id = store.next_id(MODULE, ARCHIVE_MODULE, HISTORY_MODULE)
        new_revision = int(archived_version["修订版本"]) + 1
        reopened = {
            "id": new_id,
            "线索编号": code,
            "勘探区": archived_version.get("勘探区", ""),
            "矿种": archived_version.get("矿种", ""),
            "矿化类型": archived_version.get("矿化类型", ""),
            "发现方式": archived_version.get("发现方式", ""),
            "踏勘日期": archived_version.get("踏勘日期", ""),
            "评价结论": "",
            "评价等级": archived_version.get("评价等级", ""),
            "等级来源": archived_version.get("等级来源", ""),
            "冲突说明": "",
            "status": STATUS_ORDER[0],
            "线索状态": STATUS_ORDER[0],
            "修订版本": new_revision,
            "rev_token": 1,
            "已归档": False,
            "重开自版本": archived_version["修订版本"],
            "重开原因": reason,
            "pending": True,
            "abnormal": False,
        }
        store.rows(MODULE).append(reopened)
        return reopened, (
            f"线索 {code} 已重开为修订版本 v{new_revision}：旧版本 v{archived_version['修订版本']} "
            "结论已留档，新版本从待踏勘重新逐级流转，不能回到旧结论"
        )

    # ------------------------------------------------------------- 归档

    def _archive(self, entry_id: int, expected_token: int | None) -> tuple[dict[str, Any], str]:
        entry = self._require_active(entry_id)
        self._check_token(entry, expected_token)
        if entry["status"] != EVALUATED:
            raise MineralConflict(f"只有「已评价」线索才能归档，当前为「{entry['status']}」")

        # 冻结快照：当前线索 + 该版本三表同步行，整体进入归档表，活动表删除。
        snapshot = dict(entry)
        snapshot["已归档"] = True
        snapshot["归档时间"] = self._now()
        snapshot["台账"] = [
            dict(row) for row in store.rows(LEDGER_MODULE)
            if row["线索编号"] == entry["线索编号"] and row["修订版本"] == entry["修订版本"]
        ]
        snapshot["偏离点"] = [
            dict(row) for row in store.rows(DEVIATION_MODULE)
            if row["线索编号"] == entry["线索编号"] and row["修订版本"] == entry["修订版本"]
        ]
        snapshot["验证待办"] = [
            dict(row) for row in store.rows(TODO_MODULE)
            if row["线索编号"] == entry["线索编号"] and row["修订版本"] == entry["修订版本"]
        ]
        store.rows(ARCHIVE_MODULE).append(snapshot)
        store.rows(MODULE).remove(entry)

        for table, field in ((LEDGER_MODULE, "生效状态"), (DEVIATION_MODULE, "生效状态")):
            for row in store.rows(table):
                if row["线索编号"] == entry["线索编号"] and row["修订版本"] == entry["修订版本"]:
                    row[field] = "已归档"
        for row in store.rows(TODO_MODULE):
            if row["线索编号"] == entry["线索编号"] and row["修订版本"] == entry["修订版本"]:
                row["待办状态"] = "已归档"

        return snapshot, (
            f"线索 {entry['线索编号']} v{entry['修订版本']} 已归档：台账/偏离点/待办一并冻结，"
            "归档后只读，不再写入或同步新值"
        )

    # ------------------------------------------------------------- 现场复核

    def _field_review(
        self, entry_id: int, values: dict[str, Any], expected_token: int | None
    ) -> tuple[dict[str, Any], str]:
        entry = self._require_active(entry_id)
        self._check_token(entry, expected_token)
        mineral = str(values.get("矿种") or "").strip()
        grade = str(values.get("评价等级") or "").strip()
        if not mineral and not grade:
            raise MineralConflict("现场复核至少要给出「矿种」或「评价等级」中的一项")
        if grade and grade not in GRADES:
            raise MineralConflict(f"评价等级只能是 {'/'.join(GRADES)}，收到「{grade}」")

        review = {
            "id": store.next_id(REVIEW_MODULE),
            "线索id": entry["id"],
            "线索编号": entry["线索编号"],
            "修订版本": entry["修订版本"],
            "矿种": mineral or entry["矿种"],
            "评价等级": grade or entry.get("评价等级", ""),
            "复核人": str(values.get("复核人") or "").strip(),
            "复核时间": str(values.get("复核时间") or "").strip() or self._now(),
            "说明": str(values.get("说明") or "").strip(),
        }
        store.rows(REVIEW_MODULE).append(review)

        notes: list[str] = []
        if mineral and mineral != entry["矿种"]:
            notes.append(f"矿种「{entry['矿种']}」→「{mineral}」")
            entry["矿种"] = mineral
        if grade and grade != entry.get("评价等级"):
            notes.append(f"评价等级「{entry.get('评价等级') or '空'}」→「{grade}」")
            entry["评价等级"] = grade
        entry["等级来源"] = "现场复核"
        self._bump(entry)

        # 已评价版本的复核结论同样以复核为准，事务内刷新现行同步行（归档后则进不来这里）。
        if entry["status"] == EVALUATED:
            self._refresh_evaluated_sync(entry)

        message = f"线索 {entry['线索编号']} 已记录现场复核，矿种/等级冲突以最近复核为准"
        if notes:
            message += "（" + "；".join(notes) + "）"
        return entry, message

    # ------------------------------------------------------------- 三表同步

    def _sync_evaluated(self, entry: dict[str, Any]) -> None:
        """提交结论后，把评价结论同步到台账、偏离点图清单、验证待办。"""
        code = entry["线索编号"]
        version = entry["修订版本"]
        now = self._now()
        store.rows(LEDGER_MODULE).append({
            "id": store.next_id(LEDGER_MODULE),
            "线索编号": code,
            "修订版本": version,
            "勘探区": entry.get("勘探区", ""),
            "矿种": entry["矿种"],
            "评价等级": entry["评价等级"],
            "评价结论": entry["评价结论"],
            "评价时间": entry.get("评价时间", now),
            "生效状态": "现行",
            "同步时间": now,
        })

        is_deviation = self._is_deviation(entry)
        if is_deviation:
            store.rows(DEVIATION_MODULE).append({
                "id": store.next_id(DEVIATION_MODULE),
                "线索编号": code,
                "修订版本": version,
                "勘探区": entry.get("勘探区", ""),
                "矿种": entry["矿种"],
                "评价等级": entry["评价等级"],
                "偏离摘要": entry["评价结论"],
                "判定依据": "等级三类" if entry["评价等级"] == "三类" else "结论命中负向描述",
                "生效状态": "现行",
                "记录时间": now,
            })

        store.rows(TODO_MODULE).append({
            "id": store.next_id(TODO_MODULE),
            "线索编号": code,
            "修订版本": version,
            "待办事项": f"现场验证 {code}（{entry['矿种']}，{entry['评价等级']}）",
            "优先级": _TODO_PRIORITY.get(entry["评价等级"], "中"),
            "待办状态": "待验证",
            "来源结论": entry["评价结论"],
            "创建时间": now,
        })

    def _is_deviation(self, entry: dict[str, Any]) -> bool:
        """三类评价或结论命中负向描述（无矿/矿化弱/品位低等）算作偏离点。"""
        return entry.get("评价等级") == "三类" or any(
            word in str(entry.get("评价结论") or "") for word in _DEVIATION_WORDS
        )

    def _refresh_evaluated_sync(self, entry: dict[str, Any]) -> None:
        """已评价版本做完现场复核后，用复核后的权威值刷新现行三表（同一事务）。

        偏离点随复核结论对账：改判成偏离则补登，改判不再偏离则把现行偏离点置为失效，
        历史行留痕不删。
        """
        now = self._now()
        for row in store.rows(LEDGER_MODULE):
            if (
                row["线索编号"] == entry["线索编号"]
                and row["修订版本"] == entry["修订版本"]
                and row["生效状态"] == "现行"
            ):
                row["矿种"] = entry["矿种"]
                row["评价等级"] = entry["评价等级"]
                row["评价结论"] = entry["评价结论"]
                row["同步时间"] = now

        current_deviations = [
            row for row in store.rows(DEVIATION_MODULE)
            if row["线索编号"] == entry["线索编号"]
            and row["修订版本"] == entry["修订版本"]
            and row["生效状态"] == "现行"
        ]
        for row in current_deviations:
            row["矿种"] = entry["矿种"]
            row["评价等级"] = entry["评价等级"]
            row["偏离摘要"] = entry["评价结论"]

        if self._is_deviation(entry) and not current_deviations:
            store.rows(DEVIATION_MODULE).append({
                "id": store.next_id(DEVIATION_MODULE),
                "线索编号": entry["线索编号"],
                "修订版本": entry["修订版本"],
                "勘探区": entry.get("勘探区", ""),
                "矿种": entry["矿种"],
                "评价等级": entry["评价等级"],
                "偏离摘要": entry["评价结论"],
                "判定依据": "现场复核改判" + ("（等级三类）" if entry.get("评价等级") == "三类" else "（负向描述）"),
                "生效状态": "现行",
                "记录时间": now,
            })
        elif not self._is_deviation(entry) and current_deviations:
            for row in current_deviations:
                row["生效状态"] = "复核后撤销"
                row["失效时间"] = now

    def _supersede_sync(self, code: str, version: int) -> None:
        """旧版本被重开替代：台账、偏离点标“已被修订”，验证待办标“已被修订”，全部留痕。"""
        for table in (LEDGER_MODULE, DEVIATION_MODULE):
            for row in store.rows(table):
                if row["线索编号"] == code and row["修订版本"] == version:
                    row["生效状态"] = "已被修订"
                    row["失效时间"] = self._now()
        for row in store.rows(TODO_MODULE):
            if row["线索编号"] == code and row["修订版本"] == version:
                row["待办状态"] = "已被修订"
                row["失效时间"] = self._now()

    # ------------------------------------------------------------- 内部工具

    def _require_active(self, entry_id: int) -> dict[str, Any]:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            return entry
        if store.find(ARCHIVE_MODULE, entry_id) is not None:
            raise MineralConflict("该线索已归档，归档后只读，不允许再流转、复核或写入新值")
        old = store.find(HISTORY_MODULE, entry_id)
        if old is not None:
            raise MineralConflict(
                f"该版本（v{old['修订版本']}）已被重开替代，不允许回到旧结论，请对当前版本操作"
            )
        raise MineralConflict(f"矿化线索 {entry_id} 不存在或已归档")

    def _check_token(self, entry: dict[str, Any], expected_token: int | None) -> None:
        if expected_token is not None and int(expected_token) != int(entry.get("rev_token", 0)):
            raise MineralRevisionConflict(
                "线索已被其他人改动（版本号不一致），本次并发流转未生效，请刷新后重试"
            )

    def _bump(self, entry: dict[str, Any]) -> None:
        entry["rev_token"] = int(entry.get("rev_token", 0)) + 1
        entry["pending"] = entry["status"] != EVALUATED
        entry["abnormal"] = False

    def _find_current(self, code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if row.get("线索编号") == code:
                return row
        return None

    def _find_archived_code(self, code: str) -> dict[str, Any] | None:
        for row in store.rows(ARCHIVE_MODULE):
            if row.get("线索编号") == code:
                return row
        return None

    def _latest_review(self, entry_id: int) -> dict[str, Any] | None:
        reviews = [
            row for row in store.rows(REVIEW_MODULE)
            if int(row.get("线索id", 0)) == entry_id
        ]
        if not reviews:
            return None
        return max(reviews, key=lambda row: (str(row.get("复核时间") or ""), int(row.get("id", 0))))

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _today() -> str:
        return datetime.now().date().isoformat()
