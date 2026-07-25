import sqlite3
import unittest

from kg import schema, store


class IdentityNameTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_identity_is_canonical_plus_verified_aliases_only(self):
        entity = store.add_entity(self.conn, "支持向量机", "model")
        store.add_alias(self.conn, entity.id, "SVM", status="proposed")
        store.add_alias(
            self.conn, entity.id, "Support Vector Machine", status="verified")

        names = store.identity_names(self.conn, entity.id)

        self.assertIn("支持向量机", names)
        self.assertIn("support vector machine", names)
        self.assertNotIn("svm", names)

    def test_rejected_alias_is_not_identity(self):
        entity = store.add_entity(self.conn, "线性回归", "method")
        store.add_alias(self.conn, entity.id, "回归", status="rejected")

        self.assertNotIn("回归", store.identity_names(self.conn, entity.id))

    def test_mention_check_normalizes_both_sides(self):
        names = {store.normalize_name("Softmax 回归")}

        self.assertTrue(store.mentions_identity("我们讨论 softmax回归 的输出", names))
        self.assertTrue(store.mentions_identity("SOFTMAX  回归 很常用", names))
        self.assertFalse(store.mentions_identity("我们讨论线性回归", names))

    def test_proposed_alias_in_text_does_not_count_as_a_mention(self):
        entity = store.add_entity(self.conn, "支持向量机", "model")
        store.add_alias(self.conn, entity.id, "SVM", status="proposed")

        names = store.identity_names(self.conn, entity.id)

        # 这正是 evidence 222 的情形：正文只有 SVM，而 SVM 尚未核实为别名。
        self.assertFalse(store.mentions_identity(
            "我们介绍了线性分类器中两个常用的损失函数：SVM和Softmax", names))

    def test_verifying_the_alias_makes_the_mention_count(self):
        entity = store.add_entity(self.conn, "支持向量机", "model")
        alias_id = store.add_alias(self.conn, entity.id, "SVM", status="proposed")
        store.set_alias_status(self.conn, alias_id, "verified")

        self.assertTrue(store.mentions_identity(
            "线性分类器中两个常用的损失函数：SVM和Softmax",
            store.identity_names(self.conn, entity.id)))

    def test_endpoint_mentions_flags_the_missing_side(self):
        subject = store.add_entity(self.conn, "向量", "concept")
        object_ = store.add_entity(self.conn, "矩阵", "concept")
        claim = store.add_claim(self.conn, subject.id, "part_of", object_.id)
        source_id = store.upsert_source(
            self.conn, "cs229", "cs229", "textbook", independence_group="book:cs229")
        snapshot = store.add_source_snapshot(
            self.conn, source_id, "v1", content="定义设计矩阵 X 为 n×d 的矩阵。")
        evidence = store.add_evidence(
            self.conn, snapshot.id, "定义设计矩阵 X 为 n×d 的矩阵。",
            "explicit_composition", claim_id=claim.id, mechanically_valid=True)

        row = self.conn.execute(
            "SELECT * FROM evidence WHERE id=?", (evidence.id,)).fetchone()
        mentions = store.evidence_endpoint_mentions(self.conn, row)

        self.assertFalse(mentions["subject"])
        self.assertTrue(mentions["object"])


if __name__ == "__main__":
    unittest.main()
