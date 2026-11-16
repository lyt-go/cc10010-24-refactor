"""统一数据表字段血缘接口。

对外只新增只读快照、身份解析与字段目录事务接口，不改变既有模块的输入输出：

* ``GET  /api/lineage/snapshot``           统一数据表字段血缘快照
* ``GET  /api/lineage/resolve``            表名（规范名/历史别名）解析到规范身份
* ``GET  /api/lineage/identities``         全部规范表身份
* ``POST /api/lineage/field-catalog``      事务化字段目录写入（全成功或回退）
* ``POST /api/lineage/field-catalog/rollback``  回退到上一份已确认元数据
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services.lineage import (
    CatalogConflict,
    CatalogValidation,
    NAMING_CANONICAL,
    NAMING_CHOICES,
    NAMING_LEGACY,
    metadata_catalog,
)

router = APIRouter(prefix="/api/lineage", tags=["数据表字段血缘"])


class FieldCatalogPayload(BaseModel):
    """字段目录整批写入载荷；expected_version 用于并发校正的 CAS 控制。"""

    records: list[dict[str, Any]] = Field(default_factory=list)
    expected_version: int | None = None
    remark: str | None = None


@router.get("/snapshot")
def get_snapshot(
    naming: str = Query(
        default=NAMING_LEGACY,
        description=f"命名口径：{NAMING_LEGACY}=保留历史别名，{NAMING_CANONICAL}=统一规范名",
    )
) -> dict[str, Any]:
    """从元数据表与字段结构记录解析并归并统一的数据表字段血缘快照。"""
    if naming not in NAMING_CHOICES:
        raise HTTPException(status_code=400, detail=f"不支持的命名口径：{naming}")
    return metadata_catalog.build_snapshot(naming=naming)


@router.get("/identities")
def list_identities() -> dict[str, Any]:
    """全部规范表身份（含历史别名与来源元数据记录）。"""
    identities = [item.to_dict() for item in metadata_catalog.table_identities()]
    return {
        "module": "lineage",
        "catalog_version": metadata_catalog.catalog_version(),
        "total": len(identities),
        "items": identities,
    }


@router.get("/resolve")
def resolve_table(
    name: str = Query(..., description="待解析的表名，可为规范名或历史别名")
) -> dict[str, Any]:
    """把任意表名解析到唯一规范表身份；无法归并时返回未解析而不是报错。"""
    identity = metadata_catalog.resolve_table(name)
    return {
        "module": "lineage",
        "引用名": name,
        "已解析": identity is not None,
        "表身份": identity.to_dict() if identity else None,
    }


@router.post("/field-catalog", response_model=ActionResult)
def write_field_catalog(payload: FieldCatalogPayload) -> ActionResult:
    """事务化写入字段目录：全部成功才提交，否则回退到上一份已确认元数据。

    携带 ``expected_version`` 时做乐观并发控制：确认版本已被别人改写则拒绝提交，
    保证并发校正同一版本只留下一份规范目录。
    """
    try:
        result = metadata_catalog.write_field_catalog(
            payload.records, expected_version=payload.expected_version
        )
    except CatalogConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CatalogValidation as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=result["message"], entry=result)


@router.post("/field-catalog/rollback", response_model=ActionResult)
def rollback_field_catalog() -> ActionResult:
    """把字段目录显式回退到上一份已确认元数据。"""
    result = metadata_catalog.rollback_field_catalog()
    return ActionResult(ok=True, message=result["message"], entry=result)
