import unittest

from kg.observations import parse_payload
from kg.ontology import registry


SOURCE = "分类是一个研究领域。监督学习是机器学习的一部分。线性代数是学习线性回归的前置知识。"


def _entity(name, entity_type, evidence):
    return {
        "name": name,
        "entity_type": entity_type,
        "definition": "",
        "aliases": [],
        "evidence": evidence,
        "location": "§1",
    }


def _payload(relation, subject, object_, qualifiers=None):
    entities = {
        "分类": _entity("分类", "field", "分类是一个研究领域"),
        "研究领域": _entity("研究领域", "field", "分类是一个研究领域"),
        "监督学习": _entity("监督学习", "method", "监督学习是机器学习的一部分"),
        "机器学习": _entity("机器学习", "method", "监督学习是机器学习的一部分"),
        "线性代数": _entity("线性代数", "concept", "线性代数是学习线性回归的前置知识"),
        "线性回归": _entity("线性回归", "method", "线性代数是学习线性回归的前置知识"),
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


if __name__ == "__main__":
    unittest.main()
