"""SQL模板业务规则：状态流转、字段校验与筛选口径都收在这里。

模板加载统一的数据表字段血缘快照后再校验输出列：SQL 片段里的表名与输出列都经过
血缘目录的同一套身份/字段解析，避免模板把对象引用成另一张表。
"""
from __future__ import annotations

from typing import Any

from app.services.lineage import metadata_catalog, split_name_list, extract_sql_tables
from app.store import store

MODULE = "sqltpl"
REQUIRED_FIELDS = ["模板编号", "模板名称", "模板类型"]
STATUS_ORDER = ["草稿", "模板评审中", "已发布", "已废弃"]
ACTION_RULES = {"提交模板评审": "模板评审中", "发布模板": "已发布", "废弃模板": "已废弃"}
NEGATIVE_ACTIONS = []


class SqltplService:
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
            rows = [row for row in rows if keyword in str(row.get("模板编号", ""))]
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
            return None, f"模板定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于SQL模板可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"模板定义已{action}"

    def validate_output_columns(self, entry_id: int) -> dict[str, Any] | None:
        """加载统一血缘快照，校验模板的表身份与预期输出列。

        返回结构同时给出原始引用名与解析后的规范表身份，便于核对依赖解析和
        SQL 模板读取的是同一张表、同一套字段。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        snapshot = metadata_catalog.build_snapshot()
        snapshot_by_name = {item["canonical_name"]: item for item in snapshot["items"]}

        table_refs = extract_sql_tables(entry.get("SQL片段"))
        expected_columns = split_name_list(entry.get("预期输出列"))
        resolved_tables: list[dict[str, Any]] = []
        available_fields: set[str] = set()
        for raw_name in table_refs:
            identity = metadata_catalog.resolve_table(raw_name)
            canonical = identity.canonical_name if identity else raw_name
            table_snapshot = snapshot_by_name.get(canonical)
            if table_snapshot is not None:
                available_fields.update(
                    field_item["规范字段名"] for field_item in table_snapshot["字段目录"]
                )
            resolved_tables.append(
                {
                    "引用名": raw_name,
                    "已解析": identity is not None,
                    "表身份": identity.to_dict() if identity else None,
                }
            )

        column_checks: list[dict[str, Any]] = []
        referenced_tables = [
            item["表身份"]["canonical_name"]
            for item in resolved_tables
            if item["表身份"] is not None
        ]
        for column in expected_columns:
            # 只在本模板引用到的规范表字段结构内解析，避免跨表字段别名串用。
            canonical_field = None
            for table in referenced_tables:
                candidate = metadata_catalog.resolve_field_name(column, table)
                if candidate is not None:
                    canonical_field = candidate
                    break
            column_checks.append(
                {
                    "输出列": column,
                    "规范字段名": canonical_field,
                    "命中字段结构": bool(canonical_field)
                    and canonical_field in available_fields,
                }
            )
        missing_columns = [
            item["输出列"] for item in column_checks if not item["命中字段结构"]
        ]
        unresolved_tables = [item["引用名"] for item in resolved_tables if not item["已解析"]]
        # 输出列本身未确认（空）时同样视为校验不通过，避免空模板被当成可发布。
        output_columns_confirmed = bool(expected_columns)
        return {
            "模板编号": entry.get("模板编号"),
            "catalog_version": snapshot["catalog_version"],
            "表引用": resolved_tables,
            "输出列校验": column_checks,
            "未解析表": unresolved_tables,
            "缺失输出列": missing_columns,
            "输出列未确认": not output_columns_confirmed,
            "校验通过": output_columns_confirmed
            and not unresolved_tables
            and not missing_columns,
        }
