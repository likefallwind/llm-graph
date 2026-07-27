import unittest

from kg.observations import parse_payload
from kg.ontology import registry


SOURCE = "分类是一个研究领域。监督学习是机器学习的一部分。线性代数是学习线性回归的前置知识。"


def _entity(name, entity_type, evidence, definition=""):
    return {
        "name": name,
        "entity_type": entity_type,
        # 定义是判类型的输入，空定义会被 definition_is_informative 挡住，
        # 所以夹具必须给一条能据以判型的定义。
        "definition": definition or f"{name}：{evidence}",
        "aliases": [],
        "evidence": evidence,
        "location": "§1",
    }


def _payload(relation, subject, object_, qualifiers=None):
    entities = {
        "分类": _entity("分类", "concept", "分类是一个研究领域"),
        "研究领域": _entity("研究领域", "concept", "分类是一个研究领域"),
        "监督学习": _entity("监督学习", "solution", "监督学习是机器学习的一部分"),
        "机器学习": _entity("机器学习", "solution", "监督学习是机器学习的一部分"),
        "线性代数": _entity("线性代数", "concept", "线性代数是学习线性回归的前置知识"),
        "线性回归": _entity("线性回归", "solution", "线性代数是学习线性回归的前置知识"),
    }
    return {
        "entities": [entities[subject], entities[object_]],
        "claims": [{
            "subject": subject,
            "relation": relation,
            "object": object_,
            "qualifiers": qualifiers or {},
            "evidence_type": "explicit_prerequisite",
            "evidence": (
                "线性代数是学习线性回归的前置知识"
                if relation == "prerequisite_of"
                else entities[subject]["evidence"]
            ),
            "location": "§1",
        }],
    }


class RelationV1ContractTests(unittest.TestCase):
    def test_default_extraction_has_exactly_three_core_relations(self):
        self.assertEqual(
            {"is_a", "part_of", "prerequisite_of"},
            set(registry().active_relations),
        )

    def test_field_taxonomy_is_absorbed_by_is_a(self):
        batch = parse_payload(
            _payload("is_a", "分类", "研究领域"),
            SOURCE,
        )
        self.assertEqual(1, len(batch.claims))
        self.assertFalse(batch.rejected)

    def test_experimental_relation_is_rejected_at_extraction_boundary(self):
        batch = parse_payload(
            _payload("subfield_of", "分类", "研究领域"),
            SOURCE,
        )
        self.assertFalse(batch.claims)
        self.assertTrue(any("不在默认抽取" in item for item in batch.rejected))

    def test_prerequisite_requires_kind_and_strength(self):
        batch = parse_payload(
            _payload("prerequisite_of", "线性代数", "线性回归"),
            SOURCE,
        )
        self.assertFalse(batch.claims)
        self.assertTrue(any("缺少必填 qualifiers" in item for item in batch.rejected))

    def test_prerequisite_accepts_valid_qualifiers(self):
        batch = parse_payload(
            _payload(
                "prerequisite_of",
                "线性代数",
                "线性回归",
                {"kind": "conceptual", "strength": "required"},
            ),
            SOURCE,
        )
        self.assertEqual(1, len(batch.claims))
        self.assertFalse(batch.rejected)

    def test_prerequisite_rejects_invalid_kind(self):
        batch = parse_payload(
            _payload(
                "prerequisite_of",
                "线性代数",
                "线性回归",
                {"kind": "analogical", "strength": "required"},
            ),
            SOURCE,
        )
        self.assertFalse(batch.claims)
        self.assertTrue(any("值非法" in item for item in batch.rejected))

    def test_unknown_qualifier_is_rejected(self):
        batch = parse_payload(
            _payload(
                "part_of",
                "监督学习",
                "机器学习",
                {"reason": "looks related"},
            ),
            SOURCE,
        )
        self.assertFalse(batch.claims)
        self.assertTrue(any("未知 qualifiers" in item for item in batch.rejected))

    def test_claim_endpoint_matching_ignores_whitespace_only(self):
        payload = _payload("is_a", "线性回归", "机器学习")
        payload["entities"][0]["name"] = "linear regression"
        payload["entities"][0]["evidence"] = "监督学习是机器学习的一部分"
        payload["claims"][0].update({
            "subject": "linearregression",
            "object": "机器学习",
            "evidence": "监督学习是机器学习的一部分",
        })

        batch = parse_payload(payload, SOURCE)

        self.assertEqual(1, len(batch.claims))
        self.assertFalse(batch.rejected)

    def test_claim_can_reference_one_unique_existing_endpoint(self):
        payload = {
            "entities": [
                _entity("线性代数", "concept", "线性代数是学习线性回归的前置知识"),
            ],
            "claims": [{
                "subject": "线性代数",
                "relation": "prerequisite_of",
                "object": "linearregression",
                "qualifiers": {
                    "kind": "conceptual",
                    "strength": "required",
                },
                "evidence_type": "explicit_prerequisite",
                "evidence": "线性代数是学习线性回归的前置知识",
                "location": "§1",
            }],
        }

        batch = parse_payload(payload, SOURCE)

        self.assertEqual(1, len(batch.claims))
        self.assertFalse(batch.rejected)

    def test_claim_with_no_batch_endpoint_is_out_of_scope(self):
        payload = {
            "entities": [],
            "claims": [{
                "subject": "线性代数",
                "relation": "prerequisite_of",
                "object": "线性回归",
                "qualifiers": {
                    "kind": "conceptual",
                    "strength": "required",
                },
                "evidence_type": "explicit_prerequisite",
                "evidence": "线性代数是学习线性回归的前置知识",
                "location": "§1",
            }],
        }

        batch = parse_payload(payload, SOURCE)

        self.assertFalse(batch.claims)
        self.assertTrue(any("至少一个端点" in item for item in batch.rejected))

    def test_unresolved_non_batch_endpoint_is_kept_for_pending_replay(self):
        payload = {
            "entities": [
                _entity("线性代数", "concept", "线性代数是学习线性回归的前置知识"),
            ],
            "claims": [{
                "subject": "线性代数",
                "relation": "prerequisite_of",
                "object": "线性回归",
                "qualifiers": {
                    "kind": "conceptual",
                    "strength": "required",
                },
                "evidence_type": "explicit_prerequisite",
                "evidence": "线性代数是学习线性回归的前置知识",
                "location": "§1",
            }],
        }

        batch = parse_payload(payload, SOURCE)

        self.assertEqual(1, len(batch.claims))
        self.assertFalse(batch.rejected)

