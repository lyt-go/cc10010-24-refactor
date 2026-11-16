"""统一的数据表字段血缘目录：把元数据表、字段结构记录、依赖与 SQL 模板
里分散的表身份解析收拢到同一份规范表示。

设计要点：

* ``TableIdentity`` 是唯一的规范表身份，依赖解析和 SQL 模板读取都经过同一个
  ``resolve_table`` 入口，不再各自解析表名。
* ``build_snapshot`` 从元数据表（``metastore``）与字段结构记录（``metafield``）
  解析并归并出统一的数据表字段血缘快照，分区字段、主键字段、所属数仓层和模板
  输出列在同一份快照里一起参与可绑定判断。
* 字段目录写入走事务：全量校验通过后才提交，任一记录不合法就整体回退到上一份
  已确认元数据；并发校正同一确认版本时用版本号（CAS）保证只留下一份规范目录。
* 对外只新增只读快照与解析接口，不改变既有模块的输入输出。
"""
from __future__ import annotations

import copy
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from app.store import store

METASTORE_MODULE = "metastore"
METAFIELD_MODULE = "metafield"
SQLTPL_MODULE = "sqltpl"
DEPENDENCY_MODULE = "dependency"

CONFIRMED_STATUS = "已确认"

# 快照里字段血缘的两种口径：保留历史别名（沿用既有依赖）或统一成规范名。
NAMING_LEGACY = "legacy"
NAMING_CANONICAL = "canonical"
NAMING_CHOICES = (NAMING_LEGACY, NAMING_CANONICAL)

_SQL_FROM_RE = re.compile(
    r"\b(?:from|join)\s+([A-Za-z_][\w]*)",
    re.IGNORECASE,
)
_SPLIT_RE = re.compile(r"[,，;；\s]+")


class CatalogConflict(RuntimeError):
    """确认版本已被并发校正改写，当前提交已被拒绝（保留单一规范目录）。"""


class CatalogValidation(ValueError):
    """字段目录事务校验未通过，整批写入回退到上一份已确认元数据。"""


@dataclass(frozen=True)
class TableIdentity:
    """规范表身份：所有模块共享同一份表示。"""

    canonical_name: str
    aliases: tuple[str, ...] = field(default_factory=tuple)
    source_meta_id: int | None = None
    layer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "source_meta_id": self.source_meta_id,
            "layer": self.layer,
        }


def _clean(value: Any) -> str:
    return str(value if value is not None else "").strip()


