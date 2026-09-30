"""矿产评价接口：维护矿化线索的“状态梯级板”。

线索编号确定后只能 待踏勘→踏勘中→评价中→已评价 逐级流转；跳级、终态改写、
归档后写入都在服务层拦下。这里只负责入参出参，不做业务判断。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, MineralActionPayload, PageResult
from app.services.mineral import MineralService

router = APIRouter(prefix="/api/mineral", tags=["矿产评价"])

service = MineralService()

STATUSES = ["待踏勘", "踏勘中", "评价中", "已评价"]


@router.get("/ledger")
def list_ledger(
    status: str | None = Query(default=None, description="现行/已归档/已被修订"),
    all_versions: bool = Query(default=False, description="是否包含历史版本"),
) -> dict[str, Any]:
    """矿产评价台账：默认只看现行版本，历史评价按归档/修订版本留档。"""
    items = service.list_ledger(status=status, only_current=not all_versions)
    return {"module": "mineral_ledger", "total": len(items), "items": items}


@router.get("/deviations")
def list_deviations(status: str | None = Query(default=None)) -> dict[str, Any]:
    """偏离点图清单：提交结论同步落表，归档后冻结为“已归档”。"""
    items = service.list_deviations(status=status)
    return {"module": "mineral_deviation", "total": len(items), "items": items}


@router.get("/todos")
def list_todos(status: str | None = Query(default=None)) -> dict[str, Any]:
    """验证待办：提交结论同步生成，重开/归档后标记为已被修订或已归档。"""
    items = service.list_todos(status=status)
    return {"module": "mineral_todo", "total": len(items), "items": items}


@router.get("/reviews")
def list_reviews(entry_id: int | None = Query(default=None)) -> dict[str, Any]:
    """现场复核记录：冲突时以最近一次现场复核为准。"""
    items = service.list_reviews(entry_id)
    return {"module": "mineral_review", "total": len(items), "items": items}


@router.get("/archive")
def list_archive() -> dict[str, Any]:
    """归档版本清单：归档后只读，台账/偏离点/待办快照都在里面。"""
    items = service.list_archive()
    return {"module": "mineral_archive", "total": len(items), "items": items}


@router.get("/history")
def list_history(code: str | None = Query(default=None)) -> dict[str, Any]:
    """修订历史：重开旧已评价线索后，旧结论留档在此，不允许回到旧结论。"""
    items = service.list_history(code)
    return {"module": "mineral_history", "total": len(items), "items": items}


@router.get("/boards")
def boards() -> dict[str, Any]:
    """状态梯级板看板：四档在线线索数量与三表现行/待办数量。"""
    active, _ = service.list_entries(page=1, size=100000)
    archived = service.list_archive()
    status_counts = {status: 0 for status in STATUSES}
    for row in active:
        status_counts[str(row.get("status"))] = status_counts.get(str(row.get("status")), 0) + 1
    return {
        "status": status_counts,
        "archived": len(archived),
        "ledger_current": len(service.list_ledger()),
        "deviation_current": len(service.list_deviations()),
        "todo_pending": len(service.list_todos(status="待验证")),
    }


@router.get("/export")
def export_entries(include_archived: bool = Query(default=False)) -> dict[str, Any]:
    """导出矿产评价清单：返回当前线索全量数据，可带归档版本。"""
    items, total = service.list_entries(page=1, size=100000, include_archived=include_archived)
    return {"module": "mineral", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按线索编号检索"),
    status: str | None = Query(default=None, description="待踏勘、踏勘中、评价中、已评价"),
    include_archived: bool = Query(default=False, description="是否一并返回已归档版本"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按线索编号与状态过滤矿产评价列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, page=page, size=size, include_archived=include_archived
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条矿化线索明细；归档返回冻结快照，已被替代/归档给出可读原因。"""
    entry, error = service.get_entry(entry_id)
    if entry is None:
        return {"ok": False, "id": entry_id, "message": error}
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条矿化线索，缺字段或编号重复时说明原因而不是静默丢弃。"""
    entry, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="矿化线索已登记，初始状态为待踏勘", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: MineralActionPayload) -> ActionResult:
    """对单条线索执行梯级板动作。

    安排踏勘/开始评价/提交结论 只能逐级；另有 撤销、重开、归档、现场复核。
    跳级、终态改写、归档写入、并发版本冲突都会被拦下并在 message 说明原因；
    相同 idempotencyKey 的再次提交不会重复生效。
    """
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(
        entry_id,
        action,
        payload.values,
        expected_token=payload.expected_token,
        idempotency_key=payload.idempotency_key,
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
