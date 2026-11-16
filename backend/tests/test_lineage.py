"""统一数据表字段血缘目录的核对用例（仅依赖标准库）。

覆盖需求点：
1. 元数据表 + 字段结构记录解析归并成同一份血缘快照；
2. 规范名/历史别名在依赖与 SQL 模板两侧解析到同一表身份；
3. 分区字段、主键、数仓层、模板输出列任一未确认即不可绑定；
4. 元数据确认门禁、确认状态回写 SQL 模板清单；
5. 字段目录事务：全成功或整体回退；并发版本只留一份规范目录；
6. SQL 模板加载快照后校验输出列；
7. 既有模块输入输出保持不变。
"""
from __future__ import annotations

import copy
import unittest

from app.seed import SEED_ROWS
from app.services.lineage import (
    NAMING_CANONICAL,
    NAMING_LEGACY,
    CatalogConflict,
    CatalogValidation,
    metadata_catalog,
)
from app.services.metastore import MetastoreService
from app.services.sqltpl import SqltplService
from app.services.dependency import DependencyService
from app.store import store


def setUpModule():
    """每个测试模块运行前，把受影响的表恢复成种子状态，避免用例间污染。"""
    for module in ("metastore", "metafield", "sqltpl", "dependency"):
        rows = store.rows(module)
        rows.clear()
        rows.extend(copy.deepcopy(SEED_ROWS[module]))
    metadata_catalog.reset_for_testing()


def snapshot_by_name(naming=NAMING_LEGACY):
    snap = metadata_catalog.build_snapshot(naming=naming)
    return {item["canonical_name"]: item for item in snap["items"]}


class LineageSnapshotTests(unittest.TestCase):
    def test_snapshot_merges_metastore_and_field_records(self):
        snap = metadata_catalog.build_snapshot()
        self.assertEqual(snap["module"], "lineage")
        by_name = snapshot_by_name()
        user = by_name["dwd_user_info"]
        # 4 条字段结构记录全部归并到同一张规范表。
        self.assertEqual(user["字段数量(结构)"], 4)
        self.assertEqual(len(user["字段目录"]), 4)
        self.assertEqual(user["所属数仓层"], "DWD")
        self.assertEqual(user["分区字段"], ["ds"])
        self.assertEqual(user["主键字段"], ["user_id"])

    def test_naming_views_change_field_lineage(self):
        legacy = snapshot_by_name(NAMING_LEGACY)["dwd_user_info"]
        canonical = snapshot_by_name(NAMING_CANONICAL)["dwd_user_info"]
        legacy_names = {row["字段名"] for row in legacy["字段目录"]}
        canonical_names = {row["字段名"] for row in canonical["字段目录"]}
        # 旧别名口径：字段名取别名；规范口径：字段名取规范名，但规范字段名始终保留。
        self.assertIn("uid", legacy_names)
        self.assertIn("user_id", canonical_names)
        self.assertTrue(all(row["规范字段名"] for row in legacy["字段目录"]))

    def test_alias_resolves_to_canonical_identity(self):
        identity = metadata_catalog.resolve_table("dim_user_legacy")
        self.assertIsNotNone(identity)
        self.assertEqual(identity.canonical_name, "dwd_user_info")
        self.assertIn("dim_user_legacy", identity.aliases)
        # 规范名自身也解析到同一身份。
        self.assertEqual(
            metadata_catalog.resolve_table("dwd_user_info").canonical_name,
            "dwd_user_info",
        )
        self.assertIsNone(metadata_catalog.resolve_table("not_a_real_table"))


class SharedIdentityTests(unittest.TestCase):
    def test_dependency_and_template_share_one_identity(self):
        # 依赖 DEPE-0004 上游用历史别名、下游用规范名，应解析成同一规范表。
        dep_service = DependencyService()
        resolved = dep_service.resolve_identity(4)
        endpoints = {item["角色"]: item for item in resolved["端点"]}
        self.assertTrue(all(item["已解析"] for item in resolved["端点"]))
        self.assertEqual(
            endpoints["上游"]["表身份"]["canonical_name"],
            endpoints["下游"]["表身份"]["canonical_name"],
            "dwd_user_info",
        )

        # SQL 模板 SQLT-0004 的 FROM 用别名，同样归并到 dwd_user_info。
        tpl_refs = metadata_catalog.template_references()
        self.assertIn("dwd_user_info", tpl_refs)
        ref = next(item for item in tpl_refs["dwd_user_info"] if item["模板编号"] == "SQLT-0004")
        self.assertTrue(ref["已解析"])
        self.assertEqual(ref["引用名"], "dim_user_legacy")

        # 依赖侧归并结果与模板侧使用同一个规范表名。
        dep_groups = metadata_catalog.dependency_references()
        self.assertIn("dwd_user_info", dep_groups)
        self.assertEqual(
            set(tpl_refs) & set(dep_groups) & {"dwd_user_info"},
            {"dwd_user_info"},
        )

    def test_dependency_overview_and_original_fields_unchanged(self):
        overview = DependencyService().lineage_overview()
        self.assertEqual(overview["module"], "dependency")
        # 原始依赖记录的字段没有被解析过程改写。
        dep = store.find("dependency", 4)
        self.assertEqual(dep["上游作业"], "dim_user_legacy")
        self.assertEqual(dep["下游作业"], "dwd_user_info")


