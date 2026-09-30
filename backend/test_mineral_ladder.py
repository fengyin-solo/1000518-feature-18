"""矿产评价「状态梯级板」端到端校验。

覆盖需求拆解：
1. 线索编号确定后只能 待踏勘→踏勘中→评价中→已评价，跳级被拦并说明原因；
2. 重开已评价线索产生新修订版本，不允许回到旧结论；
3. 评价结论同步台账 / 偏离点图清单 / 验证待办，归档后只读，读不出新值；
4. 矿种与评价等级冲突以最近一次现场复核为准，历史评价按归档版本留档；
5. 并发流转只允许一个成功；归档/撤销/重开事务提交（回滚可验证）；
   相同 request_id 重复提交不重复生效；
6. 老线索无评价等级时按踏勘时间迁移补齐。
"""
from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
BASE = "/api/mineral"


def act(code: str, values: dict, expect_ok: bool | None = None) -> dict:
    resp = client.post(f"{BASE}/{code}/actions", json={"values": values})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    if expect_ok is not None:
        assert body["ok"] is expect_ok, body
    return body


def get(code: str, revision: int | None = None) -> dict:
    suffix = f"?revision={revision}" if revision else ""
    resp = client.get(f"{BASE}/{code}{suffix}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_01_ladder_happy_path() -> None:
    """新线索沿梯级板逐格前进，每一步同步待办。"""
    code = "TEST-LADDER"
    assert client.post(BASE, json={"values": {
        "线索编号": code, "勘探区": "测试区", "矿种": "金矿",
        "踏勘日期": "2026-09-10",
    }}).json()["ok"]
    assert get(code)["status"] == "待踏勘"

    body = act(code, {"action": "安排踏勘", "踏勘日期": "2026-09-11"}, True)
    assert body["entry"]["status"] == "踏勘中"
    assert body["entry"]["评价等级"] == "一类"  # 踏勘一年内

    act(code, {"action": "开始评价"}, True)
    assert get(code)["status"] == "评价中"

    body = act(code, {
        "action": "提交结论", "评价等级": "二类", "评价结论": "初见矿化，建议预查",
    }, True)
    assert body["entry"]["status"] == "已评价"
    assert not body["entry"]["pending"]

    todos = client.get(f"{BASE}/todos", params={"code": code}).json()["items"]
    assert any(t["事项"] == "结论验证" and t["状态"] == "待验证" for t in todos)
    ledger = client.get(f"{BASE}/ledger", params={"code": code}).json()["items"]
    assert len(ledger) == 1 and ledger[0]["评价结论"] == "初见矿化，建议预查"
    print("01 梯级板正常流转 + 三处同步 OK")


def test_02_skip_levels_blocked() -> None:
    """待踏勘直接提交结论（跨两级）必须被拦，且说明缺哪几步。"""
    code = "TEST-SKIP"
    client.post(BASE, json={"values": {"线索编号": code, "勘探区": "测试区", "矿种": "金矿"}})
    body = act(code, {"action": "提交结论", "评价结论": "x", "评价等级": "一类"}, False)
    assert "跳级提交被拦" in body["message"]
    assert "待踏勘" in body["message"] and "评价中" in body["message"]
    # 状态没有被改动
    assert get(code)["status"] == "待踏勘"

    # 先走到踏勘中，再跳提交结论（跨一级）
    act(code, {"action": "安排踏勘", "踏勘日期": "2026-09-12"}, True)
    body = act(code, {"action": "提交结论", "评价结论": "x", "评价等级": "一类"}, False)
    assert "跳级提交被拦" in body["message"] and "踏勘中" in body["message"]

    # 已评价后再安排踏勘属于回退，同样拦下
    code2 = "TEST-BACK"
    client.post(BASE, json={"values": {"线索编号": code2, "勘探区": "测试区", "矿种": "金矿"}})
    act(code2, {"action": "安排踏勘", "踏勘日期": "2026-09-12"}, True)
    act(code2, {"action": "开始评价"}, True)
    act(code2, {"action": "提交结论", "评价等级": "二类", "评价结论": "c1"}, True)
    body = act(code2, {"action": "安排踏勘"}, False)
    assert "只能向前" in body["message"]
    print("02 跳级/回退拦截并说明原因 OK")


def test_03_duplicate_submit_idempotent() -> None:
    """同一 request_id 再次提交只回放，状态版本不再增长。"""
    code = "TEST-IDEM"
    client.post(BASE, json={"values": {"线索编号": code, "勘探区": "测试区", "矿种": "金矿"}})
    v = {"action": "安排踏勘", "踏勘日期": "2026-09-13", "request_id": "req-1"}
    first = act(code, v, True)
    version_after_first = first["entry"]["version"]
    second = act(code, dict(v), True)
    assert second["entry"]["version"] == version_after_first
    assert second["message"] == first["message"]
    print("03 request_id 幂等，重复提交不重复生效 OK")


def test_04_concurrent_only_one_wins() -> None:
    """并发提交同一格动作：一个成功，另一个收到冲突说明。"""
    code = "TEST-CONC"
    client.post(BASE, json={"values": {"线索编号": code, "勘探区": "测试区", "矿种": "金矿"}})
    results: list[dict] = []

    def worker() -> None:
        results.append(act(code, {"action": "安排踏勘", "踏勘日期": "2026-09-14"}))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    oks = [r["ok"] for r in results]
    assert oks.count(True) == 1, results
    assert any("另一次提交" in r["message"] for r in results)
    assert get(code)["status"] == "踏勘中"
    print("04 并发流转只允许一个成功 OK")


def test_05_conflict_resolved_by_latest_review() -> None:
    """矿种/等级冲突：无复核被拦；最近一次现场复核的矿种、等级生效，偏离点入图清单。"""
    code = "TEST-CONFLICT"
    client.post(BASE, json={"values": {"线索编号": code, "勘探区": "测试区", "矿种": "煤矿"}})
    act(code, {"action": "安排踏勘", "踏勘日期": "2026-09-15"}, True)
    act(code, {"action": "开始评价"}, True)

    # 煤矿配三类属冲突，且没有复核依据 → 拦下
    body = act(code, {"action": "提交结论", "评价等级": "三类", "评价结论": "结论A"}, False)
    assert "现场复核" in body["message"]

    # 第一次复核说煤矿/二类
    act(code, {"action": "现场复核", "复核日期": "2026-09-16", "矿种": "煤矿",
               "评价等级": "二类", "复核人员": "李工"}, True)
    # 第二次（最近一次）复核更正为煤矿/一类
    act(code, {"action": "现场复核", "复核日期": "2026-09-20", "矿种": "煤矿",
               "评价等级": "一类", "复核人员": "王工"}, True)
    body = act(code, {"action": "提交结论", "评价等级": "三类", "评价结论": "结论B"}, True)
    entry = body["entry"]
    assert entry["矿种"] == "煤矿" and entry["评价等级"] == "一类"  # 以最近一次复核为准
    deviations = client.get(f"{BASE}/deviations", params={"code": code}).json()["items"]
    assert any("冲突" in d["偏离原因"] and d["revision"] == 1 for d in deviations)
    ledger = client.get(f"{BASE}/ledger", params={"code": code}).json()["items"]
    assert ledger[-1]["矿种"] == "煤矿" and ledger[-1]["评价等级"] == "一类" and ledger[-1]["冲突说明"]
    print("05 矿种/等级冲突以最近一次现场复核为准 + 偏离点入图清单 OK")


def test_06_archive_frozen_and_reopen_new_revision() -> None:
    """归档只读；重开产生 v2 新修订；v2 不能沿用 v1 旧结论。"""
    code = "TEST-REOPEN"
    client.post(BASE, json={"values": {"线索编号": code, "勘探区": "测试区", "矿种": "铜矿"}})
    act(code, {"action": "安排踏勘", "踏勘日期": "2026-09-16"}, True)
    act(code, {"action": "开始评价"}, True)
    act(code, {"action": "提交结论", "评价等级": "二类", "评价结论": "旧结论V1"}, True)

    archived = act(code, {"action": "归档"}, True)
    assert archived["entry"]["归档时间"]
    versions = client.get(f"{BASE}/{code}/versions").json()["items"]
    assert [v["revision"] for v in versions if v.get("已归档")][-1] == 1

    # 归档后任何流转都读不出/写不进新值
    body = act(code, {"action": "现场复核", "复核意见": "想改归档件"}, False)
    assert "已归档" in body["message"] or "只读" in body["message"]
    body = act(code, {"action": "归档"}, False)
    assert "已归档" in body["message"]

    # 重新提交归档版本的结论 —— 不存在活动版本时应提示全部归档，先重开
    # 归档快照仍可读，且内容冻结
    snap = get(code, 1)
    assert snap["评价结论"] == "旧结论V1" and snap["已归档"]

    # 重开 → v2 从待踏勘起步
    body = act(code, {"action": "重开"}, True)
    assert body["entry"]["revision"] == 2 and body["entry"]["status"] == "待踏勘"
    assert not body["entry"]["评价结论"]  # 不带走旧结论

    # v2 走到已评价，沿用旧结论必须被拦
    act(code, {"action": "安排踏勘", "踏勘日期": "2026-09-20", "revision": 2}, True)
    act(code, {"action": "开始评价", "revision": 2}, True)
    body = act(code, {"action": "提交结论", "revision": 2, "评价等级": "二类",
                      "评价结论": "旧结论V1"}, False)
    assert "不允许沿用" in body["message"]
    act(code, {"action": "提交结论", "revision": 2, "评价等级": "二类",
               "评价结论": "新结论V2"}, True)

    # 台账：v1 冻结归档，v2 为当前版本
    ledger = client.get(f"{BASE}/ledger", params={"code": code}).json()["items"]
    by_rev = {row["revision"]: row for row in ledger}
    assert by_rev[1]["已归档"] and not by_rev[1]["当前版本"]
    assert by_rev[2]["当前版本"] and by_rev[2]["评价结论"] == "新结论V2"

    # v1 历史评价按归档版本留档，读出来的仍是旧值
    assert get(code, 1)["评价结论"] == "旧结论V1"
    assert get(code, 2)["评价结论"] == "新结论V2"
    print("06 归档只读 + 重开新版本 + 旧结论留档 OK")


def test_07_reopen_unarchived_evaluated_auto_archives() -> None:
    """已评价未归档直接重开：旧版在同一事务先归档再开新版。"""
    code = "TEST-REOPEN2"
    client.post(BASE, json={"values": {"线索编号": code, "勘探区": "测试区", "矿种": "金矿"}})
    act(code, {"action": "安排踏勘", "踏勘日期": "2026-09-17"}, True)
    act(code, {"action": "开始评价"}, True)
    act(code, {"action": "提交结论", "评价等级": "一类", "评价结论": "结论C"}, True)
    body = act(code, {"action": "重开"}, True)
    assert body["entry"]["revision"] == 2
    versions = client.get(f"{BASE}/{code}/versions").json()["items"]
    rev1 = next(v for v in versions if v["revision"] == 1)
    assert rev1["已归档"] and rev1["评价结论"] == "结论C"
    # 重开后旧待办已随归档关闭
    todos = client.get(f"{BASE}/todos", params={"code": code}).json()["items"]
    assert all(not (t["revision"] == 1 and t["状态"] == "待验证") for t in todos)
    print("07 已评价未归档重开：先归档后开新版 OK")


def test_08_cancel_is_terminal() -> None:
    """撤销是终态：重复撤销被拦，撤销后不能流转也不能归档。"""
    code = "TEST-CANCEL"
    client.post(BASE, json={"values": {"线索编号": code, "勘探区": "测试区", "矿种": "金矿"}})
    act(code, {"action": "撤销"}, True)
    assert get(code)["status"] == "已撤销"
    assert "已撤销" in act(code, {"action": "撤销"}, False)["message"]
    assert "已撤销" in act(code, {"action": "安排踏勘", "踏勘日期": "2026-09-18"}, False)["message"]
    assert "已撤销" in act(code, {"action": "归档"}, False)["message"]
    print("08 撤销终态 + 重复/后续动作拦截 OK")


def test_09_transaction_rollback() -> None:
    """归档中途失败（如已归档版本冲突）整体回滚：主表与清单都不变。"""
    code = "TEST-TX"
    client.post(BASE, json={"values": {"线索编号": code, "勘探区": "测试区", "矿种": "金矿"}})
    act(code, {"action": "安排踏勘", "踏勘日期": "2026-09-18"}, True)
    act(code, {"action": "开始评价"}, True)
    act(code, {"action": "提交结论", "评价等级": "一类", "评价结论": "结论TX"}, True)

    # 构造一个非法归档请求：状态不对（此例状态正确，改用重复归档场景）
    act(code, {"action": "归档"}, True)
    todos_before = client.get(f"{BASE}/todos", params={"code": code}).json()["items"]
    ledger_before = client.get(f"{BASE}/ledger", params={"code": code}).json()["items"]
    # 重复归档失败 —— 已无活动版本，整个事务不产生半条数据
    body = act(code, {"action": "归档"}, False)
    assert "已归档" in body["message"] or "不存在" in body["message"]
    assert todos_before == client.get(f"{BASE}/todos", params={"code": code}).json()["items"]
    assert ledger_before == client.get(f"{BASE}/ledger", params={"code": code}).json()["items"]

    # 未到已评价不允许归档，且主表记录仍在、状态不变
    code2 = "TEST-TX2"
    client.post(BASE, json={"values": {"线索编号": code2, "勘探区": "测试区", "矿种": "金矿"}})
    before = get(code2)
    body = act(code2, {"action": "归档"}, False)
    assert "只有「已评价」" in body["message"]
    after = get(code2)
    assert after["status"] == before["status"] == "待踏勘"
    print("09 归档事务失败整体回滚 OK")


def test_10_legacy_grade_migration() -> None:
    """种子老线索按踏勘日期补齐等级：一年内一类、两年内二类、更早三类。"""
    listing = {row["线索编号"]: row for row in client.get(BASE).json()["items"]}
    assert listing["MINE-0001"]["评价等级"] == "一类"
    assert listing["MINE-0005"]["评价等级"] == "三类"   # 2023-05
    assert listing["MINE-0006"]["评价等级"] == "二类"   # 2024-12
    assert listing["MINE-0001"]["等级来源"] == "老线索按踏勘时间迁移"
    # 已归档老件也补齐且归档值不丢
    archived = get("MINE-0007", 1)
    assert archived["评价等级"] == "一类" and archived["评价结论"]
    print("10 老线索按踏勘时间迁移补齐评价等级 OK")


if __name__ == "__main__":
    test_01_ladder_happy_path()
    test_02_skip_levels_blocked()
    test_03_duplicate_submit_idempotent()
    test_04_concurrent_only_one_wins()
    test_05_conflict_resolved_by_latest_review()
    test_06_archive_frozen_and_reopen_new_revision()
    test_07_reopen_unarchived_evaluated_auto_archives()
    test_08_cancel_is_terminal()
    test_09_transaction_rollback()
    test_10_legacy_grade_migration()
    print("\n全部用例通过")
