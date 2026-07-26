import sqlite3
import unittest

from kg import schema, store


class EntityMergeTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)
        self.source_id = store.upsert_source(
            self.conn, "book", "book", "textbook", independence_group="book:x")
        self.snapshot = store.add_source_snapshot(
            self.conn, self.source_id, "v1", content="正文")

    def tearDown(self):
        self.conn.close()

    def _entity(self, name, entity_type="method"):
        return store.add_entity(self.conn, name, entity_type)

    def test_merge_moves_aliases_evidence_and_claims(self):
        source = self._entity("感知机", "model")
        target = self._entity("感知器", "model")
        other = self._entity("线性分类器", "model")
        store.add_alias(self.conn, source.id, "Perceptron", status="verified")
        store.add_evidence(
            self.conn, self.snapshot.id, "感知机是一种线性分类模型",
            "explicit_definition", entity_id=source.id, mechanically_valid=True)
        claim = store.add_claim(self.conn, source.id, "is_a", other.id)

        result = store.merge_entities(
            self.conn, source.id, target.id, reason="同一概念的不同译名")

        self.assertEqual(len(result["aliases"]), 1)
        self.assertEqual(len(result["evidence"]), 1)
        self.assertEqual(result["claims_subject"], [claim.id])
        moved = store.get_claim(self.conn, claim.id)
        self.assertEqual(moved.subject_id, target.id)
        self.assertEqual(store.get_entity(self.conn, source.id).status, "merged")

    def test_original_canonical_name_survives_as_alias(self):
        source = self._entity("感知机", "model")
        target = self._entity("感知器", "model")

        store.merge_entities(self.conn, source.id, target.id)

        self.assertIn("感知机", store.identity_names(self.conn, target.id))

    def test_duplicate_claim_after_merge_is_rejected_not_crashed(self):
        source = self._entity("感知机", "model")
        target = self._entity("感知器", "model")
        other = self._entity("线性分类器", "model")
        duplicate = store.add_claim(self.conn, source.id, "is_a", other.id)
        store.add_claim(self.conn, target.id, "is_a", other.id)

        result = store.merge_entities(self.conn, source.id, target.id)

        self.assertEqual(result["claims_dropped"], [duplicate.id])
        self.assertEqual(store.get_claim(self.conn, duplicate.id).status, "rejected")

    def test_self_referencing_claim_is_dropped(self):
        source = self._entity("感知机", "model")
        target = self._entity("感知器", "model")
        claim = store.add_claim(self.conn, source.id, "is_a", target.id)

        result = store.merge_entities(self.conn, source.id, target.id)

        self.assertEqual(result["claims_dropped"], [claim.id])

    def test_merge_is_reversible(self):
        source = self._entity("感知机", "model")
        target = self._entity("感知器", "model")
        other = self._entity("线性分类器", "model")
        alias_id = store.add_alias(
            self.conn, source.id, "Perceptron", status="verified")
        claim = store.add_claim(self.conn, source.id, "is_a", other.id)
        result = store.merge_entities(self.conn, source.id, target.id)

        store.revert_merge(self.conn, result["merge_event_id"])

        self.assertEqual(store.get_entity(self.conn, source.id).status, "proposed")
        self.assertEqual(
            store.get_claim(self.conn, claim.id).subject_id, source.id)
        owner = self.conn.execute(
            "SELECT entity_id FROM aliases WHERE id=?", (alias_id,)).fetchone()
        self.assertEqual(owner["entity_id"], source.id)
        self.assertNotIn("感知机", store.identity_names(self.conn, target.id))

    def test_revert_restores_dropped_claims(self):
        source = self._entity("感知机", "model")
        target = self._entity("感知器", "model")
        other = self._entity("线性分类器", "model")
        duplicate = store.add_claim(self.conn, source.id, "is_a", other.id)
        store.add_claim(self.conn, target.id, "is_a", other.id)
        result = store.merge_entities(self.conn, source.id, target.id)

        store.revert_merge(self.conn, result["merge_event_id"])

        self.assertEqual(store.get_claim(self.conn, duplicate.id).status, "proposed")

    def test_cannot_merge_entity_into_itself(self):
        entity = self._entity("感知机", "model")

        with self.assertRaises(ValueError):
            store.merge_entities(self.conn, entity.id, entity.id)

    def test_cannot_revert_twice(self):
        source = self._entity("感知机", "model")
        target = self._entity("感知器", "model")
        result = store.merge_entities(self.conn, source.id, target.id)
        store.revert_merge(self.conn, result["merge_event_id"])

        with self.assertRaises(ValueError):
            store.revert_merge(self.conn, result["merge_event_id"])

    def test_merged_entity_cannot_be_merged_again(self):
        source = self._entity("感知机", "model")
        target = self._entity("感知器", "model")
        third = self._entity("感知网络", "model")
        store.merge_entities(self.conn, source.id, target.id)

        with self.assertRaises(ValueError):
            store.merge_entities(self.conn, source.id, third.id)


if __name__ == "__main__":
    unittest.main()
