"""依赖解析业务规则：状态流转、字段校验与筛选口径都收在这里。

表身份解析统一委托给血缘目录（``metadata_catalog``），不再各自按字符串猜测表名，
保证依赖解析与 SQL 模板读取到的是同一规范表身份与字段结构。
"""
from __future__ import annotations

from typing import Any

from app.services.lineage import metadata_catalog
from app.store import store

MODULE = "dependency"
REQUIRED_FIELDS = ["关系编号", "上游作业", "下游作业"]
STATUS_ORDER = ["草稿", "关系评审中", "已生效", "已解除"]
ACTION_RULES = {"提交关系评审": "关系评审中", "生效关系": "已生效", "解除关系": "已解除"}
NEGATIVE_ACTIONS = []


class DependencyService:
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
            rows = [row for row in rows if keyword in str(row.get("关系编号", ""))]
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
            return None, f"依赖关系 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于依赖解析可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"依赖关系已{action}"

    def resolve_identity(self, entry_id: int) -> dict[str, Any] | None:
        """把一条依赖的上游/下游引用解析到统一规范表身份（不改写原始字段）。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        result: dict[str, Any] = {
            "关系编号": entry.get("关系编号"),
            "catalog_version": metadata_catalog.catalog_version(),
            "端点": [],
        }
        for role, key in (("上游", "上游作业"), ("下游", "下游作业")):
            raw = entry.get(key)
            identity = metadata_catalog.resolve_table(raw)
            result["端点"].append(
                {
                    "角色": role,
                    "引用名": raw,
                    "已解析": identity is not None,
                    "表身份": identity.to_dict() if identity else None,
                }
            )
        return result

    def lineage_overview(self) -> dict[str, Any]:
        """全量依赖引用按规范表身份归并的结果，供与 SQL 模板侧交叉核对。"""
        references = metadata_catalog.dependency_references()
        return {
            "module": "dependency",
            "catalog_version": metadata_catalog.catalog_version(),
            "total": len(references),
            "items": [
                {"规范表名": canonical, "引用": refs}
                for canonical, refs in sorted(references.items())
            ],
        }