class BindabilityTests(unittest.TestCase):
    def test_complete_table_is_bindable(self):
        info = metadata_catalog.bindability(4)
        self.assertTrue(info["bindable"])
        self.assertEqual(info["未确认项"], [])
        self.assertEqual(info["一致性"], [])

    def test_missing_template_output_columns_blocks_binding(self):
        info = metadata_catalog.bindability(5)
        self.assertFalse(info["bindable"])
        self.assertIn("模板输出列", info["未确认项"])

    def test_seed_sample_tables_are_not_bindable(self):
        for entry_id in (1, 2, 3):
            self.assertFalse(metadata_catalog.bindability(entry_id)["bindable"])


class ConfirmationWritebackTests(unittest.TestCase):
    def test_confirm_gated_and_writes_back_templates(self):
        service = MetastoreService()
        before = len(metadata_catalog.template_writeback_manifest()["items"])

        # 未满足可绑定条件的表不能确认。
        entry, message = service.run_action(5, "确认元数据")
        self.assertIsNone(entry)
        self.assertIn("可绑定", message)
        self.assertEqual(store.find("metastore", 5)["status"], "元数据评审中")

        # 满足条件的表确认成功，并把状态回写到引用它的 SQL 模板。
        entry, message = service.run_action(4, "确认元数据")
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["status"], "已确认")
        tpl = store.find("sqltpl", 4)
        self.assertEqual(tpl["元数据回写规范表"], "dwd_user_info")
        self.assertEqual(tpl["元数据确认版本"], 0)

        manifest = metadata_catalog.template_writeback_manifest()
        self.assertEqual(len(manifest["items"]), before + 1)
        stamped = manifest["items"][-1]
        self.assertEqual(stamped["模板编号"], "SQLT-0004")
        self.assertEqual(stamped["规范表名"], "dwd_user_info")
        self.assertEqual(stamped["回写输出列"], ["user_id", "user_name", "mobile", "ds"])

    def test_other_actions_still_follow_status_order(self):
        service = MetastoreService()
        entry, _ = service.run_action(5, "提交元数据评审")
        self.assertEqual(entry["status"], "元数据评审中")
        entry, _ = service.run_action(5, "标记过期")
        self.assertEqual(entry["status"], "已过期")


class TemplateOutputCheckTests(unittest.TestCase):
    def test_published_template_output_columns_validated_against_snapshot(self):
        result = SqltplService().validate_output_columns(4)
        self.assertTrue(result["校验通过"], result)
        self.assertEqual(result["未解析表"], [])
        self.assertEqual(result["缺失输出列"], [])
        table_ref = result["表引用"][0]
        self.assertEqual(table_ref["引用名"], "dim_user_legacy")
        self.assertEqual(table_ref["表身份"]["canonical_name"], "dwd_user_info")
        # 输出列全部命中字段结构（按规范字段名核对）。
        self.assertTrue(all(item["命中字段结构"] for item in result["输出列校验"]))

    def test_draft_template_without_output_columns_fails(self):
        result = SqltplService().validate_output_columns(5)
        self.assertFalse(result["校验通过"])
        self.assertTrue(result["输出列未确认"])
        # 表身份仍能正常解析，只是输出列缺失。
        self.assertEqual(result["未解析表"], [])
        self.assertEqual(result["缺失输出列"], [])
        self.assertEqual(result["输出列校验"], [])


class FieldScopingTests(unittest.TestCase):
    def test_field_alias_resolution_scoped_to_table(self):
        # 两张表都有同名字段别名 dt，但分别指向不同规范字段时不能串用。
        records = copy.deepcopy(SEED_ROWS["metafield"]) + [
            {"id": 100, "status": "已确认", "pending": False, "abnormal": False,
             "字段编号": "MF-X1", "规范表名": "other_table", "字段名": "log_date",
             "字段别名": "dt", "数据类型": "STRING", "字段注释": "日志日期", "字段版本": "v1"}
        ]
        store.rows("metafield").clear()
        store.rows("metafield").extend(records)
        try:
            # other_table 内 dt -> log_date；dwd_user_info 内 dt -> ds。
            self.assertEqual(metadata_catalog.resolve_field_name("dt", "other_table"), "log_date")
            self.assertEqual(metadata_catalog.resolve_field_name("dt", "dwd_user_info"), "ds")
            # 不限制表时仅做兜底匹配，调用方在表级流程里一律显式传表名。
            self.assertIn(metadata_catalog.resolve_field_name("dt"), {"log_date", "ds"})
        finally:
            store.rows("metafield").clear()
            store.rows("metafield").extend(copy.deepcopy(SEED_ROWS["metafield"]))
            metadata_catalog.reset_for_testing()


