"""矿产评价“状态梯级板”领域规则测试。

只用标准库（python3 -m unittest），不依赖 fastapi/httpx：业务规则都在 service 层，
路由只做转发。每个用例换一个全新的内存 Store，避免种子数据互相干扰。
"""
from __future__ import annotations

import threading
import unittest

from app.store import Store
from app.services import mineral as mineral_module
from app.services import mineral_migration


def fresh_service() -> mineral_module.MineralService:
    store = Store()
    mineral_module.store = store
    mineral_migration.store = store
    return mineral_module.MineralService()


class LadderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = fresh_service()
        self.entry, _ = self.service.create_entry(
            {"线索编号": "MINE-T1", "勘探区": "甲区", "矿种": "铜矿", "踏勘日期": "2026-09-10"}
        )
        self.eid = self.entry["id"]

    def _act(self, action: str, **values):
        return self.service.run_action(self.eid, action, values)

    def test_skip_level_is_blocked_with_reason(self):
        entry, message = self._act("提交结论", 评价结论="见矿", 评价等级="一类")
        self.assertIsNone(entry)
        self.assertIn("直接跳到", message)
        self.assertIn("逐级", message)

    def test_walk_forward_one_step_at_a_time(self):
        self.assertEqual(self.entry["status"], "待踏勘")
        _, msg = self._act("安排踏勘")
        self.assertIn("踏勘中", msg)
        _, msg = self._act("开始评价")
        self.assertIn("评价中", msg)
        # 缺结论不能提交
        entry, message = self._act("提交结论", 评价等级="一类")
        self.assertIsNone(entry)
        self.assertIn("评价结论", message)
        entry, message = self._act("提交结论", 评价结论="见矿明显", 评价等级="一类")
        self.assertEqual(entry["status"], "已评价")

    def test_repeat_same_step_is_idempotently_blocked(self):
        self._act("安排踏勘")
        entry, message = self._act("安排踏勘")
        self.assertIsNone(entry)
        self.assertIn("已提交过", message)

    def test_undo_only_one_step_and_not_from_evaluated(self):
        entry, _ = self._act("撤销")
        self.assertIsNone(entry)  # 起点不可撤销
        self._act("安排踏勘")
        entry, _ = self._act("撤销")
        self.assertEqual(entry["status"], "待踏勘")
        self._act("安排踏勘"); self._act("开始评价")
        self._act("提交结论", 评价结论="见矿", 评价等级="一类")
        entry, message = self._act("撤销")
        self.assertIsNone(entry)
        self.assertIn("重开", message)


class SyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = fresh_service()
        self.entry, _ = self.service.create_entry(
            {"线索编号": "MINE-S1", "勘探区": "乙区", "矿种": "金矿"}
        )
        self.eid = self.entry["id"]
        for action in ("安排踏勘", "开始评价"):
            self.service.run_action(self.eid, action, {})

    def test_conclusion_syncs_ledger_deviation_todo(self):
        entry, _ = self.service.run_action(
            self.eid, "提交结论", {"评价结论": "矿化弱，品位低，建议否定", "评价等级": "三类"}
        )
        self.assertEqual(entry["status"], "已评价")
        ledger = self.service.list_ledger()
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["线索编号"], "MINE-S1")
        self.assertEqual(ledger[0]["评价等级"], "三类")
        deviations = self.service.list_deviations()
        self.assertEqual(len(deviations), 1)
        self.assertEqual(deviations[0]["生效状态"], "现行")
        todos = self.service.list_todos()
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["待办状态"], "待验证")
        self.assertEqual(todos[0]["优先级"], "低")

    def test_positive_conclusion_has_no_deviation_but_has_todo(self):
        self.service.run_action(
            self.eid, "提交结论", {"评价结论": "见矿明显，品位高", "评价等级": "一类"}
        )
        self.assertEqual(len(self.service.list_deviations()), 0)
        todos = self.service.list_todos()
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["优先级"], "高")


class RevisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = fresh_service()
        self.entry, _ = self.service.create_entry(
            {"线索编号": "MINE-R1", "勘探区": "丙区", "矿种": "铅锌矿"}
        )
        self.eid = self.entry["id"]
        for action in ("安排踏勘", "开始评价"):
            self.service.run_action(self.eid, action, {})
        self.service.run_action(
            self.eid, "提交结论", {"评价结论": "初版结论", "评价等级": "二类"}
        )

    def test_reopen_creates_new_revision_and_keeps_old(self):
        # 必须填重开原因
        entry, message = self.service.run_action(self.eid, "重开", {})
        self.assertIsNone(entry)
        self.assertIn("重开原因", message)

        new, message = self.service.run_action(
            self.eid, "重开", {"重开原因": "野外新证据"}
        )
        self.assertEqual(new["修订版本"], 2)
        self.assertEqual(new["status"], "待踏勘")
        self.assertEqual(new["评价结论"], "")
        self.assertNotEqual(new["id"], self.eid)
        self.assertIn("不能回到旧结论", message)

        # 旧版本只能读历史，不能再操作
        old, error = self.service.get_entry(self.eid)
        self.assertIsNone(old)
        self.assertIn("当前版本", error)
        history = self.service.list_history("MINE-R1")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["修订版本"], 1)
        self.assertEqual(history[0]["评价结论"], "初版结论")

        # 旧版本三表同步行被标记为已被修订
        self.assertEqual(self.service.list_ledger(status="已被修订")[0]["线索编号"], "MINE-R1")
        self.assertEqual(self.service.list_todos(status="已被修订")[0]["线索编号"], "MINE-R1")

        # 新版本走完，现行台账指向 v2，旧结论不被覆盖
        for action, values in (
            ("安排踏勘", {}), ("开始评价", {}),
            ("提交结论", {"评价结论": "修订后结论", "评价等级": "一类"}),
        ):
            new, _ = self.service.run_action(new["id"], action, values)
        current = [r for r in self.service.list_ledger() if r["线索编号"] == "MINE-R1"]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["修订版本"], 2)
        self.assertEqual(current[0]["评价结论"], "修订后结论")
        self.assertEqual(history[0]["评价结论"], "初版结论")  # 旧结论留档不变

    def test_archive_freezes_and_blocks_new_writes(self):
        archived, message = self.service.run_action(self.eid, "归档", {})
        self.assertTrue(archived["已归档"])
        self.assertIn("冻结", message)
        # 活动表读不到（get_entry 回退到归档快照），归档表读到冻结快照
        snapshot_get, error = self.service.get_entry(self.eid)
        self.assertIsNone(error)
        self.assertTrue(snapshot_get["已归档"])
        snapshot = self.service.list_archive()[0]
        self.assertEqual(snapshot["评价结论"], "初版结论")
        self.assertTrue(snapshot["台账"])
        self.assertTrue(snapshot["验证待办"])
        # 归档后任何写动作都被拦下
        for action, values in (
            ("现场复核", {"评价等级": "三类"}),
            ("重开", {"重开原因": "x"}),
            ("归档", {}),
            ("撤销", {}),
        ):
            entry, msg = self.service.run_action(self.eid, action, values)
            self.assertIsNone(entry, action)
            self.assertIn("归档", msg)
        # 现行台账已清空该线索，只剩已归档
        self.assertFalse(self.service.list_ledger())
        self.assertEqual(self.service.list_ledger(status="已归档")[0]["线索编号"], "MINE-R1")
        self.assertEqual(self.service.list_todos(status="已归档")[0]["待办状态"], "已归档")


