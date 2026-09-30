"""矿产评价业务规则：状态梯级板、修订版本、归档与配套清单同步都收在这里。

状态梯级板：线索编号确定后，状态只能沿 ``待踏勘 → 踏勘中 → 评价中 → 已评价``
逐格向前走。安排踏勘 / 开始评价 / 提交结论分别负责一格，跳级提交会被拦下并说明
原因；已评价线索不允许回到旧结论，重开走的是一条全新的修订版本。

提交结论时评价结论同步到三处：矿产评价台账、偏离点图清单、验证待办。归档后
线索进入只读快照，任何动作都读不出、也写不进新值。

并发与幂等：流转按线索编号加锁 + 状态/版本双重校验，并发提交只允许一个成功；
同一 ``request_id`` 重复提交直接回放首次结果，不重复生效；归档、撤销、重开都
在一个事务里提交，中途失败整体回滚。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.store import MINERAL_TABLES, store

MODULE = "MINERAL"
REQUIRED_FIELDS = ["线索编号", "勘探区", "矿种"]
OPTIONAL_FIELDS = ["矿化类型", "发现方式", "踏勘日期", "踏勘人员"]

# 状态梯级板：只能按下标逐格前进，不允许跳级、不允许回退。
STATUS_ORDER = ["待踏勘", "踏勘中", "评价中", "已评价"]
# 每个动作负责把线索从当前格推进到下一格；键是动作名，值是允许的前置状态。
FORWARD_ACTIONS = {"安排踏勘": "待踏勘", "开始评价": "踏勘中", "提交结论": "评价中"}
CANCEL_ACTION = "撤销"
ARCHIVE_ACTION = "归档"
REOPEN_ACTION = "重开"
REVIEW_ACTION = "现场复核"
TERMINAL_STATUS = "已撤销"

# 矿种与评价等级的常规配套关系：提交结论时两者不配套视为冲突。
# 冲突以最近一次现场复核为准；没有复核依据时拦下提交，要求先做现场复核。
GRADE_BY_MINERAL = {
    "煤矿": ["一类", "二类"],
    "铁矿": ["二类", "三类"],
    "铜矿": ["二类", "三类"],
    "金矿": ["一类", "二类"],
    "铅锌矿": ["三类"],
}
GRADE_LEVEL = {"一类": 1, "二类": 2, "三类": 3}

# 老线索没有评价等级时，按踏勘时间迁移补齐的分档口径。
GRADE_WITHIN_ONE_YEAR = "一类"
GRADE_WITHIN_TWO_YEARS = "二类"
GRADE_OLDER = "三类"


class TransactionError(Exception):
    """业务规则不允许当前操作：事务回滚，并把原因原样带给调用方。"""


# request_id -> 首次提交的动作结果（ok/message/entry）。重复提交回放，不再执行。
_idempotency_cache: dict[str, dict[str, Any]] = {}
_migrated = False


def infer_grade_by_survey_date(survey_date: str | None, *, today: date | None = None) -> str:
    """老线索补档口径：踏勘一年内一类、两年内二类、更早三类；日期缺失按最保守的三类。"""
    if not survey_date:
        return GRADE_OLDER
    try:
        surveyed = date.fromisoformat(str(survey_date)[:10])
    except ValueError:
        return GRADE_OLDER
    today = today or date.today()
    years = (today - surveyed).days / 365.25
    if years <= 1:
        return GRADE_WITHIN_ONE_YEAR
    if years <= 2:
        return GRADE_WITHIN_TWO_YEARS
    return GRADE_OLDER


def _next_id(table: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1


def _latest_revision(code: str) -> dict[str, Any] | None:
    """按修订版本号找到线索编号下的最新一版（含已归档前的活动版本）。"""
    revisions = [
        row for row in store.rows("mineral")
        if row.get("线索编号") == code and not row.get("已归档")
    ]
    if not revisions:
        return None
    return max(revisions, key=lambda row: int(row.get("revision", 1)))


def _find_revision(code: str, revision: int) -> dict[str, Any] | None:
    for row in store.rows("mineral"):
        if row.get("线索编号") == code and int(row.get("revision", 1)) == revision:
            return row
    return None


def _archived_revision(code: str, revision: int) -> dict[str, Any] | None:
    for row in store.rows("mineral_archive"):
        if row.get("线索编号") == code and int(row.get("revision", 1)) == revision:
            return row
    return None


def _grade_conflict(grade: str, mineral: str) -> bool:
    expected = GRADE_BY_MINERAL.get(mineral)
    if expected is None:
        # 未登记配套口径的矿种不做冲突判定，避免把规则外的矿种误拦下来。
        return False
    return grade not in expected


class MineralService:
    def __init__(self) -> None:
        self._migrate_legacy_grades()

    # ------------------------------------------------------------------ 读

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        include_archived: bool = False,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(MODULE.lower()))
        if include_archived:
            # 归档版本单独存表，列表需要时合并进来，并带上归档只读标记。
            rows += [dict(row, 已归档=True) for row in store.rows("mineral_archive")]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("线索编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, code: str, revision: int | None = None) -> dict[str, Any] | None:
        """读取线索明细。

        归档后不得读出新值：指定版本已归档时返回归档快照（只读），快照里是什么
        就给什么；未指定版本时返回最新修订版。
        """
        if revision is not None:
            archived = _archived_revision(code, revision)
            if archived is not None:
                return dict(archived)
            live = _find_revision(code, revision)
            return dict(live) if live else None
        latest = _latest_revision(code)
        if latest is not None:
            return dict(latest)
        # 所有版本都已归档且没有新版本时，给出最近一次归档快照供查阅。
        archived_versions = [
            row for row in store.rows("mineral_archive") if row.get("线索编号") == code
        ]
        if archived_versions:
            newest = max(archived_versions, key=lambda row: int(row.get("revision", 1)))
            return dict(newest)
        return None

    def list_versions(self, code: str) -> list[dict[str, Any]]:
        """线索编号下的全部修订版本：活动版本 + 归档版本，按版本号排序留档。"""
        versions = [
            dict(row) for row in store.rows(MODULE.lower()) if row.get("线索编号") == code
        ]
        versions += [
            dict(row) for row in store.rows("mineral_archive") if row.get("线索编号") == code
        ]
        return sorted(versions, key=lambda row: int(row.get("revision", 1)))

    def list_ledger(self, code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows("mineral_ledger")
        return [dict(row) for row in rows if code is None or row.get("线索编号") == code]

    def list_deviations(self, code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows("mineral_deviation")
        return [dict(row) for row in rows if code is None or row.get("线索编号") == code]

    def list_todos(self, code: str | None = None, open_only: bool = False) -> list[dict[str, Any]]:
        rows = store.rows("mineral_todo")
        if code is not None:
            rows = [row for row in rows if row.get("线索编号") == code]
        if open_only:
            rows = [row for row in rows if row.get("状态") == "待验证"]
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 登记

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        code = str(values["线索编号"]).strip()
        with store.lock_for(code), store.transaction():
            if _latest_revision(code) is not None or self.list_versions(code):
                raise TransactionError(f"线索编号 {code} 已存在，不能重复登记；如需更新请重开修订版本")
            entry = {
                "id": _next_id(MODULE.lower()),
                "线索编号": code,
                "revision": 1,
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
                "已归档": False,
                "已撤销": False,
                "矿种": str(values.get("矿种") or "").strip(),
                "评价等级": "",
                "等级来源": "",
                "踏勘日期": "",
                "复核记录": [],
                "评价结论": "",
                "结论历史": [],
                "version": 1,
            }
            for field in REQUIRED_FIELDS[1:] + OPTIONAL_FIELDS:
                if values.get(field) is not None:
                    entry[field] = str(values.get(field)).strip()
            # 登记时已带踏勘日期的（多为老线索补录），直接按踏勘时间补一档；
            # 没有踏勘日期的留空，等安排踏勘时再补齐。
            if entry.get("踏勘日期"):
                entry["评价等级"] = infer_grade_by_survey_date(entry.get("踏勘日期"))
                entry["等级来源"] = "踏勘时间补档"
            store.rows(MODULE.lower()).append(entry)
        return dict(entry), []

    # ------------------------------------------------------------------ 流转

    def run_action(
        self,
        code: str,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        expected_version: int | None = None,
        request_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """执行状态梯级板动作。

        返回 ``(entry, message, conflict)``：``conflict=True`` 表示并发冲突，调用方
        可按 409 处理。其余业务拦截返回 ``(None, 原因, False)``。
        """
        action = str(action or "").strip()
        payload = payload or {}
        if request_id:
            cached = _idempotency_cache.get(f"{code}:{request_id}")
            if cached is not None:
                # 再次提交不重复生效：原样回放首次结果。
                return cached.get("entry"), cached["message"], False

        if action == REVIEW_ACTION:
            entry, message = self._record_review(code, payload)
        elif action in FORWARD_ACTIONS:
            entry, message, conflict = self._forward(code, action, payload, expected_version)
            if conflict:
                return None, message, True
        elif action == ARCHIVE_ACTION:
            entry, message = self._archive(code, payload)
        elif action == CANCEL_ACTION:
            entry, message = self._cancel(code, payload)
        elif action == REOPEN_ACTION:
            entry, message = self._reopen(code, payload)
        else:
            allowed = "安排踏勘、开始评价、提交结论、现场复核、归档、撤销、重开"
            return None, f"动作「{action}」不属于矿产评价可执行范围（状态梯级板只支持：{allowed}）", False

        if request_id and entry is not None:
            _idempotency_cache[f"{code}:{request_id}"] = {"entry": dict(entry), "message": message}
        return (dict(entry) if entry is not None else None), message, False

    def _live_revision_or_raise(self, code: str, payload: dict[str, Any]) -> dict[str, Any]:
        revision = payload.get("revision")
        with store.lock_for(code):
            if revision is not None:
                row = _find_revision(code, int(revision))
                if row is None and _archived_revision(code, int(revision)) is not None:
                    raise TransactionError(
                        f"线索 {code} 第 {revision} 版已归档，归档版本只读，不能再改；"
                        "请重开一条新的修订版本"
                    )
            else:
                row = _latest_revision(code)
            if row is None:
                if self.list_versions(code):
                    raise TransactionError(
                        f"线索 {code} 已归档，归档版本只读、不得读出或写入新值；"
                        "如需变更请先「重开」一条新的修订版本"
                    )
                raise TransactionError(f"矿化线索 {code} 不存在")
            return row

    def _forward(
        self,
        code: str,
        action: str,
        payload: dict[str, Any],
        expected_version: int | None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        required_status = FORWARD_ACTIONS[action]
        target_index = STATUS_ORDER.index(required_status) + 1
        target_status = STATUS_ORDER[target_index]
        with store.lock_for(code):
            try:
                with store.transaction():
                    entry = self._live_revision_or_raise(code, payload)
                    if entry.get("已撤销"):
                        raise TransactionError(f"线索 {code} 已撤销，流转已终止")
                    current = entry["status"]
                    if current == target_status:
                        # 并发/重复提交：另一个请求已经把这一格走完，本次不再重复生效。
                        return (
                            None,
                            f"线索 {code} 当前已是「{current}」，动作「{action}」已被另一次提交处理，"
                            "本次不重复生效",
                            True,
                        )
                    if current != required_status:
                        # 跳级/回退拦截：说明梯级板允许的来路。
                        cur_idx = STATUS_ORDER.index(current) if current in STATUS_ORDER else -1
                        if cur_idx >= 0 and cur_idx > target_index:
                            reason = f"状态梯级板只能向前：{current} 不能回退到 {target_status}"
                        else:
                            missing_steps = " → ".join(STATUS_ORDER[cur_idx:target_index + 1]) \
                                if cur_idx >= 0 else f"先进入「{required_status}」"
                            reason = (
                                f"跳级提交被拦：线索 {code} 当前为「{current}」，"
                                f"「{action}」只能在「{required_status}」执行，目标为「{target_status}」；"
                                f"需依次走完 {missing_steps}"
                            )
                        raise TransactionError(reason)

                    # 乐观并发：携带版本号时，版本对不上说明期间已被别人改过。
                    if expected_version is not None and int(entry.get("version", 1)) != int(expected_version):
                        return (
                            None,
                            f"线索 {code} 数据已被其他提交更新（当前版本 {entry.get('version')}），"
                            "请刷新后重试；并发流转只允许一个成功",
                            True,
                        )

                    if action == "安排踏勘":
                        surveyed = str(payload.get("踏勘日期") or entry.get("踏勘日期") or "").strip()
                        if not surveyed:
                            raise TransactionError("安排踏勘需填写踏勘日期，否则不能进入「踏勘中」")
                        entry["踏勘日期"] = surveyed
                        if payload.get("踏勘人员"):
                            entry["踏勘人员"] = str(payload["踏勘人员"]).strip()
                        entry["评价等级"] = infer_grade_by_survey_date(surveyed)
                        entry["等级来源"] = "踏勘时间补档"
                        self._upsert_todo(entry, "踏勘核实", "安排踏勘：踏勘核实待办")
                    elif action == "开始评价":
                        self._upsert_todo(entry, "资料汇总评价", "开始评价：资料汇总评价待办")
                    else:  # 提交结论
                        self._apply_conclusion(entry, payload)

                    entry["status"] = target_status
                    entry["pending"] = target_status != STATUS_ORDER[-1]
                    entry["version"] = int(entry.get("version", 1)) + 1
                    return entry, f"矿化线索 {code} 已{action}，状态推进到「{target_status}」", False
            except TransactionError as exc:
                return None, str(exc), False

    def _apply_conclusion(self, entry: dict[str, Any], payload: dict[str, Any]) -> None:
        conclusion = str(payload.get("评价结论") or "").strip()
        grade = str(payload.get("评价等级") or entry.get("评价等级") or "").strip()
        mineral = str(payload.get("矿种") or entry.get("矿种") or "").strip()
        if not conclusion:
            raise TransactionError("提交结论必须填写评价结论")
        if not grade:
            raise TransactionError("提交结论必须明确评价等级")

        # 修订版本不允许回到旧结论：新版结论必须与该线索编号下任何已归档版本不同。
        code = entry["线索编号"]
        old_conclusions = {
            str(row.get("评价结论") or "").strip()
            for row in store.rows("mineral_archive")
            if row.get("线索编号") == code
        }
        rev = int(entry.get("revision", 1))
        if rev > 1 and conclusion in old_conclusions:
            raise TransactionError(
                f"第 {rev} 次修订不允许沿用第 {rev - 1} 版已归档结论「{conclusion}」，"
                "重开属于新的修订版本，必须给出新的评价结论"
            )

        review_note = ""
        if _grade_conflict(grade, mineral):
            # 矿种与评价等级冲突：以最近一次现场复核为准。
            reviews = entry.get("复核记录") or []
            latest_review = reviews[-1] if reviews else None
            if not latest_review:
                raise TransactionError(
                    f"矿种「{mineral}」与评价等级「{grade}」不配套且无现场复核依据，"
                    "请先完成现场复核，冲突以最近一次现场复核为准"
                )
            review_mineral = str(latest_review.get("矿种") or "").strip()
            review_grade = str(latest_review.get("评价等级") or "").strip()
            if review_mineral:
                entry["矿种"] = review_mineral
                mineral = review_mineral
            if review_grade:
                grade = review_grade
            review_note = (
                f"矿种/等级冲突已按最近一次现场复核（{latest_review.get('复核日期') or '日期未记'}）"
                f"更正为：矿种={mineral}，评价等级={grade}"
            )
            if _grade_conflict(grade, mineral):
                raise TransactionError(
                    f"即便按最近一次现场复核，矿种「{mineral}」与评价等级「{grade}」仍不配套，"
                    "结论不能提交"
                )

        entry["矿种"] = mineral
        entry["评价等级"] = grade
        entry["等级来源"] = str(entry.get("等级来源") or "提交结论")
        entry["评价结论"] = conclusion
        entry.setdefault("结论历史", []).append(
            {"revision": rev, "评价等级": grade, "评价结论": conclusion, "说明": review_note}
        )

        # 评价结论同步到矿产评价台账、偏离点图清单与验证待办，三处与结论在同一事务提交。
        self._sync_ledger(entry, conclusion, review_note)
        if _grade_conflict(str(payload.get("评价等级") or grade), str(payload.get("矿种") or mineral)) \
                or review_note:
            self._sync_deviation(entry, conclusion, review_note or "矿种与评价等级冲突，待图面核对")
        self._upsert_todo(entry, "结论验证", f"评价结论待验证：{conclusion}", overwrite=True)

    def _sync_ledger(self, entry: dict[str, Any], conclusion: str, note: str) -> None:
        code = entry["线索编号"]
        rev = int(entry.get("revision", 1))
        ledger = store.rows("mineral_ledger")
        row = next(
            (row for row in ledger if row.get("线索编号") == code and int(row.get("revision", 1)) == rev),
            None,
        )
        snapshot = {
            "线索编号": code,
            "revision": rev,
            "勘探区": entry.get("勘探区", ""),
            "矿种": entry.get("矿种", ""),
            "评价等级": entry.get("评价等级", ""),
            "评价结论": conclusion,
            "踏勘日期": entry.get("踏勘日期", ""),
            "最近复核": (entry.get("复核记录") or [{}])[-1].get("复核日期", ""),
            "冲突说明": note,
            "已归档": False,
            "更新时间": date.today().isoformat(),
        }
        if row is None:
            snapshot["id"] = _next_id("mineral_ledger")
            ledger.append(snapshot)
        else:
            row.update(snapshot)
        # 台账里同编号只保留最新一版为活动账，旧版标记为历史，归档后冻结。
        for other in ledger:
            if other.get("线索编号") == code and other is not row:
                other["当前版本"] = False
        if row is not None:
            row["当前版本"] = True
        else:
            ledger[-1]["当前版本"] = True

    def _sync_deviation(self, entry: dict[str, Any], conclusion: str, reason: str) -> None:
        code = entry["线索编号"]
        rev = int(entry.get("revision", 1))
        table = store.rows("mineral_deviation")
        row = next(
            (row for row in table
             if row.get("线索编号") == code and int(row.get("revision", 1)) == rev
             and row.get("状态") == "待上图"),
            None,
        )
        snapshot = {
            "线索编号": code,
            "revision": rev,
            "勘探区": entry.get("勘探区", ""),
            "偏离原因": reason,
            "评价结论": conclusion,
            "评价等级": entry.get("评价等级", ""),
            "状态": "待上图",
            "登记日期": date.today().isoformat(),
        }
        if row is None:
            snapshot["id"] = _next_id("mineral_deviation")
            table.append(snapshot)
        else:
            row.update(snapshot)

    def _upsert_todo(
        self, entry: dict[str, Any], title: str, description: str, *, overwrite: bool = False
    ) -> None:
        code = entry["线索编号"]
        rev = int(entry.get("revision", 1))
        table = store.rows("mineral_todo")
        existing = [
            row for row in table
            if row.get("线索编号") == code and int(row.get("revision", 1)) == rev
            and row.get("状态") == "待验证"
        ]
        if existing and not overwrite:
            return
        target = existing[0] if existing else None
        snapshot = {
            "线索编号": code,
            "revision": rev,
            "事项": title,
            "说明": description,
            "状态": "待验证",
            "登记日期": date.today().isoformat(),
        }
        if target is None:
            snapshot["id"] = _next_id("mineral_todo")
            table.append(snapshot)
        else:
            target.update(snapshot)

    def _record_review(self, code: str, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        review_date = str(payload.get("复核日期") or date.today().isoformat()).strip()
        record = {
            "复核日期": review_date,
            "复核人员": str(payload.get("复核人员") or "").strip(),
            "矿种": str(payload.get("矿种") or "").strip(),
            "评价等级": str(payload.get("评价等级") or "").strip(),
            "复核意见": str(payload.get("复核意见") or "").strip(),
        }
        with store.lock_for(code):
            try:
                with store.transaction():
                    entry = self._live_revision_or_raise(code, payload)
                    if entry.get("已归档"):
                        raise TransactionError(f"线索 {code} 已归档，不能再做现场复核")
                    if entry.get("已撤销"):
                        raise TransactionError(f"线索 {code} 已撤销，不能再做现场复核")
                    entry.setdefault("复核记录", []).append(record)
                    # 现场复核确认的矿种/等级直接落到活动版本，作为冲突时的最近一次依据。
                    if record["矿种"]:
                        entry["矿种"] = record["矿种"]
                    if record["评价等级"]:
                        entry["评价等级"] = record["评价等级"]
                        entry["等级来源"] = "现场复核"
                    entry["version"] = int(entry.get("version", 1)) + 1
                    return entry, f"线索 {code} 已登记 {review_date} 现场复核"
            except TransactionError as exc:
                return None, str(exc)

    # ---------------------------------------------------------- 归档/撤销/重开

    def _archive(self, code: str, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        with store.lock_for(code):
            try:
                with store.transaction():
                    entry = self._live_revision_or_raise(code, payload)
                    if entry.get("已归档"):
                        raise TransactionError(f"线索 {code} 已归档，归档不得重复执行")
                    if entry.get("已撤销"):
                        raise TransactionError(f"线索 {code} 已撤销，撤销版本不允许归档")
                    if entry["status"] != STATUS_ORDER[-1]:
                        raise TransactionError(
                            f"只有「已评价」线索可以归档，{code} 当前为「{entry['status']}」"
                        )
                    rev = int(entry.get("revision", 1))
                    snapshot = dict(entry)
                    snapshot["已归档"] = True
                    snapshot["归档时间"] = date.today().isoformat()
                    archive = store.rows("mineral_archive")
                    if _archived_revision(code, rev) is not None:
                        raise TransactionError(f"线索 {code} 第 {rev} 版归档已存在，不得重复归档")
                    archive.append(snapshot)
                    store.rows(MODULE.lower()).remove(entry)

                    # 配套清单一并冻结：台账置归档、偏离点置已上图、待办置关闭。
                    for row in store.rows("mineral_ledger"):
                        if row.get("线索编号") == code and int(row.get("revision", 1)) == rev:
                            row["已归档"] = True
                            row["当前版本"] = False
                            row["归档时间"] = snapshot["归档时间"]
                    for row in store.rows("mineral_deviation"):
                        if row.get("线索编号") == code and int(row.get("revision", 1)) == rev:
                            row["状态"] = "已上图"
                    for row in store.rows("mineral_todo"):
                        if row.get("线索编号") == code and int(row.get("revision", 1)) == rev:
                            row["状态"] = "已关闭"
                            row["关闭原因"] = "归档冻结"
                    return snapshot, f"线索 {code} 第 {rev} 版已归档，归档版本只读，不再接受修改"
            except TransactionError as exc:
                return None, str(exc)

    def _cancel(self, code: str, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        with store.lock_for(code):
            try:
                with store.transaction():
                    entry = self._live_revision_or_raise(code, payload)
                    if entry.get("已归档"):
                        raise TransactionError(f"线索 {code} 已归档，归档版本不能撤销；如需变更请重开修订版本")
                    if entry.get("已撤销"):
                        raise TransactionError(f"线索 {code} 已撤销，撤销不得重复执行")
                    entry["已撤销"] = True
                    entry["status"] = TERMINAL_STATUS
                    entry["pending"] = False
                    entry["abnormal"] = True
                    entry["version"] = int(entry.get("version", 1)) + 1
                    for row in store.rows("mineral_todo"):
                        if (row.get("线索编号") == code
                                and int(row.get("revision", 1)) == int(entry.get("revision", 1))
                                and row.get("状态") == "待验证"):
                            row["状态"] = "已关闭"
                            row["关闭原因"] = "线索撤销"
                    return entry, f"线索 {code} 已撤销，流转终止"
            except TransactionError as exc:
                return None, str(exc)

    def _reopen(self, code: str, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """重开已评价线索：旧版结论留档，新开一条从「待踏勘」起步的修订版本。"""
        with store.lock_for(code):
            try:
                with store.transaction():
                    versions = self.list_versions(code)
                    if not versions:
                        raise TransactionError(f"矿化线索 {code} 不存在，无从重开")
                    live = _latest_revision(code)
                    if live is not None and not live.get("已归档") and not live.get("已撤销"):
                        if live["status"] != STATUS_ORDER[-1]:
                            raise TransactionError(
                                f"线索 {code} 还在「{live['status']}」，只有已评价线索才能重开修订"
                            )
                        # 已评价但尚未归档：先把旧版归档，保证旧结论原样留档、不被覆盖。
                        archived, msg = self._archive(code, {"revision": live["revision"]})
                        if archived is None:
                            raise TransactionError(msg)
                    last = max(versions, key=lambda row: int(row.get("revision", 1)))
                    new_rev = int(last.get("revision", 1)) + 1
                    new_entry = {
                        "id": _next_id(MODULE.lower()),
                        "线索编号": code,
                        "revision": new_rev,
                        "status": STATUS_ORDER[0],
                        "pending": True,
                        "abnormal": False,
                        "已归档": False,
                        "已撤销": False,
                        "勘探区": last.get("勘探区", ""),
                        "矿种": last.get("矿种", ""),
                        "矿化类型": last.get("矿化类型", ""),
                        "发现方式": last.get("发现方式", ""),
                        # 重开不带走旧结论：新修订必须重新踏勘、重新评价。
                        "踏勘日期": "",
                        "评价等级": "",
                        "等级来源": "",
                        "评价结论": "",
                        "结论历史": [],
                        "复核记录": [],
                        "重开自版本": int(last.get("revision", 1)),
                        "version": 1,
                    }
                    store.rows(MODULE.lower()).append(new_entry)
                    return new_entry, (
                        f"线索 {code} 已重开为第 {new_rev} 次修订，从「待踏勘」重新流转；"
                        f"第 {new_rev - 1} 版结论按归档版本留档，新修订不得回到旧结论"
                    )
            except TransactionError as exc:
                return None, str(exc)

    # ------------------------------------------------------------------ 迁移

    def _migrate_legacy_grades(self) -> None:
        """老线索没有评价等级的，按踏勘时间迁移补齐（幂等，只补空值）。"""
        global _migrated
        if _migrated:
            return
        with store.transaction():
            for row in store.rows(MODULE.lower()):
                if str(row.get("评价等级") or "").strip():
                    continue
                grade = infer_grade_by_survey_date(row.get("踏勘日期"))
                row["评价等级"] = grade
                row["等级来源"] = "老线索按踏勘时间迁移"
            for row in store.rows("mineral_archive"):
                if str(row.get("评价等级") or "").strip():
                    continue
                row["评价等级"] = infer_grade_by_survey_date(row.get("踏勘日期"))
                row["等级来源"] = "老线索按踏勘时间迁移"
        _migrated = True
