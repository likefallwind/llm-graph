"""六类主类型带来的三道确定性闸：定义下限、name_variant 类型闸、is_a 同类型。"""
import sqlite3
import unittest

from kg import pipeline, schema, store
from kg.entity_resolution import _validated_direct_match_type
from kg.observations import definition_is_informative, parse_payload


SOURCE = "回归是能为自变量与因变量之间关系建模的一类方法。分类问题要求预测离散标签。"


def _entity(name, entity_type, definition, evidence):
    return {
        "name": name, "entity_type": entity_type, "definition": definition,
        "aliases": [], "evidence": evidence, "location": "§1",
    }


class DefinitionFloorTests(unittest.TestCase):
    def test_informative_definition_passes(self):
        self.assertTrue(definition_is_informative(
            "回归", "能为自变量与因变量之间关系建模的一类方法"))

    def test_empty_and_short_definitions_are_rejected(self):
        self.assertFalse(definition_is_informative("回归", ""))
        self.assertFalse(definition_is_informative("回归", "一类"))

    def test_definition_repeating_the_name_is_rejected(self):
        self.assertFalse(definition_is_informative("神经网络训练", "神经网络训练"))

    def test_vacuous_head_noun_is_rejected(self):
        """只说了位置没说是什么，判不出任何一格，收进来主类型只能靠猜。"""
        for text in ("深度学习所围绕的核心主题。", "关于数据的实用技能之一。"):
            with self.subTest(text=text):
                self.assertFalse(definition_is_informative("优化", text))

    def test_pointer_clause_is_stripped_not_fatal(self):
        """定位从句常常包着真内容，剥掉从句看剩下的，不要见到「本章」就整条丢。"""
        for name, text in (
                ("神经网络训练",
                 "本章要完整介绍的神经网络训练过程，包含定义架构、数据处理、"
                 "损失函数与训练模型等环节"),
                ("支持向量机", "本章将介绍的四类线性分类模型之一"),
                ("多层神经网络", "由多个层组成的人工神经网络架构，是本书第4章主题。")):
            with self.subTest(name=name):
                self.assertTrue(definition_is_informative(name, text))

    def test_entity_with_weak_definition_is_dropped_at_parse(self):
        batch = parse_payload({
            "entities": [
                _entity("回归", "solution", "能为自变量与因变量之间关系建模的一类方法",
                        "回归是能为自变量与因变量之间关系建模的一类方法"),
                _entity("分类问题", "task", "本章要介绍的核心主题",
                        "分类问题要求预测离散标签"),
            ],
            "claims": [],
        }, SOURCE)
        self.assertEqual(["回归"], [item.name for item in batch.entities])
        self.assertTrue(any("定义不足以判定主类型" in item
                            for item in batch.rejected))


class NameVariantTypeGateTests(unittest.TestCase):
    """NAME_VARIANT_SUFFIXES 剥掉的恰好是类型标记，所以后缀剥离要受类型约束。"""

    def test_same_type_variant_still_takes_the_fast_path(self):
        self.assertEqual("name_variant", _validated_direct_match_type(
            "反向传播算法", "反向传播", "name_variant",
            observed_type="solution", entity_type="solution"))

    def test_cross_type_variant_loses_the_fast_path(self):
        for alias, canonical, observed, existing in (
                ("回归问题", "回归", "task", "solution"),
                ("概率模型", "概率", "solution", "concept"),
                ("交叉熵损失", "交叉熵", "criterion", "concept")):
            with self.subTest(alias=alias):
                self.assertIsNone(_validated_direct_match_type(
                    alias, canonical, "name_variant",
                    observed_type=observed, entity_type=existing))

    def test_gate_is_skipped_when_a_type_is_unknown(self):
        """两个复核队列里别名只有名字没有类型，那里无从比较，不能假装拦得住。"""
        self.assertEqual("name_variant", _validated_direct_match_type(
            "回归问题", "回归", "name_variant"))

    def test_translation_alias_is_unaffected(self):
        self.assertEqual("translation_alias", _validated_direct_match_type(
            "Logistic Function", "逻辑函数", "translation_alias",
            observed_type="concept", entity_type="concept"))


class TaxonomyTypeReportTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def _add(self, name, entity_type):
        return store.add_entity(self.conn, name, entity_type, definition=f"{name} 的定义")

    def test_same_type_is_a_is_not_reported(self):
        child = self._add("交叉熵损失", "criterion")
        parent = self._add("损失函数", "criterion")
        store.add_claim(self.conn, child.id, "is_a", parent.id)

        report = pipeline.taxonomy_type_report(self.conn)

        self.assertEqual(1, report["is_a_claims"])
        self.assertEqual(0, report["type_crossing"])

    def test_cross_type_is_a_is_reported(self):
        child = self._add("线性回归", "solution")
        parent = self._add("回归", "task")
        store.add_claim(self.conn, child.id, "is_a", parent.id)

        report = pipeline.taxonomy_type_report(self.conn)

        self.assertEqual(1, report["type_crossing"])
        self.assertIn("线性回归(solution) is_a 回归(task)",
                      report["items"][0]["edge"])

    def test_other_relations_are_not_checked(self):
        subject = self._add("线性回归", "solution")
        object_ = self._add("回归", "task")
        store.add_claim(self.conn, subject.id, "used_for", object_.id)

        report = pipeline.taxonomy_type_report(self.conn)

        self.assertEqual(0, report["is_a_claims"])
        self.assertEqual(0, report["type_crossing"])


if __name__ == "__main__":
    unittest.main()