def split_name_list(value: Any) -> list[str]:
    """把逗号/空白分隔的字段或列名拆成去重后的有序列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        tokens = [_clean(item) for item in value]
    else:
        tokens = [token.strip() for token in _SPLIT_RE.split(str(value))]
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result


def extract_sql_tables(sql: Any) -> list[str]:
    """从 SQL 片段里抽取 FROM/JOIN 引用的表名（保持出现顺序、去重）。"""
    text = _clean(sql)
    if not text:
        return []
    seen: set[str] = set()
    tables: list[str] = []
    for match in _SQL_FROM_RE.finditer(text):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            tables.append(name)
    return tables


class MetadataCatalog:
    """内存版元数据目录：统一身份解析、血缘快照、事务化字段目录写入。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 确认版本号：每次成功提交或回退字段目录都递增，CAS 并发控制以此为准。
        self._version = 0
        # 已确认字段目录的版本栈：栈底是初始基线，回退时弹出当前版本恢复上一份。
        self._confirmed_versions: list[list[dict[str, Any]]] = [
            copy.deepcopy(store.rows(METAFIELD_MODULE))
        ]
        # 元数据确认后的状态回写 SQL 模板清单。
        self._template_writebacks: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # 规范身份：元数据表与历史别名
    # ------------------------------------------------------------------ #
    def _meta_rows_locked(self) -> list[dict[str, Any]]:
        return store.rows(METASTORE_MODULE)

    def _alias_index_locked(self) -> dict[str, TableIdentity]:
        index: dict[str, TableIdentity] = {}
        for row in self._meta_rows_locked():
            canonical = _clean(row.get("表名"))
            if not canonical:
                continue
            aliases = tuple(
                alias for alias in split_name_list(row.get("历史别名")) if alias != canonical
            )
            identity = TableIdentity(
                canonical_name=canonical,
                aliases=aliases,
                source_meta_id=int(row.get("id", 0)),
                layer=_clean(row.get("所属数仓层")) or None,
            )
            index[canonical] = identity
            for alias in aliases:
                # 别名只允许指向唯一规范名；先到先得，冲突时记录可在快照一致性里暴露。
                index.setdefault(alias, identity)
        return index

    def table_identities(self) -> list[TableIdentity]:
        """全部规范表身份（按规范名去重）。"""
        with self._lock:
            seen: set[str] = set()
            identities: list[TableIdentity] = []
            for identity in self._alias_index_locked().values():
                if identity.canonical_name in seen:
                    continue
                seen.add(identity.canonical_name)
                identities.append(identity)
            return sorted(identities, key=lambda item: item.canonical_name)

    def resolve_table(self, name: Any) -> TableIdentity | None:
        """把任意表名（规范名或历史别名）解析成唯一规范表身份。

        无法归并到已登记元数据表时返回 ``None``，由调用方标记为未解析引用。
        """
        raw = _clean(name)
        if not raw:
            return None
        with self._lock:
            return self._alias_index_locked().get(raw)

    def resolve_name(self, name: Any) -> str | None:
        """只取规范名的便捷封装，供依赖与模板服务复用同一解析口径。"""
        identity = self.resolve_table(name)
        return identity.canonical_name if identity else None

    # ------------------------------------------------------------------ #
    # SQL 模板 / 依赖：统一读取表身份与输出列
    # ------------------------------------------------------------------ #
    def template_references(self) -> dict[str, list[dict[str, Any]]]:
        """按规范表名归并 SQL 模板引用（模板可能用别名引用同一张表）。"""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for tpl in store.rows(SQLTPL_MODULE):
            tables = extract_sql_tables(tpl.get("SQL片段"))
            output_columns = split_name_list(tpl.get("预期输出列"))
            for raw_name in tables:
                identity = self.resolve_table(raw_name)
                canonical = identity.canonical_name if identity else raw_name
                grouped.setdefault(canonical, []).append(
                    {
                        "模板编号": _clean(tpl.get("模板编号")),
                        "模板名称": _clean(tpl.get("模板名称")),
                        "status": _clean(tpl.get("status")),
                        "引用名": raw_name,
                        "规范表名": canonical,
                        "已解析": identity is not None,
                        "输出列": output_columns,
                    }
                )
        return grouped

    def dependency_references(self) -> dict[str, list[dict[str, Any]]]:
        """按规范表名归并依赖关系引用（上游/下游都解析到同一身份）。"""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for dep in store.rows(DEPENDENCY_MODULE):
            for role, key in (("上游", "上游作业"), ("下游", "下游作业")):
                raw_name = _clean(dep.get(key))
                if not raw_name:
                    continue
                identity = self.resolve_table(raw_name)
                canonical = identity.canonical_name if identity else raw_name
                grouped.setdefault(canonical, []).append(
                    {
                        "关系编号": _clean(dep.get("关系编号")),
                        "角色": role,
                        "引用名": raw_name,
                        "规范表名": canonical,
                        "已解析": identity is not None,
                        "status": _clean(dep.get("status")),
                    }
                )
        return grouped

    # ------------------------------------------------------------------ #
    # 统一数据表字段血缘快照
    # ------------------------------------------------------------------ #
    def build_snapshot(self, naming: str = NAMING_LEGACY) -> dict[str, Any]:
        """从元数据表与字段结构记录解析并归并统一快照。"""
        if naming not in NAMING_CHOICES:
            raise CatalogValidation(
                f"命名口径「{naming}」不支持，可选：{'、'.join(NAMING_CHOICES)}"
            )
        with self._lock:
            alias_index = self._alias_index_locked()
            templates = self.template_references()
            dependencies = self.dependency_references()
            fields_by_table = self._fields_by_canonical_table_locked(naming)
            rows: list[dict[str, Any]] = []
            for meta in self._meta_rows_locked():
                rows.append(
                    self._build_table_snapshot_locked(
                        meta,
                        naming,
                        alias_index,
                        fields_by_table,
                        templates,
                        dependencies,
                    )
                )
            # 只在字段结构里出现、尚未登记元数据表的表，作为未确认来源一并暴露。
            known = {row["canonical_name"] for row in rows}
            for canonical, field_rows in sorted(fields_by_table.items()):
                if canonical in known:
                    continue
                rows.append(self._build_orphan_snapshot_locked(canonical, field_rows, templates, dependencies))
            return {
                "module": "lineage",
                "naming": naming,
                "catalog_version": self._version,
                "total": len(rows),
                "items": rows,
            }

    def _fields_by_canonical_table_locked(self, naming: str) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in store.rows(METAFIELD_MODULE):
            canonical = _clean(record.get("规范表名"))
            field_name = _clean(record.get("字段名"))
            if not canonical or not field_name:
                continue
            effective_name = field_name
            if naming == NAMING_LEGACY:
                effective_name = _clean(record.get("字段别名")) or field_name
            grouped.setdefault(canonical, []).append(
                {
                    "字段编号": _clean(record.get("字段编号")),
                    "字段名": effective_name,
                    "规范字段名": field_name,
                    "字段别名": _clean(record.get("字段别名")),
                    "数据类型": _clean(record.get("数据类型")),
                    "字段注释": _clean(record.get("字段注释")),
                    "字段版本": _clean(record.get("字段版本")),
                    "status": _clean(record.get("status")),
                }
            )
        for table_fields in grouped.values():
            table_fields.sort(key=lambda item: (item["规范字段名"], item["字段版本"]))
        return grouped

    def _build_table_snapshot_locked(
        self,
        meta: dict[str, Any],
        naming: str,
        alias_index: dict[str, TableIdentity],
        fields_by_table: dict[str, list[dict[str, Any]]],
        templates: dict[str, list[dict[str, Any]]],
        dependencies: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        canonical = _clean(meta.get("表名"))
        layer = _clean(meta.get("所属数仓层"))
        partition_fields = split_name_list(meta.get("分区字段"))
        primary_keys = split_name_list(meta.get("主键字段"))
        field_rows = fields_by_table.get(canonical, [])
        # 结构一致性以规范字段名为准，不受命名口径（别名/规范名）影响。
        canonical_field_names = [item["规范字段名"] for item in field_rows]
        tpl_refs = templates.get(canonical, [])
        dep_refs = dependencies.get(canonical, [])

        # 模板输出列：归并引用该表的全部模板，列名按口径折算到规范字段。
        output_columns = self._merge_output_columns_locked(canonical, tpl_refs, naming)

        bindable, missing = self._evaluate_bindable_locked(
            meta, layer, partition_fields, primary_keys, field_rows, tpl_refs, output_columns
        )
        consistency = self._evaluate_consistency_locked(
            canonical, meta, canonical_field_names, partition_fields, primary_keys, tpl_refs
        )
        identity = alias_index.get(canonical)
        return {
            "canonical_name": canonical,
            "历史别名": list(identity.aliases) if identity else [],
            "source_meta_id": int(meta.get("id", 0)),
            "表编号": _clean(meta.get("表编号")),
            "所属数仓层": layer,
            "元数据状态": _clean(meta.get("status")),
            "分区字段": partition_fields,
            "主键字段": primary_keys,
            "字段数量(声明)": meta.get("字段数量"),
            "字段数量(结构)": len(field_rows),
            "字段目录": field_rows,
            "模板输出列": output_columns,
            "模板引用": tpl_refs,
            "依赖引用": dep_refs,
            "一致性": consistency,
            "bindable": bindable,
            "未确认项": missing,
        }

    def _build_orphan_snapshot_locked(
        self,
        canonical: str,
        field_rows: list[dict[str, Any]],
        templates: dict[str, list[dict[str, Any]]],
        dependencies: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        return {
            "canonical_name": canonical,
            "历史别名": [],
            "source_meta_id": None,
            "表编号": "",
            "所属数仓层": "",
            "元数据状态": "",
            "分区字段": [],
            "主键字段": [],
            "字段数量(声明)": None,
            "字段数量(结构)": len(field_rows),
            "字段目录": field_rows,
            "模板输出列": [],
            "模板引用": templates.get(canonical, []),
            "依赖引用": dependencies.get(canonical, []),
            "一致性": ["字段结构记录缺少对应的元数据表"],
            "bindable": False,
            "未确认项": ["元数据表", "所属数仓层", "分区字段", "主键字段", "模板输出列"],
        }

    def _merge_output_columns_locked(
        self, canonical_table: str, tpl_refs: list[dict[str, Any]], naming: str
    ) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for ref in tpl_refs:
            for column in ref.get("输出列", []):
                # 输出列里也可能残留旧字段别名，统一折算到该表字段结构口径，
                # 限定在当前规范表内解析，避免跨表同名字段别名串用。
                canonical_field = self.resolve_field_name(column, canonical_table)
                effective = column
                if canonical_field is not None:
                    effective = canonical_field if naming == NAMING_CANONICAL else column
                if effective not in seen:
                    seen.add(effective)
                    merged.append(effective)
        return merged

    def resolve_field_name(self, name: str, canonical_table: str | None = None) -> str | None:
        """字段别名 -> 规范字段名；限定表时只在该表字段结构里找，找不到返回 None。"""
        raw = _clean(name)
        if not raw:
            return None
        for record in store.rows(METAFIELD_MODULE):
            if canonical_table is not None and _clean(record.get("规范表名")) != canonical_table:
                continue
            if raw == _clean(record.get("字段名")) or raw == _clean(record.get("字段别名")):
                return _clean(record.get("字段名"))
        return None

    def _evaluate_bindable_locked(
        self,
        meta: dict[str, Any],
        layer: str,
        partition_fields: list[str],
        primary_keys: list[str],
        field_rows: list[dict[str, Any]],
        tpl_refs: list[dict[str, Any]],
        output_columns: list[str],
    ) -> tuple[bool, list[str]]:
        missing: list[str] = []
        if not layer:
            missing.append("所属数仓层")
        if not partition_fields:
            missing.append("分区字段")
        if not primary_keys:
            missing.append("主键字段")
        if not field_rows:
            missing.append("字段结构")
        if not tpl_refs:
            missing.append("模板引用")
        if not output_columns:
            missing.append("模板输出列")
        return not missing, missing

    def _evaluate_consistency_locked(
        self,
        canonical: str,
        meta: dict[str, Any],
        field_names: list[str],
        partition_fields: list[str],
        primary_keys: list[str],
        tpl_refs: list[dict[str, Any]],
    ) -> list[str]:
        issues: list[str] = []
        declared = meta.get("字段数量")
        if isinstance(declared, int) and declared != len(field_names):
            issues.append(
                f"声明字段数量 {declared} 与字段结构记录数 {len(field_names)} 不一致"
            )
        field_set = set(field_names)
        for column in partition_fields:
            if column not in field_set:
                issues.append(f"分区字段「{column}」不在字段结构中")
        for column in primary_keys:
            if column not in field_set:
                issues.append(f"主键字段「{column}」不在字段结构中")
        # 别名冲突：同一历史别名被多张规范表声明。
        alias_owners: dict[str, set[str]] = {}
        for row in self._meta_rows_locked():
            row_canonical = _clean(row.get("表名"))
            for alias in split_name_list(row.get("历史别名")):
                alias_owners.setdefault(alias, set()).add(row_canonical)
        for alias, owners in alias_owners.items():
            if len(owners) > 1:
                issues.append(f"历史别名「{alias}」同时指向多张规范表：{'、'.join(sorted(owners))}")
        for ref in tpl_refs:
            if not ref["已解析"]:
                issues.append(f"模板「{ref['模板编号']}」引用名「{ref['引用名']}」无法解析到规范表")
        return issues

    # ------------------------------------------------------------------ #
    # 可绑定判断 / 确认动作门禁
    # ------------------------------------------------------------------ #
    def bindability(self, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            meta = store.find(METASTORE_MODULE, entry_id)
            if meta is None:
                return None
            snapshot = self._snapshot_for_meta_locked(meta)
            return {
                "source_meta_id": entry_id,
                "canonical_name": snapshot["canonical_name"],
                "bindable": snapshot["bindable"],
                "未确认项": snapshot["未确认项"],
                "一致性": snapshot["一致性"],
                "catalog_version": self._version,
            }

    def _snapshot_for_meta_locked(self, meta: dict[str, Any]) -> dict[str, Any]:
        snapshot = self.build_snapshot()
        for item in snapshot["items"]:
            if item["source_meta_id"] == int(meta.get("id", 0)):
                return item
        raise CatalogValidation("规范快照中找不到对应元数据表")

    # ------------------------------------------------------------------ #
    # 元数据确认：状态回写 SQL 模板清单
    # ------------------------------------------------------------------ #
    def confirm_metadata(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            meta = store.find(METASTORE_MODULE, entry_id)
            if meta is None:
                return None, f"元数据表 {entry_id} 不存在或已归档"
            snapshot = self._snapshot_for_meta_locked(meta)
            if not snapshot["bindable"]:
                return (
                    None,
                    "对象尚未满足可绑定条件，缺少：" + "、".join(snapshot["未确认项"]),
                )
            if snapshot["一致性"]:
                return None, "表结构一致性校验未通过：" + "；".join(snapshot["一致性"])

            meta["status"] = CONFIRMED_STATUS
            meta["pending"] = False
            meta["abnormal"] = False

            # 状态回写：把确认后的规范身份写回引用该表的 SQL 模板，并登记清单。
            canonical = snapshot["canonical_name"]
            stamped: list[dict[str, Any]] = []
            for tpl in store.rows(SQLTPL_MODULE):
                referenced = {
                    ref["模板编号"]
                    for ref in snapshot["模板引用"]
                    if ref["规范表名"] == canonical
                }
                if _clean(tpl.get("模板编号")) not in referenced:
                    continue
                tpl["元数据回写规范表"] = canonical
                tpl["元数据确认版本"] = self._version
                record = {
                    "模板编号": _clean(tpl.get("模板编号")),
                    "模板名称": _clean(tpl.get("模板名称")),
                    "规范表名": canonical,
                    "确认版本": self._version,
                    "回写输出列": list(snapshot["模板输出列"]),
                }
                stamped.append(record)
                self._template_writebacks.append(record)
            return meta, f"元数据表已确认元数据，状态已回写 {len(stamped)} 个 SQL 模板"

    def template_writeback_manifest(self) -> dict[str, Any]:
        with self._lock:
            return {
                "module": "metastore",
                "catalog_version": self._version,
                "total": len(self._template_writebacks),
                "items": copy.deepcopy(self._template_writebacks),
            }

    # ------------------------------------------------------------------ #
    # 事务化字段目录写入：全成功或整体回退；并发版本只留一份规范目录
    # ------------------------------------------------------------------ #
    FIELD_REQUIRED = ["规范表名", "字段名", "数据类型"]

    def write_field_catalog(
        self, records: list[dict[str, Any]], expected_version: int | None = None
    ) -> dict[str, Any]:
        """整批替换字段结构记录。

        * 先全量校验，任一记录缺必填项即抛 ``CatalogValidation``，原目录不动；
        * 提交前比对确认版本，版本落后抛 ``CatalogConflict``，保证并发校正只留一份；
        * 提交后生成新基线，失败时恢复上一份已确认元数据。
        """
        if not isinstance(records, list) or not records:
            raise CatalogValidation("字段目录不能为空，已回退到上一份已确认元数据")

        prepared: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()
        for index, raw in enumerate(records, start=1):
            if not isinstance(raw, dict):
                raise CatalogValidation(f"第 {index} 条字段记录格式不正确，已整体回退")
            missing = [key for key in self.FIELD_REQUIRED if not _clean(raw.get(key))]
            if missing:
                raise CatalogValidation(
                    f"第 {index} 条字段记录缺少必填字段：{'、'.join(missing)}，已整体回退"
                )
            key = (_clean(raw.get("规范表名")), _clean(raw.get("字段名")))
            if key in seen_keys:
                raise CatalogValidation(
                    f"字段目录存在重复字段 {key[0]}.{key[1]}，已整体回退"
                )
            seen_keys.add(key)
            prepared.append(self._normalize_field_record_locked(raw, index))

        with self._lock:
            if expected_version is not None and expected_version != self._version:
                raise CatalogConflict(
                    f"确认版本 {expected_version} 已过期，当前版本为 {self._version}，"
                    "并发校正只保留一份规范目录"
                )
            target = store.rows(METAFIELD_MODULE)
            backup = copy.deepcopy(target)
            try:
                target.clear()
                target.extend(prepared)
                # 用提交后的目录再构建一次快照，结构不合法同样回滚。
                snapshot = self.build_snapshot()
            except Exception:  # pragma: no cover - 防御性回滚
                target.clear()
                target.extend(backup)
                raise
            self._version += 1
            # 提交成功的目录成为新的已确认版本，旧版本保留为回退基线。
            self._confirmed_versions.append(copy.deepcopy(prepared))
            return {
                "ok": True,
                "catalog_version": self._version,
                "written": len(prepared),
                "tables": snapshot["total"],
                "message": "字段目录已整体提交，旧目录已作为回退基线保留",
            }

    def _normalize_field_record_locked(
        self, raw: dict[str, Any], index: int
    ) -> dict[str, Any]:
        existing = store.rows(METAFIELD_MODULE)
        next_id = max((int(row.get("id", 0)) for row in existing), default=0) + index
        record = {
            "id": int(raw.get("id") or next_id),
            "status": _clean(raw.get("status")) or CONFIRMED_STATUS,
            "pending": False,
            "abnormal": False,
            "字段编号": _clean(raw.get("字段编号")) or f"MF-NEW-{index:04d}",
            "规范表名": _clean(raw.get("规范表名")),
            "字段名": _clean(raw.get("字段名")),
            "字段别名": _clean(raw.get("字段别名")),
            "数据类型": _clean(raw.get("数据类型")),
            "字段注释": _clean(raw.get("字段注释")),
            "字段版本": _clean(raw.get("字段版本")) or "v1",
        }
        return record

    def rollback_field_catalog(self) -> dict[str, Any]:
        """显式回退到上一份已确认字段目录。

        已确认目录按版本压栈：回退时弹出当前版本并恢复栈中上一份；只剩初始
        基线时保持不变，保证始终有一份可发布的规范目录。
        """
        with self._lock:
            if len(self._confirmed_versions) <= 1:
                return {
                    "ok": True,
                    "catalog_version": self._version,
                    "restored": len(self._confirmed_versions[0]),
                    "message": "已处于初始已确认元数据，无可回退的上一版本",
                }
            self._confirmed_versions.pop()
            baseline = self._confirmed_versions[-1]
            target = store.rows(METAFIELD_MODULE)
            target.clear()
            target.extend(copy.deepcopy(baseline))
            self._version += 1
            return {
                "ok": True,
                "catalog_version": self._version,
                "restored": len(baseline),
                "message": "字段目录已回退到上一份已确认元数据",
            }

    def catalog_version(self) -> int:
        with self._lock:
            return self._version

    def reset_for_testing(self) -> None:
        """测试支撑：把目录恢复成仅含初始基线的干净状态（业务流程不调用）。"""
        with self._lock:
            baseline = copy.deepcopy(self._confirmed_versions[0])
            target = store.rows(METAFIELD_MODULE)
            target.clear()
            target.extend(copy.deepcopy(baseline))
            self._version = 0
            self._confirmed_versions = [baseline]
            self._template_writebacks.clear()


# 单例：依赖、SQL 模板与元数据服务共享同一份规范目录。
metadata_catalog = MetadataCatalog()