class FieldCatalogTransactionTests(unittest.TestCase):
    def setUp(self):
        # 每个用例独立起点：干净的版本栈与种子字段目录。
        for module in ("metastore", "metafield", "sqltpl", "dependency"):
            rows = store.rows(module)
            rows.clear()
            rows.extend(copy.deepcopy(SEED_ROWS[module]))
        metadata_catalog.reset_for_testing()
        self.original = copy.deepcopy(store.rows("metafield"))
        self.version_before = metadata_catalog.catalog_version()

    def tearDown(self):
        store.rows("metafield").clear()
        store.rows("metafield").extend(copy.deepcopy(self.original))
        metadata_catalog.reset_for_testing()

    def test_invalid_batch_rolls_back_entirely(self):
        bad_records = [
            {"规范表名": "dwd_user_info", "字段名": "new_col", "数据类型": "STRING"},
            {"规范表名": "dwd_user_info", "字段名": "", "数据类型": "STRING"},
        ]
        with self.assertRaises(CatalogValidation):
            metadata_catalog.write_field_catalog(bad_records)
        # 目录原样保留，版本号不前进。
        self.assertEqual(store.rows("metafield"), self.original)
        self.assertEqual(metadata_catalog.catalog_version(), self.version_before)

    def test_duplicate_field_rejected_and_rolled_back(self):
        dup = [
            {"规范表名": "dwd_user_info", "字段名": "user_id", "数据类型": "BIGINT"},
            {"规范表名": "dwd_user_info", "字段名": "user_id", "数据类型": "BIGINT"},
        ]
        with self.assertRaises(CatalogValidation):
            metadata_catalog.write_field_catalog(dup)
        self.assertEqual(len(store.rows("metafield")), len(self.original))

    def test_valid_batch_commits_and_rollback_restores_baseline(self):
        seed_fields = {row["字段名"] for row in self.original}
        records = [
            {"规范表名": "dwd_user_info", "字段名": "user_id", "字段别名": "uid", "数据类型": "BIGINT"},
            {"规范表名": "dwd_user_info", "字段名": "new_col", "字段别名": "nc", "数据类型": "STRING"},
        ]
        result = metadata_catalog.write_field_catalog(records, expected_version=self.version_before)
        self.assertTrue(result["ok"])
        self.assertEqual(result["written"], 2)
        self.assertEqual(metadata_catalog.catalog_version(), self.version_before + 1)
        self.assertEqual(
            {row["字段名"] for row in store.rows("metafield")},
            {"user_id", "new_col"},
        )

        # 回退到上一份已确认元数据（即提交前的种子基线）。
        rolled = metadata_catalog.rollback_field_catalog()
        self.assertTrue(rolled["ok"])
        self.assertEqual(
            {row["字段名"] for row in store.rows("metafield")},
            seed_fields,
        )

    def test_empty_catalog_rejected(self):
        with self.assertRaises(CatalogValidation):
            metadata_catalog.write_field_catalog([])

    def test_concurrent_correction_leaves_single_catalog(self):
        records = [
            {"规范表名": "dwd_user_info", "字段名": f"col_{i}", "数据类型": "STRING"}
            for i in range(2)
        ]
        # 先成功提交一次，版本前进；再拿旧版本号并发提交必须被拒绝。
        metadata_catalog.write_field_catalog(records, expected_version=self.version_before)
        stale = [
            {"规范表名": "dwd_user_info", "字段名": "stale_col", "数据类型": "STRING"}
        ]
        with self.assertRaises(CatalogConflict):
            metadata_catalog.write_field_catalog(stale, expected_version=self.version_before)
        # 拿当前版本号提交则成功。
        ok = metadata_catalog.write_field_catalog(
            stale, expected_version=metadata_catalog.catalog_version()
        )
        self.assertTrue(ok["ok"])


class ExistingContractTests(unittest.TestCase):
    def test_seed_modules_and_shapes_preserved(self):
        # 既有模块的种子数据一条不少（新增记录只做加法，动作不增删行）。
        for module in ("metastore", "dependency", "sqltpl", "metafield"):
            self.assertEqual(len(store.rows(module)), len(SEED_ROWS[module]))
        # 既有字段集合保持不变（新字段只允许出现在新记录上）。
        original_keys = {
            "表编号", "表名", "所属数仓层", "字段数量", "分区字段",
            "主键字段", "数据量级", "最近更新时间", "id", "status",
            "pending", "abnormal",
        }
        for row in store.rows("metastore")[:3]:
            self.assertTrue(original_keys.issubset(set(row)))

    def test_standard_list_action_contract_unchanged(self):
        # 既有列表与动作接口的返回结构不变。
        items, total = MetastoreService().list_entries(page=1, size=20)
        self.assertEqual(len(items), total)
        self.assertIn("status", items[0])
        dep_entry, _ = DependencyService().run_action(5, "提交关系评审")
        self.assertEqual(dep_entry["status"], "关系评审中")


if __name__ == "__main__":
    unittest.main(verbosity=2)
