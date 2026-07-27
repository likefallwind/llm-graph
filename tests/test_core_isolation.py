"""新旧核心隔离的结构性约束。

旧核心（nodes/edges 及其 CLI）已冻结为只读参照，新核心不得依赖它。这些测试
是机制保证——靠人记得不行，改错了要在测试里立刻失败。
"""
import ast
import pathlib
import re
import unittest

import yaml

from kg.ontology import REQUIRED_RELATION_FIELDS, registry


KG = pathlib.Path(__file__).resolve().parent.parent / "kg"

# 新核心模块：Entity/Claim/Evidence/Decision 这条链路上的全部代码。
NEW_CORE = {
    "models", "store", "schema", "ontology", "coverage", "observations",
    "entity_resolution", "claims", "validators", "decision", "pipeline",
    "review_queues", "targeting", "alias_evidence", "local_corpus",
}

# 旧核心模块：只有显式迁移代码可以碰。
OLD_CORE = {
    "db", "ingest", "dedup", "verify", "expand", "mine", "wikidata",
    "guards", "calibrate", "export", "viz", "seed",
}

# 旧核心独有的表。采集层的 corpus/doc_sections 是新旧共用的，不在此列。
OLD_CORE_TABLES = (
    "nodes", "edges", "node_page", "review_log", "review_signals",
    "ingest_log",
)


def _module_source(name):
    return (KG / f"{name}.py").read_text(encoding="utf-8")


class ModuleIsolationTests(unittest.TestCase):
    def test_new_core_does_not_import_old_core(self):
        offenders = []
        for name in sorted(NEW_CORE):
            tree = ast.parse(_module_source(name))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level:
                    for alias in node.names:
                        if alias.name in OLD_CORE:
                            offenders.append(f"kg/{name}.py -> {alias.name}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        tail = alias.name.rsplit(".", 1)[-1]
                        if alias.name.startswith("kg.") and tail in OLD_CORE:
                            offenders.append(f"kg/{name}.py -> {tail}")
        self.assertEqual(offenders, [], f"新核心不得 import 旧核心: {offenders}")

    def test_new_core_does_not_query_old_core_tables(self):
        offenders = []
        for name in sorted(NEW_CORE):
            source = _module_source(name)
            for table in OLD_CORE_TABLES:
                if re.search(rf"\b(?:FROM|JOIN|INTO|UPDATE)\s+{table}\b",
                             source, re.IGNORECASE):
                    offenders.append(f"kg/{name}.py 查询了 {table}")
        self.assertEqual(offenders, [], f"新核心不得查询旧核心表: {offenders}")


class RegistryIsSingleSourceTests(unittest.TestCase):
    """配置是唯一事实来源：代码里不许再出现平行的硬编码词表。"""

    def test_every_relation_declares_all_required_fields(self):
        for name, policy in registry().relations.items():
            for field in REQUIRED_RELATION_FIELDS:
                self.assertIn(field, policy, f"关系 {name} 缺字段 {field}")

    def test_registry_has_no_unconsumed_fields(self):
        """registry 里出现的每个字段都必须有代码读它。死配置会让人误以为规则生效。"""
        data = yaml.safe_load(
            (KG.parent / "config" / "relation-registry.yaml").read_text(
                encoding="utf-8"))
        declared = set()
        for policy in data["relations"].values():
            declared |= set(policy)
        sources = "\n".join(_module_source(name) for name in sorted(NEW_CORE))
        unconsumed = [
            field for field in sorted(declared)
            if f'"{field}"' not in sources and f"'{field}'" not in sources]
        self.assertEqual(unconsumed, [], f"registry 字段无人消费: {unconsumed}")

    def test_evidence_types_are_not_hardcoded_outside_the_registry(self):
        """证据类型词表只能来自 registry，提示词用生成的，不许再写死一份。"""
        known = set(registry().evidence_type_names())
        # minimum_evidence 的键也以 explicit_ 开头，但它是门槛参数不是证据类型。
        for policy in registry().relations.values():
            known |= set(policy["minimum_evidence"])
        offenders = []
        for name in sorted(NEW_CORE):
            source = _module_source(name)
            found = set(re.findall(r"\bexplicit_[a-z_]+\b", source))
            # 允许在注释和 part_of 的语义说明里提到，但不许拼成可选值列表。
            for literal in found:
                if literal not in known:
                    offenders.append(f"kg/{name}.py 用了未登记的证据类型 {literal}")
            if re.search(r"explicit_[a-z_]+\|explicit_", source):
                offenders.append(f"kg/{name}.py 硬编码了证据类型可选值列表")
        self.assertEqual(offenders, [], str(offenders))

    def test_accepted_evidence_types_are_all_strong(self):
        for name, policy in registry().relations.items():
            for evidence_type in policy["accepted_evidence_types"]:
                self.assertEqual(
                    registry().evidence_types[evidence_type]["strength"], "strong",
                    f"关系 {name} 接受了弱证据 {evidence_type}")

    def test_part_of_does_not_accept_functional_evidence(self):
        # part_of 的 description 明确排除「用于/依赖/参与」，证据类型必须一致。
        self.assertNotIn(
            "explicit_function",
            registry().relation("part_of")["accepted_evidence_types"])


if __name__ == "__main__":
    unittest.main()