class ReviewConflictTest(unittest.TestCase):
    def test_latest_field_review_wins_on_grade_conflict(self):
        service = fresh_service()
        entry, _ = service.create_entry({"线索编号": "MINE-C1", "勘探区": "丁区", "矿种": "铜矿"})
        eid = entry["id"]
        service.run_action(eid, "现场复核", {"评价等级": "一类", "复核时间": "2026-09-01T09:00:00"})
        service.run_action(eid, "现场复核", {"矿种": "钼矿", "评价等级": "二类", "复核时间": "2026-09-02T09:00:00"})
        for action in ("安排踏勘", "开始评价"):
            service.run_action(eid, action, {})
        # 提交时给的等级与最近复核冲突 -> 以复核“二类”为准，矿种也以复核为准
        result, message = service.run_action(
            eid, "提交结论", {"评价结论": "见矿", "评价等级": "三类"}
        )
        self.assertEqual(result["评价等级"], "二类")
        self.assertEqual(result["矿种"], "钼矿")
        self.assertIn("以复核为准", message)
        self.assertEqual(service.list_ledger()[0]["评价等级"], "二类")
        self.assertEqual(service.list_ledger()[0]["矿种"], "钼矿")

    def test_field_review_reconciles_deviation_after_evaluated(self):
        service = fresh_service()
        entry, _ = service.create_entry({"线索编号": "MINE-C2", "勘探区": "丁区", "矿种": "铜矿"})
        eid = entry["id"]
        for action in ("安排踏勘", "开始评价"):
            service.run_action(eid, action, {})
        service.run_action(eid, "提交结论", {"评价结论": "见矿明显", "评价等级": "一类"})
        self.assertEqual(len(service.list_deviations()), 0)
        # 复核改判三类 -> 偏离点出现
        service.run_action(eid, "现场复核", {"评价等级": "三类", "复核时间": "2026-09-05T09:00:00"})
        self.assertEqual(len(service.list_deviations()), 1)
        self.assertEqual(service.list_deviations()[0]["评价等级"], "三类")
        # 再次复核改判一类 -> 现行偏离点撤销（默认只看现行，历史行留痕不删除）
        service.run_action(eid, "现场复核", {"评价等级": "一类", "复核时间": "2026-09-06T09:00:00"})
        self.assertEqual(len(service.list_deviations()), 0)
        self.assertEqual(len(service.list_deviations(status="复核后撤销")), 1)


class ConcurrencyTest(unittest.TestCase):
    def test_only_one_parallel_transition_succeeds(self):
        service = fresh_service()
        entry, _ = service.create_entry({"线索编号": "MINE-X1", "勘探区": "戊区", "矿种": "银矿"})
        eid = entry["id"]  # rev_token = 1
        results: list[object] = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            results.append(service.run_action(eid, "安排踏勘", {}, expected_token=1))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ok = [r for r in results if r[0] is not None]
        blocked = [r for r in results if r[0] is None]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(blocked), 1)
        self.assertIn("并发", blocked[0][1])

    def test_stale_token_after_someone_else_moved_is_blocked(self):
        service = fresh_service()
        entry, _ = service.create_entry({"线索编号": "MINE-X2", "勘探区": "己区", "矿种": "钨矿"})
        eid = entry["id"]
        service.run_action(eid, "安排踏勘", {})  # token -> 2
        stale, message = service.run_action(eid, "开始评价", {}, expected_token=1)
        self.assertIsNone(stale)
        self.assertIn("版本号不一致", message)
        fresh, _ = service.run_action(eid, "开始评价", {}, expected_token=2)
        self.assertEqual(fresh["status"], "评价中")

    def test_idempotency_key_does_not_apply_twice(self):
        service = fresh_service()
        entry, _ = service.create_entry({"线索编号": "MINE-X3", "勘探区": "庚区", "矿种": "锡矿"})
        eid = entry["id"]
        first, _ = service.run_action(eid, "安排踏勘", {}, idempotency_key="key-1")
        token_after_first = first["rev_token"]
        second, _ = service.run_action(eid, "安排踏勘", {}, idempotency_key="key-1")
        self.assertEqual(second["rev_token"], token_after_first)  # 没有二次推进
        current, _ = service.get_entry(eid)
        self.assertEqual(current["rev_token"], token_after_first)
        self.assertEqual(current["status"], "踏勘中")

    def test_same_idempotency_key_concurrently_applies_once(self):
        service = fresh_service()
        entry, _ = service.create_entry({"线索编号": "MINE-X4", "勘探区": "壬区", "矿种": "钼矿"})
        eid = entry["id"]
        outcomes: list[object] = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            # 同键并发：即使不带 expected_token，幂等缓存也只能让一次真正推进
            outcomes.append(service.run_action(eid, "安排踏勘", {}, idempotency_key="same-key"))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(outcomes), 4)
        current, _ = service.get_entry(eid)
        self.assertEqual(current["status"], "踏勘中")
        # token 只在唯一一次成功里 +1，其余命中首次结果，没有重复推进
        self.assertEqual(current["rev_token"], 2)


