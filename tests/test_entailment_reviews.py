import json
import sqlite3
import unittest
from unittest.mock import patch

from kg import decision, pipeline, schema, store, validators


def _evidence(conn, *, entailment="unreviewed"):
    subject = store.add_entity(conn, "review-subject", "solution")
    object_ = store.add_entity(conn, "review-object", "solution")
    claim = store.add_claim(conn, subject.id, "alternative_to", object_.id)
    source_id = store.upsert_source(
        conn, "review-source", "review-source", "textbook",
        independence_group="book:review",
        authority_profile={"alternative_to": "high"})
    snapshot = store.add_source_snapshot(
        conn, source_id, "v1", content="The subject is an alternative to the object.")
    evidence = store.add_evidence(
        conn, snapshot.id, "The subject is an alternative to the object.",
        "explicit_comparison", claim_id=claim.id, mechanically_valid=True,
        entailment=entailment)
    return claim, evidence


class EntailmentReviewHistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_repeated_reviews_keep_history_and_move_current(self):
        _, evidence = _evidence(self.conn)

        first = store.add_entailment_review(
            self.conn, evidence.id, "contradicts", reason="方向反了")
        second = store.add_entailment_review(
            self.conn, evidence.id, "supports", reason="重判后成立")

        history = store.entailment_reviews(self.conn, evidence.id)
        self.assertEqual([item.id for item in history], [first.id, second.id])
        self.assertEqual([item.verdict for item in history],
                         ["contradicts", "supports"])
        row = self.conn.execute(
            "SELECT entailment,current_entailment_review_id FROM evidence WHERE id=?",
            (evidence.id,)).fetchone()
        self.assertEqual(row["entailment"], "supports")
        self.assertEqual(row["current_entailment_review_id"], second.id)

    def test_update_entailment_still_records_a_review(self):
        _, evidence = _evidence(self.conn)

        store.update_entailment(self.conn, evidence.id, "insufficient", reason="材料不足")

        history = store.entailment_reviews(self.conn, evidence.id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].verdict, "insufficient")
        self.assertIsNone(history[0].run_id)

    def test_rejects_unknown_verdict(self):
        _, evidence = _evidence(self.conn)

        with self.assertRaises(ValueError):
            store.add_entailment_review(self.conn, evidence.id, "maybe")

    def test_backfill_gives_legacy_verdict_a_review_without_run(self):
        _, evidence = _evidence(self.conn)
        # 模拟留痕机制之前的库：verdict 直接写在 evidence 上。
        self.conn.execute(
            "UPDATE evidence SET entailment='supports',"
            " current_entailment_review_id=NULL WHERE id=?", (evidence.id,))
        self.conn.commit()

        schema.ensure(self.conn)

        history = store.entailment_reviews(self.conn, evidence.id)
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0].run_id)
        self.assertEqual(history[0].raw_output["provenance"],
                         "pre_versioning_backfill")

    def test_backfill_is_idempotent(self):
        _, evidence = _evidence(self.conn)
        self.conn.execute(
            "UPDATE evidence SET entailment='supports',"
            " current_entailment_review_id=NULL WHERE id=?", (evidence.id,))
        self.conn.commit()

        schema.ensure(self.conn)
        schema.ensure(self.conn)

        self.assertEqual(len(store.entailment_reviews(self.conn, evidence.id)), 1)