class EndpointTypesAreGuidanceNotGateTests(unittest.TestCase):
    """端点类型不再拒收 claim，只标记为非典型。

    主类型表达实体的规范身份，关系问的是它此处承担的角色。拿身份闸角色会拒掉
    「正则化 solves 过拟合」这类成立的说法，而且和"主类型不声称表达全部用途"
    自相矛盾。关系成不成立交给证据和蕴含验证。
    """

    def test_atypical_endpoint_no_longer_raises(self):
        # resource 不在 part_of 的典型主语范围里，但不该因此拒收。
        registry().validate_claim("resource", "part_of", "concept")
        registry().validate_claim_endpoint_types(
            "data", "used_for", "task", active_only=False)

    def test_atypical_endpoint_is_reported(self):
        reasons = registry().atypical_endpoints("resource", "part_of", "concept")

        self.assertEqual(1, len(reasons))
        self.assertIn("resource", reasons[0])

    def test_typical_endpoint_reports_nothing(self):
        self.assertEqual(
            [], registry().atypical_endpoints("solution", "part_of", "concept"))

    def test_unknown_entity_type_still_raises(self):
        with self.assertRaises(ValueError):
            registry().validate_claim("不存在的类型", "part_of", "concept")

    def test_experimental_relation_still_blocked_at_extraction(self):
        with self.assertRaises(ValueError):
            registry().validate_claim_endpoint_types(
                "solution", "used_for", "task", active_only=True)

    def test_typical_endpoints_reach_the_extraction_contract(self):
        """典型端点要进契约给模型看——不进就成了没有消费者的死配置。"""
        import json
        contract = json.loads(registry().extraction_contract())

        self.assertIn("typical_subject_types", contract["part_of"])
        self.assertIn("typical_object_types", contract["part_of"])

if __name__ == "__main__":
    unittest.main()
