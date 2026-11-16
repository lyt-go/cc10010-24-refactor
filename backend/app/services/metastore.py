"""元数据存储业务规则：状态流转、字段校验与筛选口径都收在这里。

确认动作不再直接改状态，而是交给统一血缘目录做可绑定门禁：分区字段、主键字段、
所属数仓层和模板输出列全部确认后才能置为「已确认」，并把状态回写 SQL 模板清单。
"""
from __future__ import annotations

from typing import Any

from app.services.lineage import metadata_catalog
from app.store import store

MODULE = "metastore"
REQUIRED_FIELDS = ["表编号", "表名", "所属数仓层"]
STATUS_ORDER = ["草稿", "元数据评审中", "已确认", "已过期"]
ACTION_RULES = {"提交元数据评审": "元数据评审中", "确认元数据": "已确认", "标记过期": "已过期"}
NEGATIVE_ACTIONS = []


class MetastoreService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("表编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"元数据表 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于元数据存储可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        # 确认元数据走统一血缘目录的可绑定门禁，并由目录完成 SQL 模板状态回写。
        if action == "确认元数据":
            return metadata_catalog.confirm_metadata(entry_id)
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"元数据表已{action}"

    def bindability(self, entry_id: int) -> dict[str, Any] | None:
        """读取单表的可绑定判断与一致性来源，定位到同一份规范快照。"""
        return metadata_catalog.bindability(entry_id)

    def writeback_manifest(self) -> dict[str, Any]:
        """元数据确认后的状态回写 SQL 模板清单。"""
        return metadata_catalog.template_writeback_manifest()