class SchemaLedgerTests(unittest.TestCase):
    def test_migration_versions_are_unique_and_registered(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        schema.ensure(conn)

        rows = conn.execute(
            "SELECT version,name FROM schema_migrations ORDER BY version").fetchall()
        names = {row["version"]: row["name"] for row in rows}
        # INSERT OR IGNORE 会让重号静默失败，这里锁住每个号对应的名字。
        self.assertEqual(len(names), len(rows))
        self.assertEqual(names[8], "append_only_entailment_reviews")
        self.assertEqual(names[7], "model_queue_reviews")


class EntailmentRunTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    @patch("kg.validators.llm.chat_json")
    def test_verify_entailment_records_run_with_versions(self, chat_json):
        chat_json.return_value = {"verdict": "supports", "reason": "成立"}
        claim, evidence = _evidence(self.conn)

        validators.verify_entailment(self.conn, claim.id)

        run = self.conn.execute(
            "SELECT * FROM runs WHERE run_type='entailment_verification'").fetchone()
        self.assertIsNotNone(run)
        self.assertEqual(run["algorithm_version"], validators.VALIDATOR_VERSION)
        self.assertEqual(run["prompt_version"], validators.ENTAILMENT_PROMPT_VERSION)
        self.assertEqual(run["status"], "completed")
        review = store.entailment_reviews(self.conn, evidence.id)[0]
        self.assertEqual(review.run_id, run["id"])
        self.assertEqual(review.raw_output["verdict"], "supports")

    @patch("kg.validators.llm.chat_json")
    def test_no_run_created_when_nothing_needs_judging(self, chat_json):
        claim, _ = _evidence(self.conn, entailment="supports")

        validators.verify_entailment(self.conn, claim.id)

        chat_json.assert_not_called()
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE run_type='entailment_verification'"
        ).fetchone()[0])

    @patch("kg.validators.llm.chat_json")
    def test_caller_supplied_run_is_shared_and_left_open(self, chat_json):
        chat_json.return_value = {"verdict": "supports", "reason": "成立"}
        claim, evidence = _evidence(self.conn)
        run_id = validators.create_entailment_run(self.conn)

        validators.verify_entailment(self.conn, claim.id, run_id=run_id)

        self.assertEqual(store.entailment_reviews(self.conn, evidence.id)[0].run_id,
                         run_id)
        status = self.conn.execute(
            "SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"]
        self.assertEqual(status, "running")


class StaleSelectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_legacy_and_unreviewed_evidence_count_as_stale(self):
        claim, evidence = _evidence(self.conn)
        store.update_entailment(self.conn, evidence.id, "supports")

        self.assertEqual(validators.stale_evidence_ids(self.conn, claim.id),
                         {evidence.id})

    @patch("kg.validators.llm.chat_json")
    def test_current_version_evidence_is_not_stale(self, chat_json):
        chat_json.return_value = {"verdict": "supports", "reason": "成立"}
        claim, _ = _evidence(self.conn)

        validators.verify_entailment(self.conn, claim.id)

        self.assertEqual(validators.stale_evidence_ids(self.conn, claim.id), set())

    @patch("kg.validators.llm.chat_json")
    def test_only_stale_skips_current_version_evidence(self, chat_json):
        chat_json.return_value = {"verdict": "supports", "reason": "成立"}
        claim, _ = _evidence(self.conn)
        validators.verify_entailment(self.conn, claim.id)
        chat_json.reset_mock()

        validators.verify_entailment(self.conn, claim.id, only_stale=True)

        chat_json.assert_not_called()

    @patch("kg.validators.llm.chat_json")
    def test_only_stale_rejudges_legacy_evidence(self, chat_json):
        chat_json.return_value = {"verdict": "contradicts", "reason": "重判"}
        claim, evidence = _evidence(self.conn)
        store.update_entailment(self.conn, evidence.id, "supports")

        validators.verify_entailment(self.conn, claim.id, only_stale=True)

        chat_json.assert_called_once()
        history = store.entailment_reviews(self.conn, evidence.id)
        self.assertEqual([item.verdict for item in history],
                         ["supports", "contradicts"])


class BatchConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def _claims(self, count):
        made = []
        source_id = store.upsert_source(
            self.conn, "batch-source", "batch-source", "textbook",
            independence_group="book:batch", authority_profile={})
        snapshot = store.add_source_snapshot(
            self.conn, source_id, "v1", content="batch evidence text")
        for index in range(count):
            subject = store.add_entity(self.conn, f"batch-subject-{index}", "solution")
            object_ = store.add_entity(self.conn, f"batch-object-{index}", "solution")
            claim = store.add_claim(
                self.conn, subject.id, "alternative_to", object_.id)
            store.add_evidence(
                self.conn, snapshot.id, f"batch evidence text {index}",
                "explicit_comparison", claim_id=claim.id, mechanically_valid=True)
            made.append(claim)
        return made

    @patch("kg.validators.llm.pmap")
    def test_all_claims_go_into_one_llm_batch(self, pmap):
        made = self._claims(3)
        pmap.side_effect = lambda fn, items: [
            {"verdict": "supports", "reason": "批量"} for _ in items]

        validators.verify_entailment_batch(
            self.conn, [claim.id for claim in made])

        pmap.assert_called_once()
        # 三条 claim 的 prompt 必须一次性交给 pmap，否则并发度等于 1。
        self.assertEqual(len(pmap.call_args.args[1]), 3)

    def test_pmap_worker_never_touches_the_connection(self):
        made = self._claims(2)
        seen = []

        def fake_pmap(fn, items):
            for item in items:
                # 工作线程里执行 fn；碰 conn 会因 check_same_thread 报错。
                seen.append(fn(item))
            return seen

        with patch("kg.validators.llm.pmap", side_effect=fake_pmap), \
                patch("kg.validators.llm.chat_json") as chat_json:
            chat_json.return_value = {"verdict": "supports", "reason": "ok"}
            validators.verify_entailment_batch(
                self.conn, [claim.id for claim in made])

        # fn 只应产生 LLM 结果，不产生任何数据库副作用。
        self.assertEqual(len(seen), 2)
        self.assertEqual(chat_json.call_count, 2)

    @patch("kg.validators.llm.pmap")
    def test_failed_item_does_not_kill_the_batch(self, pmap):
        made = self._claims(2)
        pmap.side_effect = lambda fn, items: [
            RuntimeError("模型截断"), {"verdict": "supports", "reason": "第二条正常"}]

        lines = validators.verify_entailment_batch(
            self.conn, [claim.id for claim in made])

        self.assertIn("复核失败", lines[0])
        self.assertIn("supports", lines[1])
        first_evidence = store.evidence_for_claim(self.conn, made[0].id)[0]
        second_evidence = store.evidence_for_claim(self.conn, made[1].id)[0]
        self.assertEqual(first_evidence.entailment, "unreviewed")
        self.assertEqual(second_evidence.entailment, "supports")


class DecisionSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_shadow_decision_snapshots_the_review_it_used(self):
        claim, evidence = _evidence(self.conn)
        first = store.add_entailment_review(
            self.conn, evidence.id, "supports", reason="第一次判定")

        decision.shadow_claim(self.conn, claim.id)

        snapshot = json.loads(self.conn.execute(
            "SELECT evidence_review_snapshot FROM decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()["evidence_review_snapshot"])
        self.assertEqual(
            snapshot,
            [{"evidence_id": evidence.id, "entailment_review_id": first.id}])

    def test_old_decision_still_resolves_to_its_original_verdict(self):
        claim, evidence = _evidence(self.conn)
        store.add_entailment_review(self.conn, evidence.id, "supports")
        decision.shadow_claim(self.conn, claim.id)
        store.add_entailment_review(self.conn, evidence.id, "contradicts")
        decision.shadow_claim(self.conn, claim.id)

        rows = self.conn.execute(
            "SELECT evidence_review_snapshot FROM decisions ORDER BY id").fetchall()
        verdicts = []
        for row in rows:
            entry = json.loads(row["evidence_review_snapshot"])[0]
            verdicts.append(self.conn.execute(
                "SELECT verdict FROM entailment_reviews WHERE id=?",
                (entry["entailment_review_id"],)).fetchone()["verdict"])

        self.assertEqual(verdicts, ["supports", "contradicts"])


class ReshadowTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    @patch("kg.validators.llm.chat_json")
    def test_default_mode_makes_no_llm_call(self, chat_json):
        claim, evidence = _evidence(self.conn)
        store.update_entailment(self.conn, evidence.id, "supports")

        result = pipeline.reshadow(self.conn)

        chat_json.assert_not_called()
        self.assertEqual(result["examined_claims"], 1)
        self.assertEqual(result["reverified_evidence"], 0)
        self.assertIsNone(result["entailment_run_id"])
        # 一处高权威语料 + 已判 supports，在门槛 1 下达标；本用例测的是零 LLM。
        self.assertEqual(result["outcomes"], {"auto_approve": 1})
        self.assertEqual(
            1, self.conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE target_id=?",
                (claim.id,)).fetchone()[0])

    @patch("kg.validators.llm.chat_json")
    def test_force_entailment_rejudges_and_shares_one_run(self, chat_json):
        chat_json.return_value = {"verdict": "supports", "reason": "重判"}
        _, evidence = _evidence(self.conn)
        store.update_entailment(self.conn, evidence.id, "contradicts")

        result = pipeline.reshadow(self.conn, force_entailment=True)

        chat_json.assert_called_once()
        self.assertEqual(result["reverified_evidence"], 1)
        review = store.entailment_reviews(self.conn, evidence.id)[-1]
        self.assertEqual(review.run_id, result["entailment_run_id"])
        run = self.conn.execute(
            "SELECT status FROM runs WHERE id=?", (result["entailment_run_id"],)
        ).fetchone()
        self.assertEqual(run["status"], "completed")

    def test_force_and_only_stale_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            pipeline.reshadow(self.conn, force_entailment=True, only_stale=True)


if __name__ == "__main__":
    unittest.main()
