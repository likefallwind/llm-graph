import sqlite3
import unittest

from kg import review_queues, schema, store


class EntityRevisionTests(unittest.TestCase):
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

    def _entity(self, name="反向传播", entity_type="concept", definition="旧定义"):
        return store.add_entity(
            self.conn, name, entity_type, definition=definition)

    def test_retype_changes_primary_type_and_records_before(self):
        entity = self._entity()

        result = store.revise_entity(
            self.conn, entity.id, entity_type="solution", reason="是算法不是概念")

        self.assertEqual(
            store.get_entity(self.conn, entity.id).entity_type, "solution")
        self.assertEqual(result["before"], {"entity_type": "concept"})
        self.assertEqual(result["after"], {"entity_type": "solution"})

    def test_definition_is_revisable_together_with_type(self):
        entity = self._entity()

        store.revise_entity(
            self.conn, entity.id, entity_type="solution", definition="新定义",
            reason="定义改了类型跟着改")

        revised = store.get_entity(self.conn, entity.id)
        self.assertEqual(revised.entity_type, "solution")
        self.assertEqual(revised.definition, "新定义")

    def test_revision_is_reversible(self):
        entity = self._entity()
        result = store.revise_entity(
            self.conn, entity.id, entity_type="solution", definition="新定义",
            reason="先改再撤")

        store.revert_revision(self.conn, result["revision_id"])

        restored = store.get_entity(self.conn, entity.id)
        self.assertEqual(restored.entity_type, "concept")
        self.assertEqual(restored.definition, "旧定义")

    def test_cannot_revert_twice(self):
        entity = self._entity()
        result = store.revise_entity(
            self.conn, entity.id, entity_type="solution", reason="改一次")
        store.revert_revision(self.conn, result["revision_id"])

        with self.assertRaises(ValueError):
            store.revert_revision(self.conn, result["revision_id"])

    def test_reason_is_required(self):
        entity = self._entity()

        with self.assertRaises(ValueError):
            store.revise_entity(
                self.conn, entity.id, entity_type="solution", reason="   ")

    def test_unknown_entity_type_is_rejected(self):
        entity = self._entity()

        with self.assertRaises(ValueError):
            store.revise_entity(
                self.conn, entity.id, entity_type="不存在的类型", reason="非法")
        self.assertEqual(
            store.get_entity(self.conn, entity.id).entity_type, "concept")

    def test_definition_cannot_be_emptied(self):
        entity = self._entity()

        with self.assertRaises(ValueError):
            store.revise_entity(
                self.conn, entity.id, definition="  ", reason="清空定义")

    def test_no_op_revision_is_rejected(self):
        entity = self._entity()

        with self.assertRaises(ValueError):
            store.revise_entity(
                self.conn, entity.id, entity_type="concept", definition="旧定义",
                reason="什么也没改")

    def test_merged_entity_cannot_be_revised(self):
        source = self._entity("感知机", "solution")
        target = self._entity("感知器", "solution")
        store.merge_entities(self.conn, source.id, target.id)

        with self.assertRaises(ValueError):
            store.revise_entity(
                self.conn, source.id, entity_type="solution", reason="已合并")

    def test_history_keeps_reverted_revisions(self):
        entity = self._entity()
        first = store.revise_entity(
            self.conn, entity.id, entity_type="solution", reason="第一次")
        store.revert_revision(self.conn, first["revision_id"])
        store.revise_entity(
            self.conn, entity.id, entity_type="task", reason="第二次")

        history = store.entity_revisions(self.conn, entity.id)
        self.assertEqual([item["status"] for item in history],
                         ["applied", "reverted"])
        self.assertEqual([item["reason"] for item in history],
                         ["第二次", "第一次"])

    def test_retype_clears_resolved_conflicts_from_queue(self):
        entity = self._entity()
        run_id = store.create_run(self.conn, "extract", "test-1")
        observation_id = store.add_observation(
            self.conn, run_id, self.snapshot.id, subject_text="反向传播",
            subject_type="solution", excerpt="正文")
        store.add_type_assertion(
            self.conn, entity.id, "solution", observation_id=observation_id,
            status="conflict", reason="观察类型与主类型不一致")
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM entity_type_assertions a"
            " JOIN entities e ON e.id=a.entity_id"
            " WHERE a.status='conflict' AND a.observed_type!=e.entity_type"
        ).fetchone()[0]
        self.assertEqual(pending, 1)

        store.revise_entity(
            self.conn, entity.id, entity_type="solution", reason="观察是对的")

        self.assertEqual(review_queues.review_type_conflicts(self.conn), [])
        # 断言本身是观察史，不因改型而被改写。
        self.assertEqual(self.conn.execute(
            "SELECT status FROM entity_type_assertions").fetchone()["status"],
            "conflict")


if __name__ == "__main__":
    unittest.main()