class TransactionTest(unittest.TestCase):
    def test_failure_during_sync_rolls_back_entire_transition(self):
        service = fresh_service()
        entry, _ = service.create_entry({"线索编号": "MINE-TX", "勘探区": "辛区", "矿种": "铁矿"})
        eid = entry["id"]
        service.run_action(eid, "安排踏勘", {})
        service.run_action(eid, "开始评价", {})

        def boom(entry):  # 模拟三表同步中途失败
            raise RuntimeError("同步台账失败")

        service._sync_evaluated = boom  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            service.run_action(eid, "提交结论", {"评价结论": "见矿", "评价等级": "一类"})
        # 整事务回滚：状态仍是评价中，台账没有写入半截数据
        current, _ = service.get_entry(eid)
        self.assertEqual(current["status"], "评价中")
        self.assertFalse(service.list_ledger())
        self.assertFalse(service.list_todos())


class MigrationTest(unittest.TestCase):
    def test_seed_leads_without_grade_backfilled_by_survey_date(self):
        store = Store()
        mineral_module.store = store
        mineral_migration.store = store
        service = mineral_module.MineralService()  # 构造时迁移
        rows = store.rows("mineral")
        by_code = {row["线索编号"]: row for row in rows}
        self.assertEqual(by_code["MINE-0001"]["评价等级"], "一类")
        self.assertEqual(by_code["MINE-0002"]["评价等级"], "二类")
        self.assertEqual(by_code["MINE-0003"]["评价等级"], "三类")
        for row in rows:
            self.assertEqual(row["等级来源"], "踏勘时间迁移")
            self.assertEqual(row["迁移版本"], 1)
        # 再次构造不重复迁移、不覆盖
        mineral_module.MineralService()
        self.assertEqual(store.rows("mineral")[0]["等级来源"], "踏勘时间迁移")

    def test_assign_grades_buckets_and_handles_missing_dates(self):
        rows = [
            {"id": 1, "踏勘日期": "2026-09-04"},
            {"id": 2, "踏勘日期": "2026-09-01"},
            {"id": 3, "踏勘日期": ""},
            {"id": 4, "踏勘日期": "not-a-date"},
            {"id": 5, "踏勘日期": "2026-09-02"},
            {"id": 6, "踏勘日期": "2026-09-03"},
        ]
        # 6 条分三档，每档 2 条；无日期的排最后
        count = mineral_migration.assign_grades(rows)
        self.assertEqual(count, 6)
        ordered = sorted(rows, key=lambda r: r["id"])
        by_id = {r["id"]: r for r in ordered}
        # 排序后日期顺序: 09-01(id2),09-02(id5) -> 一类; 09-03(id6),09-04(id1) -> 二类; 无 id3,id4 -> 三类
        self.assertEqual(by_id[2]["评价等级"], "一类")
        self.assertEqual(by_id[5]["评价等级"], "一类")
        self.assertEqual(by_id[6]["评价等级"], "二类")
        self.assertEqual(by_id[1]["评价等级"], "二类")
        self.assertEqual(by_id[3]["评价等级"], "三类")
        self.assertEqual(by_id[4]["评价等级"], "三类")
        # 已有等级不覆盖
        existed = [{"id": 9, "踏勘日期": "2026-09-01", "评价等级": "二类"}]
        self.assertEqual(mineral_migration.assign_grades(existed), 0)
        self.assertEqual(existed[0]["评价等级"], "二类")


if __name__ == "__main__":
    unittest.main(verbosity=2)
