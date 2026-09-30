"""矿产评价接口：维护矿化线索的状态梯级板与修订版本。

动作覆盖：安排踏勘、开始评价、提交结论、现场复核、归档、撤销、重开。
配套清单：矿产评价台账、偏离点图清单、验证待办，均随评价结论在同一事务同步。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.mineral import MODULE, MineralService
from app.store import store

router = APIRouter(prefix="/api/mineral", tags=["矿产评价"])

service = MineralService()

LIST_FIELDS = ["线索编号", "勘探区", "矿种", "矿化类型", "发现方式", "踏勘日期", "评价等级", "评价结论", "线索状态"]
STATUSES = ["待踏勘", "踏勘中", "评价中", "已评价", "已撤销"]

# 动作可从 values 里取的控制字段与业务字段。
_CONTROL_KEYS = ("action", "revision", "expected_version", "request_id")
_BUSINESS_KEYS = (
    "矿种", "踏勘日期", "踏勘人员", "评价结论", "评价等级",
    "复核日期", "复核人员", "复核意见",
)


def _resolve_code(identifier: str) -> str:
    """路径标识兼容数字 id 与线索编号：数字时先解析出线索编号。"""
    if not identifier.isdigit():
        return identifier
    entry_id = int(identifier)
    for table in ("mineral", "mineral_archive"):
        row = store.find(table, entry_id)
        if row is not None:
            return str(row["线索编号"])
    return identifier


def _extract_action(values: dict[str, Any]) -> tuple[str, dict[str, Any], int | None, str | None]:
    action = str(values.get("action") or "").strip()
    expected_version = values.get("expected_version", values.get("version"))
    request_id = values.get("request_id")
    payload = {
        key: values[key]
        for key in (*_CONTROL_KEYS, *_BUSINESS_KEYS)
        if key in values and key != "action"
    }
    return action, payload, expected_version, str(request_id) if request_id else None


@router.get("/ledger")
def list_ledger(code: str | None = Query(default=None, description="按线索编号过滤")) -> dict[str, Any]:
    """矿产评价台账：每个已提交结论的修订版本一条账，归档版本冻结。"""
    return {"module": "mineral_ledger", "items": service.list_ledger(code)}


@router.get("/deviations")
def list_deviations(code: str | None = Query(default=None, description="按线索编号过滤")) -> dict[str, Any]:
    """偏离点图清单：矿种与评价等级冲突等需上图核实的偏离点。"""
    return {"module": "mineral_deviation", "items": service.list_deviations(code)}


@router.get("/todos")
def list_todos(
    code: str | None = Query(default=None, description="按线索编号过滤"),
    open_only: bool = False,
) -> dict[str, Any]:
    """验证待办：随安排踏勘、开始评价、提交结论同步生成或关闭。"""
    return {"module": "mineral_todo", "items": service.list_todos(code, open_only=open_only)}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按线索编号检索"),
    status: str | None = Query(default=None, description="待踏勘、踏勘中、评价中、已评价、已撤销"),
    include_archived: bool = Query(default=False, description="是否一并读出归档版本"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按线索编号与状态过滤矿产评价列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, include_archived=include_archived, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出矿产评价清单：返回全量活动版本（归档版本走版本/台账接口，不在此读出新值）。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": MODULE.lower(), "total": total, "items": items}


@router.get("/{identifier}/versions")
def list_versions(identifier: str) -> dict[str, Any]:
    """读出某线索编号下的全部修订版本：活动版本与历史归档版本按版本号留档。"""
    code = _resolve_code(identifier)
    versions = service.list_versions(code)
    if not versions:
        raise HTTPException(status_code=404, detail=f"矿化线索 {code} 不存在")
    return {"线索编号": code, "total": len(versions), "items": versions}


@router.get("/{identifier}", response_model=dict)
def get_entry(identifier: str, revision: int | None = None) -> dict:
    """读取单条矿化线索明细；已归档版本返回归档快照，读不出新值。"""
    code = _resolve_code(identifier)
    entry = service.get_entry(code, revision)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"矿化线索 {code} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条矿化线索，线索编号一旦确定即作为后续状态流转与修订的业务主键。"""
    try:
        entry, missing = service.create_entry(payload.values)
    except Exception as exc:  # 业务拦截统一回 ActionResult，路由层不做业务判断
        return ActionResult(ok=False, message=str(exc))
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="矿化线索已登记", entry=entry)


@router.post("/{identifier}/actions", response_model=ActionResult)
def run_action(identifier: str, payload: EntryPayload) -> ActionResult:
    """驱动状态梯级板。

    - 跳级/回退、重复提交、归档后修改、修订沿用旧结论等都会被拦下并在 message 说明原因；
    - 并发流转同一线索只允许一个成功，落败方收到冲突说明；
    - 携带相同 request_id 的再次提交回放首次结果，不重复生效。
    """
    code = _resolve_code(identifier)
    action, data, expected_version, request_id = _extract_action(payload.values)
    entry, message, conflict = service.run_action(
        code, action, data, expected_version=expected_version, request_id=request_id
    )
    if conflict:
        return ActionResult(ok=False, message=message, conflict=True, entry=entry)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
