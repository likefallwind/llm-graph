import sqlite3
import unittest
from unittest.mock import patch

from kg import claims, entity_resolution, pipeline, review_queues, schema, store
from kg.observations import (
    ClaimObservation,
    EntityObservation,
    ObservationBatch,
)


def observation(name: str, entity_type: str = "concept") -> EntityObservation:
    return EntityObservation(
        name=name,
        entity_type=entity_type,
        definition=f"{name} 的定义",
        aliases=(),
        evidence=f"教材明确讨论了{name}。",
        location="§1",
        raw={},
    )


class EntityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)

    def tearDown(self):
        self.conn.close()

    def add_snapshot(self, slug: str, group: str):
        source_id = store.upsert_source(
            self.conn, slug, slug, "textbook",
            independence_group=group)
        return store.add_source_snapshot(
            self.conn, source_id, "v1", content=f"{slug} content")

    def test_canonical_exact_has_priority_over_alias(self):
        canonical = store.add_entity(self.conn, "机器学习", "concept")
        other = store.add_entity(self.conn, "机器学习方法", "solution")
        with self.assertRaisesRegex(ValueError, "规范名冲突"):
            store.add_alias(
                self.conn, other.id, "机器学习", status="verified")

        result = entity_resolution.resolve(
            self.conn, observation(" 机器学习 ", "concept"))

        self.assertEqual(canonical.id, result.entity_id)
        self.assertEqual("canonical_exact", result.matched_by)

    def test_verified_alias_is_reused_without_llm(self):
        entity = store.add_entity(self.conn, "监督学习", "solution")
        store.add_alias(
            self.conn, entity.id, "Supervised Learning",
            language="en", status="verified")

        def fail_if_called(_observation, _candidates):
            self.fail("verified alias 命中时不应调用 LLM")

        result = entity_resolution.resolve(
            self.conn, observation("supervised learning", "solution"),
            llm_normalizer=fail_if_called)

        self.assertEqual(entity.id, result.entity_id)
        self.assertEqual("verified_alias_exact", result.matched_by)

    def test_whitespace_reference_catalog_keeps_competing_entities_ambiguous(self):
        first = store.add_entity(self.conn, "li nearregression", "solution")
        second = store.add_entity(self.conn, "linear regression", "solution")

        hits = store.find_reference_entities(self.conn, "linearreg ression")

        self.assertEqual({first.id, second.id}, {item.id for item in hits})
        # 同一个 reference_key 下有两个实体，目录必须把它们都留着表示歧义，
        # 不能塌成一个。
        self.assertEqual(2, len(store.identity_catalog(self.conn)[
            store.reference_key("linearregression")]))

    def test_proposed_alias_does_not_auto_match(self):
        entity = store.add_entity(self.conn, "监督学习", "solution")
        store.add_alias(
            self.conn, entity.id, "Supervised Learning",
            language="en", status="proposed")

        result = entity_resolution.resolve(
            self.conn, observation("Supervised Learning", "solution"),
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "ambiguous",
                "canonical_name": "",
                "confidence": 0.4,
                "reason": "证据不足",
            })

        self.assertIsNone(result.entity_id)
        self.assertEqual("llm_ambiguous", result.matched_by)

    def test_exact_name_type_conflict_reuses_entity_and_records_assertion(self):
        entity = store.add_entity(self.conn, "预测", "task")

        result = entity_resolution.resolve(
            self.conn, observation("预测", "concept"),
            source_snapshot_id=None, observation_id=None)

        self.assertEqual(entity.id, result.entity_id)
        self.assertEqual("type_conflict", result.outcome)
        assertion = self.conn.execute(
            "SELECT * FROM entity_type_assertions WHERE entity_id=?",
            (entity.id,)).fetchone()
        self.assertEqual("conflict", assertion["status"])
        self.assertEqual("concept", assertion["observed_type"])

    def test_llm_canonicalization_can_reuse_existing_entity(self):
        entity = store.add_entity(self.conn, "监督学习", "solution")
        snapshot = self.add_snapshot("book-a", "book:a")

        result = entity_resolution.resolve(
            self.conn, observation("Supervised Learning", "solution"),
            source_snapshot_id=snapshot.id,
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing",
                "candidate_id": None,
                "canonical_name": "监督学习",
                "proposed_alias": "Supervised Learning",
                "match_type": "translation_alias",
                "confidence": 0.98,
                "reason": "中英文同义术语",
            })

        self.assertEqual(entity.id, result.entity_id)
        self.assertEqual("llm_translation_alias", result.matched_by)
        alias = self.conn.execute(
            "SELECT status FROM aliases WHERE entity_id=? AND normalized_name=?",
            (entity.id, store.normalize_name("Supervised Learning"))).fetchone()
        self.assertEqual("verified", alias["status"])
        self.assertEqual(
            [entity], store.find_verified_alias_entities(
                self.conn, "Supervised Learning"))
        candidate = self.conn.execute(
            "SELECT * FROM entity_alignment_candidates").fetchone()
        self.assertEqual("suspected_same_entity", candidate["relation"])
        self.assertEqual(1, candidate["independent_sources"])

    def test_high_confidence_translation_is_immediately_verified(self):
        entity = store.add_entity(self.conn, "二分类", "task")
        snapshot = self.add_snapshot("book-a", "book:a")

        result = entity_resolution.resolve(
            self.conn, observation("Binary classification", "task"),
            source_snapshot_id=snapshot.id,
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing",
                "candidate_id": entity.id,
                "canonical_name": "二分类",
                "match_type": "translation_alias",
                "confidence": 0.98,
                "reason": "英文术语的直接中文翻译",
            })

        self.assertEqual(entity.id, result.entity_id)
        self.assertEqual("llm_translation_alias", result.matched_by)
        alias = self.conn.execute(
            "SELECT status,alias_type FROM aliases WHERE entity_id=?",
            (entity.id,)).fetchone()
        self.assertEqual("verified", alias["status"])
        self.assertEqual("translation_alias", alias["alias_type"])
        candidate = self.conn.execute(
            "SELECT status FROM entity_alignment_candidates").fetchone()
        self.assertEqual("verified", candidate["status"])

    def test_high_confidence_abbreviation_links_contextually_but_alias_stays_proposed(self):
        """缩写在本章语境里指得清楚，不代表「CE」这个字符串全局归它。

        本次 mention 落地，全局别名仍要靠跨语境稳定性挣。两个门槛分开正是
        contextual_same_entity 存在的理由。
        """
        entity = store.add_entity(self.conn, "交叉熵损失", "criterion")
        snapshot = self.add_snapshot("book-a", "book:a")

        result = entity_resolution.resolve(
            self.conn, observation("CE", "criterion"),
            source_snapshot_id=snapshot.id,
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing",
                "candidate_id": entity.id,
                "canonical_name": "交叉熵损失",
                "match_type": "abbreviation",
                "confidence": 0.99,
                "reason": "可能的缩写",
            })

        self.assertEqual(entity.id, result.entity_id)
        self.assertEqual("contextual_same_entity", result.outcome)
        alias = self.conn.execute(
            "SELECT status,alias_type FROM aliases WHERE entity_id=?",
            (entity.id,)).fetchone()
        self.assertEqual("proposed", alias["status"])
        self.assertEqual("abbreviation", alias["alias_type"])
        candidate = self.conn.execute(
            "SELECT status FROM entity_alignment_candidates").fetchone()
        self.assertEqual("suspected", candidate["status"])

    def test_semantic_prefix_cannot_masquerade_as_name_variant(self):
        """限定词不是类别后缀，剥掉它可能换掉一个知识对象，必须先反向复核。"""
        entity = store.add_entity(self.conn, "交叉熵损失", "criterion")
        snapshot = self.add_snapshot("book-a", "book:a")

        result = entity_resolution.resolve(
            self.conn, observation("softmax-交叉熵损失", "criterion"),
            source_snapshot_id=snapshot.id,
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing",
                "candidate_id": entity.id,
                "canonical_name": "交叉熵损失",
                "match_type": "name_variant",
                "confidence": 0.99,
                "reason": "模型误认为只是名称变化",
            },
            llm_separation_verifier=lambda _observation, _candidate: {
                "verdict": "should_stay_separate",
                "confidence": 0.9,
                "reason": "softmax 前缀限定了求损失的输出层，不是同一个判据",
            })

        self.assertIsNone(result.entity_id)
        self.assertEqual("ambiguous", result.outcome)
        self.assertEqual("separation_objection", result.matched_by)
        alias = self.conn.execute(
            "SELECT status FROM aliases WHERE entity_id=?",
            (entity.id,)).fetchone()
        # 被反向复核否决的一轮不留别名，也不记对齐证据。
        self.assertIsNone(alias)
        self.assertIsNone(self.conn.execute(
            "SELECT status FROM entity_alignment_candidates").fetchone())

    def test_chinese_suffix_is_validated_as_name_variant(self):
        entity = store.add_entity(self.conn, "分类问题", "task")
        snapshot = self.add_snapshot("book-a", "book:a")

        result = entity_resolution.resolve(
            self.conn, observation("分类", "task"),
            source_snapshot_id=snapshot.id,
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing",
                "candidate_id": entity.id,
                "canonical_name": "分类问题",
                "match_type": "name_variant",
                "confidence": 0.98,
                "reason": "只省略问题后缀",
            })

        self.assertEqual(entity.id, result.entity_id)
        alias = self.conn.execute(
            "SELECT status,alias_type FROM aliases WHERE entity_id=?",
            (entity.id,)).fetchone()
        self.assertEqual("verified", alias["status"])
        self.assertEqual("name_variant", alias["alias_type"])

    def test_new_entity_is_created_above_the_creation_threshold(self):
        # 只有达到统一的自动执行门槛，才允许创建 proposed 实体。
        result = entity_resolution.resolve(
            self.conn, observation("全新术语"),
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "new",
                "canonical_name": "全新术语",
                "confidence": 0.95,
                "reason": "确定是新概念",
            })

        self.assertIsNotNone(result.entity_id)
        created = store.find_canonical_entity(self.conn, "全新术语")
        self.assertEqual(created.status, "proposed")
        self.assertFalse(created.metadata["below_auto_link_confidence"])

    def test_new_entity_below_creation_threshold_needs_review(self):
        result = entity_resolution.resolve(
            self.conn, observation("全新术语"),
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "new",
                "canonical_name": "全新术语",
                "confidence": 0.92,
                "reason": "像是新概念，但不足以自动执行",
            })

        self.assertIsNone(result.entity_id)
        self.assertEqual("below_confidence", result.outcome)
        self.assertIsNone(store.find_canonical_entity(self.conn, "全新术语"))

    def test_invalid_existing_response_is_not_semantic_ambiguity(self):
        result = entity_resolution.resolve(
            self.conn, observation("未知术语"),
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing",
                "candidate_id": 999,
                "canonical_name": "不存在的候选",
                "confidence": 0.99,
                "reason": "错误地引用候选",
            })

        self.assertIsNone(result.entity_id)
        self.assertEqual("invalid_response", result.outcome)
        self.assertEqual("llm_invalid_response", result.matched_by)

    def test_low_confidence_llm_does_not_merge(self):
        existing = store.add_entity(self.conn, "监督学习", "solution")

        result = entity_resolution.resolve(
            self.conn, observation("有监督学习", "solution"),
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing",
                "candidate_id": existing.id,
                "canonical_name": "监督学习",
                "confidence": 0.5,
                "reason": "拿不准",
            })

        self.assertIsNone(result.entity_id)

    def test_llm_ambiguous_does_not_merge_despite_existing_canonical_name(self):
        store.add_entity(self.conn, "监督学习", "solution")

        result = entity_resolution.resolve(
            self.conn, observation("Supervised Learning", "solution"),
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "ambiguous",
                "canonical_name": "监督学习",
                "confidence": 0.99,
                "reason": "上下文不足",
            })

        self.assertIsNone(result.entity_id)
        self.assertEqual("llm_ambiguous", result.matched_by)

    def test_ambiguous_verified_alias_can_be_disambiguated_by_llm(self):
        """一个字符串同时是两个实体的 verified alias 时，只有语境能选。

        这正是语境链接该干的活：本次 mention 归其中一个，而「SG」这个名字仍然
        歧义——两个候选互相竞争，谁也拿不到全局别名。
        """
        first = store.add_entity(self.conn, "梯度方法", "solution")
        second = store.add_entity(self.conn, "统计梯度", "concept")
        store.add_alias(self.conn, first.id, "SG", status="verified")
        store.add_alias(self.conn, second.id, "SG", status="verified")

        result = entity_resolution.resolve(
            self.conn, observation("SG", "solution"),
            llm_normalizer=lambda _observation, candidates: {
                "decision": "existing",
                "candidate_id": first.id,
                "canonical_name": "梯度方法",
                "confidence": 0.99,
                "reason": f"在 {len(candidates)} 个同名候选中结合上下文选择",
            })

        self.assertEqual(first.id, result.entity_id)
        self.assertEqual("contextual_same_entity", result.outcome)
        candidate = self.conn.execute(
            "SELECT status FROM entity_alignment_candidates"
            " WHERE entity_id=?", (first.id,)).fetchone()
        self.assertEqual("suspected", candidate["status"])

    def test_high_confidence_llm_new_creates_canonical_and_proposed_alias(self):
        result = entity_resolution.resolve(
            self.conn, observation("RLHF", "solution"),
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "new",
                "canonical_name": "基于人类反馈的强化学习",
                "proposed_alias": "RLHF",
                "confidence": 0.99,
                "reason": "规范为中文全称",
            })

        entity = store.get_entity(self.conn, result.entity_id)
        self.assertEqual("基于人类反馈的强化学习", entity.canonical_name)
        alias = self.conn.execute(
            "SELECT status FROM aliases WHERE entity_id=?",
            (entity.id,)).fetchone()
        self.assertEqual("proposed", alias["status"])

    def test_medium_confidence_existing_match_is_saved_as_suspected(self):
        entity = store.add_entity(self.conn, "分类问题", "task")
        snapshot = self.add_snapshot("book-a", "book:a")

        result = entity_resolution.resolve(
            self.conn, observation("分类", "concept"),
            source_snapshot_id=snapshot.id,
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing",
                "candidate_id": entity.id,
                "canonical_name": "分类问题",
                "confidence": 0.82,
                "reason": "定义一致但类型观察有冲突",
            })

        self.assertIsNone(result.entity_id)
        self.assertEqual("suspected_same_entity", result.outcome)
        self.assertEqual(entity.id, result.selected_candidate_id)
        candidate = self.conn.execute(
            "SELECT * FROM entity_alignment_candidates").fetchone()
        self.assertEqual("suspected", candidate["status"])
        self.assertAlmostEqual(0.82, candidate["score"])

    def test_two_independent_suspicions_promote_verified_alias(self):
        entity = store.add_entity(self.conn, "分类问题", "task")
        first = self.add_snapshot("book-a", "book:a")
        second = self.add_snapshot("book-b", "book:b")

        def same_entity(_observation, _candidates):
            return {
                "decision": "existing",
                "candidate_id": entity.id,
                "canonical_name": "分类问题",
                "confidence": 0.82,
                "reason": "定义一致",
            }

        first_result = entity_resolution.resolve(
            self.conn, observation("分类", "task"),
            source_snapshot_id=first.id, llm_normalizer=same_entity)
        second_result = entity_resolution.resolve(
            self.conn, observation("分类", "task"),
            source_snapshot_id=second.id, llm_normalizer=same_entity)

        self.assertEqual("suspected_same_entity", first_result.outcome)
        self.assertEqual(entity.id, second_result.entity_id)
        self.assertEqual("accumulated_alignment", second_result.matched_by)
        candidate = self.conn.execute(
            "SELECT * FROM entity_alignment_candidates").fetchone()
        self.assertEqual("verified", candidate["status"])
        self.assertEqual(2, candidate["independent_sources"])
        self.assertGreater(candidate["score"], 0.95)
        alias = self.conn.execute(
            "SELECT status FROM aliases WHERE entity_id=?"
            " AND normalized_name=?",
            (entity.id, store.normalize_name("分类"))).fetchone()
        self.assertEqual("verified", alias["status"])

    def test_same_source_group_does_not_promote_suspected_alias(self):
        entity = store.add_entity(self.conn, "分类问题", "task")
        first = self.add_snapshot("edition-a", "book:shared")
        second = self.add_snapshot("edition-b", "book:shared")
        response = lambda _observation, _candidates: {
            "decision": "existing",
            "candidate_id": entity.id,
            "canonical_name": "分类问题",
            "confidence": 0.90,
            "reason": "定义一致",
        }

        entity_resolution.resolve(
            self.conn, observation("分类", "task"),
            source_snapshot_id=first.id, llm_normalizer=response)
        result = entity_resolution.resolve(
            self.conn, observation("分类", "task"),
            source_snapshot_id=second.id, llm_normalizer=response)

        self.assertIsNone(result.entity_id)
        candidate = self.conn.execute(
            "SELECT * FROM entity_alignment_candidates").fetchone()
        self.assertEqual("suspected", candidate["status"])
        self.assertEqual(1, candidate["independent_sources"])

    def test_alias_identical_to_canonical_is_not_stored(self):
        entity = store.add_entity(self.conn, "牛顿法", "solution")

        alias_id = store.add_alias(
            self.conn, entity.id, " 牛顿法 ", status="proposed")

        self.assertIsNone(alias_id)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM aliases WHERE entity_id=?",
            (entity.id,)).fetchone()[0]
        self.assertEqual(0, count)


class ExistingSchemaMigrationTests(unittest.TestCase):
    def test_existing_aliases_are_migrated_as_verified(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL
            );
            CREATE TABLE entities (
                id INTEGER PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE,
                entity_type TEXT NOT NULL,
                definition TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'proposed',
                embedding TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE aliases (
                id INTEGER PRIMARY KEY,
                entity_id INTEGER NOT NULL REFERENCES entities(id),
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT '',
                alias_type TEXT NOT NULL DEFAULT 'alias',
                source_snapshot_id INTEGER,
                created_at REAL NOT NULL,
                UNIQUE(entity_id, normalized_name, language)
            );
            INSERT INTO entities VALUES
                (1,'监督学习','监督学习','solution','','proposed',NULL,'{}',0,0);
            INSERT INTO aliases VALUES
                (1,1,'Supervised Learning','supervised learning','en','alias',NULL,0);
        """)

        schema.ensure(conn)

        row = conn.execute("SELECT status FROM aliases WHERE id=1").fetchone()
        self.assertEqual("verified", row["status"])
        migration = conn.execute(
            "SELECT name FROM schema_migrations WHERE version=5").fetchone()
        self.assertEqual("auditable_entity_resolution", migration["name"])
        conn.close()


class SuspectedMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)
        source_id = store.upsert_source(
            self.conn, "book-a", "book-a", "textbook",
            independence_group="book:a")
        self.snapshot = store.add_source_snapshot(
            self.conn, source_id, "v1", content="分类材料")
        self.run_id = store.create_run(
            self.conn, "test", "test-version")
        store.add_entity(self.conn, "分类问题", "task")
        store.add_entity(self.conn, "二分类", "task")

    def tearDown(self):
        self.conn.close()

    def test_suspected_entity_and_dependent_claim_remain_pending(self):
        batch = ObservationBatch(
            entities=(
                observation("分类", "task"),
                observation("二分类", "task"),
            ),
            claims=(
                ClaimObservation(
                    subject="二分类",
                    relation="is_a",
                    object="分类",
                    qualifiers={},
                    evidence_type="explicit_taxonomy",
                    evidence="二分类属于分类。",
                    location="§1",
                    raw={},
                ),
            ),
            next_reading_targets=(),
            rejected=(),
        )

        with patch(
                "kg.entity_resolution._llm_normalize",
                return_value={
                    "decision": "existing",
                    "candidate_id": 1,
                    "canonical_name": "分类问题",
                    "confidence": 0.82,
                    "reason": "定义一致",
                }):
            result = claims.materialize(
                self.conn, batch, source_snapshot_id=self.snapshot.id,
                run_id=self.run_id)

        self.assertEqual((), result.claim_ids)
        statuses = {
            row["subject_text"]: row["status"]
            for row in self.conn.execute(
                "SELECT subject_text,status FROM observations"
                " WHERE run_id=?", (self.run_id,))
        }
        self.assertEqual("pending", statuses["分类"])
        claim_status = self.conn.execute(
            "SELECT status FROM observations WHERE run_id=? AND relation='is_a'",
            (self.run_id,)).fetchone()["status"]
        self.assertEqual("pending", claim_status)

    def test_resolver_call_failure_leaves_the_observation_pending(self):
        """调用失败不是结论，观察必须留给 replay，不能进 rejected 终态。"""
        batch = ObservationBatch(
            entities=(observation("感知机", "concept"),),
            claims=(), next_reading_targets=(), rejected=())

        with patch(
                "kg.entity_resolution._llm_normalize",
                side_effect=RuntimeError("连接被重置")):
            result = claims.materialize(
                self.conn, batch, source_snapshot_id=self.snapshot.id,
                run_id=self.run_id)

        self.assertEqual((), result.entity_ids)
        self.assertEqual("pending", self.conn.execute(
            "SELECT status FROM observations WHERE subject_text='感知机'",
        ).fetchone()["status"])
        self.assertEqual("resolver_error", self.conn.execute(
            "SELECT outcome FROM entity_resolution_events WHERE raw_name='感知机'",
        ).fetchone()["outcome"])

    def test_explicit_ambiguity_leaves_the_observation_pending(self):
        batch = ObservationBatch(
            entities=(observation("感知机", "concept"),),
            claims=(), next_reading_targets=(), rejected=())

        with patch(
                "kg.entity_resolution._llm_normalize",
                return_value={
                    "decision": "ambiguous",
                    "canonical_name": "",
                    "confidence": 0.6,
                    "reason": "上下文不足",
                }):
            claims.materialize(
                self.conn, batch, source_snapshot_id=self.snapshot.id,
                run_id=self.run_id)

        self.assertEqual("pending", self.conn.execute(
            "SELECT status FROM observations WHERE subject_text='感知机'",
        ).fetchone()["status"])

    def test_resolver_failure_does_not_block_endpoints_from_landing(self):
        """失败的名字不进 suspected：既有实体的精确匹配和这次失败无关。"""
        store.add_entity(self.conn, "感知机", "concept")
        batch = ObservationBatch(
            entities=(observation("感知机", "concept"),),
            claims=(
                ClaimObservation(
                    subject="二分类", relation="is_a", object="感知机",
                    qualifiers={}, evidence_type="explicit_taxonomy",
                    evidence="二分类属于感知机。", location="§1", raw={}),
            ),
            next_reading_targets=(), rejected=())

        with patch(
                "kg.entity_resolution._llm_normalize",
                side_effect=RuntimeError("连接被重置")):
            result = claims.materialize(
                self.conn, batch, source_snapshot_id=self.snapshot.id,
                run_id=self.run_id)

        self.assertEqual(1, len(result.claim_ids))

    def test_replayed_low_confidence_observation_remains_pending(self):
        """置信度不足不是 observation 无效，重放后仍应等待复核。"""
        store.add_entity(self.conn, "感知器", "concept")
        observation_id = store.add_observation(
            self.conn, self.run_id, self.snapshot.id,
            subject_text="感知机", subject_type="concept",
            excerpt="教材明确讨论了感知机。",
            payload={"name": "感知机", "entity_type": "concept", "aliases": []})

        with patch(
                "kg.entity_resolution._llm_normalize",
                return_value={
                    "decision": "existing", "candidate_id": 1,
                    "canonical_name": "感知器", "confidence": 0.4,
                    "reason": "不确定",
                }):
            report = claims.replay_pending(self.conn)

        self.assertEqual([observation_id], report["still_pending"])
        self.assertEqual([observation_id], report["needs_review"])
        self.assertEqual([], report["rejected"])
        self.assertEqual("pending", self.conn.execute(
            "SELECT status FROM observations WHERE id=?",
            (observation_id,)).fetchone()["status"])

    def test_same_resolver_does_not_repeat_needs_review_llm_call(self):
        observation_id = store.add_observation(
            self.conn, self.run_id, self.snapshot.id,
            subject_text="软间隔", subject_type="concept",
            excerpt="教材明确讨论了软间隔。",
            payload={"name": "软间隔", "entity_type": "concept", "aliases": []})

        with patch(
                "kg.entity_resolution._llm_normalize",
                return_value={
                    "decision": "new", "canonical_name": "软间隔",
                    "confidence": 0.92, "reason": "应当新建",
                }) as normalizer:
            first = claims.replay_pending(self.conn)
            second = claims.replay_pending(self.conn)

        self.assertEqual(1, normalizer.call_count)
        self.assertEqual([observation_id], first["needs_review"])
        self.assertEqual([observation_id], second["needs_review"])

    def test_policy_migration_reopens_legacy_ambiguous_observation(self):
        observation_id = store.add_observation(
            self.conn, self.run_id, self.snapshot.id,
            subject_text="软间隔", subject_type="concept",
            excerpt="教材明确讨论了软间隔。",
            payload={"name": "软间隔", "entity_type": "concept", "aliases": []})
        store.resolve_observation(self.conn, observation_id, False)
        store.add_resolution_event(
            self.conn, raw_name="软间隔", deterministic_name="软间隔",
            entity_id=None, outcome="ambiguous", matched_by="llm_ambiguous",
            candidate_ids=[], confidence=0.92, reason="旧策略门槛不足",
            resolver_version="entity-resolver-5",
            source_snapshot_id=self.snapshot.id, observation_id=observation_id)
        self.conn.execute("DELETE FROM schema_migrations WHERE version=11")
        self.conn.commit()

        schema.ensure(self.conn)

        self.assertEqual("pending", self.conn.execute(
            "SELECT status FROM observations WHERE id=?",
            (observation_id,)).fetchone()["status"])


class EndpointFallbackTests(unittest.TestCase):
    """端点落不到本批 resolved 时，退回库里已有实体的确定性精确匹配。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)
        source_id = store.upsert_source(
            self.conn, "book-a", "book-a", "textbook",
            independence_group="book:a")
        self.snapshot = store.add_source_snapshot(
            self.conn, source_id, "v1", content="材料")
        self.run_id = store.create_run(self.conn, "test", "test-version")

    def tearDown(self):
        self.conn.close()

    def _batch(self):
        return ObservationBatch(
            entities=(observation("二分类", "task"), observation("分类", "task")),
            claims=(ClaimObservation(
                subject="二分类", relation="is_a", object="分类", qualifiers={},
                evidence_type="explicit_taxonomy", evidence="二分类属于分类。",
                location="§1", raw={}),),
            next_reading_targets=(), rejected=())

    def _materialize_with_one_endpoint_failing(self):
        # 「分类」这一端在本批消歧失败（ambiguous），但它早就在库里。
        def normalizer(obs, candidates):
            if obs.name == "分类":
                return {"decision": "ambiguous", "confidence": 0.1,
                        "reason": "歧义"}
            return {"decision": "new", "canonical_name": obs.name,
                    "confidence": 0.99, "reason": "新概念"}

        with patch("kg.entity_resolution._llm_normalize", side_effect=normalizer):
            return claims.materialize(
                self.conn, self._batch(), source_snapshot_id=self.snapshot.id,
                run_id=self.run_id)

    def test_existing_entity_is_claimed_when_batch_resolution_failed(self):
        existing = store.add_entity(self.conn, "分类", "task")

        result = self._materialize_with_one_endpoint_failing()

        self.assertEqual(1, len(result.claim_ids))
        claim = store.get_claim(self.conn, result.claim_ids[0])
        self.assertEqual(existing.id, claim.object_id)

    def test_verified_alias_also_reaches_the_existing_entity(self):
        existing = store.add_entity(self.conn, "分类问题", "task")
        store.add_alias(self.conn, existing.id, "分类", status="verified")

        result = self._materialize_with_one_endpoint_failing()

        self.assertEqual(1, len(result.claim_ids))
        self.assertEqual(
            existing.id, store.get_claim(self.conn, result.claim_ids[0]).object_id)

    def test_whitespace_only_difference_reaches_existing_endpoint(self):
        existing = store.add_entity(self.conn, "linear regression", "task")
        batch = ObservationBatch(
            entities=(observation("二分类", "task"),),
            claims=(ClaimObservation(
                subject="二分类", relation="is_a", object="linearregression",
                qualifiers={}, evidence_type="explicit_taxonomy",
                evidence="二分类属于线性回归。", location="§1", raw={}),),
            next_reading_targets=(), rejected=())

        with patch(
                "kg.entity_resolution._llm_normalize",
                return_value={
                    "decision": "new", "canonical_name": "二分类",
                    "confidence": 0.99, "reason": "新概念",
                }):
            result = claims.materialize(
                self.conn, batch, source_snapshot_id=self.snapshot.id,
                run_id=self.run_id)

        self.assertEqual(1, len(result.claim_ids))
        self.assertEqual(
            existing.id, store.get_claim(self.conn, result.claim_ids[0]).object_id)

    def test_unresolvable_endpoint_leaves_the_claim_pending_not_rejected(self):
        # 库里没有这个实体。observation 必须留 pending——rejected 是终态，
        # replay_pending 永远不会再看它。
        result = self._materialize_with_one_endpoint_failing()

        self.assertEqual((), result.claim_ids)
        status = self.conn.execute(
            "SELECT status FROM observations WHERE run_id=? AND relation='is_a'",
            (self.run_id,)).fetchone()["status"]
        self.assertEqual("pending", status)

    def test_merged_entity_is_not_claimed_as_an_endpoint(self):
        gone = store.add_entity(self.conn, "分类", "task")
        target = store.add_entity(self.conn, "分类问题", "task")
        store.merge_entities(self.conn, gone.id, target.id)

        result = self._materialize_with_one_endpoint_failing()

        # 合并把「分类」变成了 target 的 verified alias，所以应该落到 target，
        # 绝不能落回状态为 merged 的死实体。
        self.assertEqual(1, len(result.claim_ids))
        self.assertEqual(
            target.id, store.get_claim(self.conn, result.claim_ids[0]).object_id)

    def test_suspected_endpoint_is_not_short_circuited_by_a_later_batch_entity(self):
        # 「分类」进了疑似对齐队列，随后本批另一个实体恰好建出了同名规范名。
        # 此时退回精确匹配会认领这个新实体，等于抢在对齐裁决之前替它做了决定。
        store.add_entity(self.conn, "分类问题", "task")
        batch = ObservationBatch(
            entities=(
                observation("二分类", "task"),
                observation("分类", "task"),
                observation("类别划分", "task"),
            ),
            claims=(ClaimObservation(
                subject="二分类", relation="is_a", object="分类", qualifiers={},
                evidence_type="explicit_taxonomy", evidence="二分类属于分类。",
                location="§1", raw={}),),
            next_reading_targets=(), rejected=())

        def normalizer(obs, candidates):
            if obs.name == "分类":
                return {"decision": "existing", "candidate_id": 1,
                        "canonical_name": "分类问题", "confidence": 0.82,
                        "reason": "定义一致"}
            if obs.name == "类别划分":
                return {"decision": "new", "canonical_name": "分类",
                        "confidence": 0.99, "reason": "新概念"}
            return {"decision": "new", "canonical_name": obs.name,
                    "confidence": 0.99, "reason": "新概念"}

        with patch("kg.entity_resolution._llm_normalize", side_effect=normalizer):
            result = claims.materialize(
                self.conn, batch, source_snapshot_id=self.snapshot.id,
                run_id=self.run_id)

        self.assertIsNotNone(store.find_canonical_entity(self.conn, "分类"))
        self.assertEqual((), result.claim_ids)


class ProposedAliasReviewTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)
        source_id = store.upsert_source(
            self.conn, "book-a", "book-a", "textbook",
            independence_group="book:a")
        self.snapshot = store.add_source_snapshot(
            self.conn, source_id, "v1", content="术语材料")
        self.entity = store.add_entity(self.conn, "梯度上升", "solution")
        store.add_alias(
            self.conn, self.entity.id, "Gradient ascent",
            source_snapshot_id=self.snapshot.id, status="proposed")

    def tearDown(self):
        self.conn.close()

    def test_translation_review_verifies_proposed_alias(self):
        with patch(
                "kg.entity_resolution._llm_normalize",
                return_value={
                    "decision": "existing",
                    "candidate_id": self.entity.id,
                    "canonical_name": "梯度上升",
                    "match_type": "translation_alias",
                    "confidence": 0.99,
                    "reason": "直接翻译",
                }):
            results = entity_resolution.review_proposed_aliases(
                self.conn, limit=10)

        self.assertEqual("verified", results[0]["status"])
        alias = self.conn.execute(
            "SELECT status,alias_type FROM aliases").fetchone()
        self.assertEqual("verified", alias["status"])
        self.assertEqual("translation_alias", alias["alias_type"])

    def test_single_llm_failure_does_not_abort_alias_review_batch(self):
        other = store.add_entity(self.conn, "牛顿法", "solution")
        store.add_alias(
            self.conn, other.id, "Newton's method",
            source_snapshot_id=self.snapshot.id, status="proposed")

        def classify(observation, _candidates):
            if observation.name == "Gradient ascent":
                raise ValueError("bad JSON")
            return {
                "decision": "existing",
                "candidate_id": other.id,
                "canonical_name": "牛顿法",
                "match_type": "translation_alias",
                "confidence": 0.99,
                "reason": "直接翻译",
            }

        with patch("kg.entity_resolution._llm_normalize", side_effect=classify):
            results = entity_resolution.review_proposed_aliases(
                self.conn, limit=10)

        by_alias = {item["alias"]: item for item in results}
        self.assertEqual("proposed", by_alias["Gradient ascent"]["status"])
        self.assertEqual("verified", by_alias["Newton's method"]["status"])


class QueueProcessingTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)
        source_id = store.upsert_source(
            self.conn, "book-a", "book-a", "textbook",
            independence_group="book:a")
        self.snapshot = store.add_source_snapshot(
            self.conn, source_id, "v1", content="分类材料")
        # 类型冲突队列只看当前算法版本产出的断言，所以夹具的 run 必须用当前版本。
        self.run_id = store.create_run(
            self.conn, "test", pipeline.ALGORITHM_VERSION)

    def tearDown(self):
        self.conn.close()

    def _type_conflict(self, name, primary_type, observed_type, *, run_id=None):
        entity = store.add_entity(self.conn, name, primary_type)
        observation_id = store.add_observation(
            self.conn, run_id or self.run_id, self.snapshot.id,
            subject_text=name, subject_type=observed_type,
            excerpt=f"教材讨论{name}。")
        store.add_type_assertion(
            self.conn, entity.id, observed_type,
            source_snapshot_id=self.snapshot.id,
            observation_id=observation_id, status="conflict")
        return entity

    def test_type_conflict_from_a_stale_algorithm_version_is_not_queued(self):
        """换词表之后，旧版本记的观察类型跟新主类型比对没有意义，不能塞进队列。"""
        stale_run = store.create_run(self.conn, "test", "grounded-pipeline-3")
        self._type_conflict("感知器损失", "criterion", "concept", run_id=stale_run)

        self.assertEqual([], review_queues.review_type_conflicts(self.conn))

    def _suspected(self, canonical: str, observed: str, entity_type="concept"):
        entity = store.add_entity(self.conn, canonical, entity_type)
        store.add_alias(
            self.conn, entity.id, observed,
            source_snapshot_id=self.snapshot.id, status="proposed")
        store.add_alignment_evidence(
            self.conn, observed_name=observed, entity_id=entity.id,
            confidence=0.88, policy_version="test",
            resolver_version="test", source_snapshot_id=self.snapshot.id)
        return entity

    def test_suspected_name_variant_review_is_verified_but_model_review_is_not_source(self):
        self._suspected("反向传播", "反向传播算法", entity_type="solution")

        with patch(
                "kg.entity_resolution._review_alignment_with_llm",
                return_value={
                    "verdict": "same",
                    "match_type": "name_variant",
                    "confidence": 0.98,
                    "reason": "仅多一个类别词",
                }):
            result = entity_resolution.review_suspected_alignments(
                self.conn, limit=10)

        self.assertTrue(result[0]["auto_verified"])
        candidate = self.conn.execute(
            "SELECT * FROM entity_alignment_candidates").fetchone()
        self.assertEqual("verified", candidate["status"])
        self.assertEqual(1, candidate["independent_sources"])
        review = self.conn.execute(
            "SELECT * FROM model_queue_reviews").fetchone()
        self.assertEqual("entity_alignment", review["queue_type"])

    def test_sibling_head_words_are_not_auto_verified_in_the_queue(self):
        """「softmax运算」对「softmax函数」共享词根但换了中心词，队列里也不放行。

        队列这条路径拿不到观察类型，类型闸在这里是空转的；挡住它的必须是包含关系
        本身，否则复核队列会成为绕开快路收窄的后门。
        """
        self._suspected("Softmax 函数", "softmax运算")

        with patch(
                "kg.entity_resolution._review_alignment_with_llm",
                return_value={
                    "verdict": "same",
                    "match_type": "name_variant",
                    "confidence": 0.98,
                    "reason": "仅类别词不同",
                }):
            result = entity_resolution.review_suspected_alignments(
                self.conn, limit=10)

        self.assertFalse(result[0]["auto_verified"])
        candidate = self.conn.execute(
            "SELECT * FROM entity_alignment_candidates").fetchone()
        self.assertEqual("suspected", candidate["status"])
        # 模型说了话就要留痕，哪怕结论没被采信。
        review = self.conn.execute(
            "SELECT * FROM model_queue_reviews").fetchone()
        self.assertEqual("same", review["verdict"])

    def test_pending_observations_replay_idempotently_after_alias_verification(self):
        category = store.add_entity(self.conn, "分类问题", "task")
        binary = store.add_entity(self.conn, "二分类", "task")
        store.add_alias(
            self.conn, category.id, "分类",
            source_snapshot_id=self.snapshot.id, status="verified")
        entity_observation_id = store.add_observation(
            self.conn, self.run_id, self.snapshot.id,
            subject_text="分类", subject_type="task",
            excerpt="教材明确讨论了分类。", payload={
                "name": "分类", "entity_type": "task", "aliases": []})
        claim_observation_id = store.add_observation(
            self.conn, self.run_id, self.snapshot.id,
            subject_text="二分类", relation="is_a", object_text="分类",
            excerpt="二分类属于分类。", payload={
                "subject": "二分类", "relation": "is_a", "object": "分类",
                "qualifiers": {}, "evidence_type": "explicit_taxonomy"})

        first = claims.replay_pending(self.conn)
        second = claims.replay_pending(self.conn)

        self.assertEqual([category.id], first["resolved_entities"])
        self.assertEqual(1, len(first["resolved_claims"]))
        self.assertEqual(0, second["examined"])
        self.assertEqual(
            ["resolved", "resolved"],
            [row["status"] for row in self.conn.execute(
                "SELECT status FROM observations WHERE id IN (?,?) ORDER BY id",
                (entity_observation_id, claim_observation_id))])
        self.assertEqual(
            1, self.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0])
        self.assertEqual(binary.id, self.conn.execute(
            "SELECT subject_id FROM claims").fetchone()[0])

    def test_type_conflict_review_records_evidence_without_changing_primary_type(self):
        entity = store.add_entity(self.conn, "损失函数", "criterion")
        observation_id = store.add_observation(
            self.conn, self.run_id, self.snapshot.id,
            subject_text="损失函数", subject_type="concept",
            excerpt="教材讨论损失函数。")
        store.add_type_assertion(
            self.conn, entity.id, "concept",
            source_snapshot_id=self.snapshot.id,
            observation_id=observation_id, status="conflict")

        with patch(
                "kg.review_queues._review_type_with_llm",
                return_value={
                    "verdict": "keep_primary",
                    "suggested_type": "criterion",
                    "confidence": 0.99,
                    "reason": "loss 比 concept 更具体",
                }):
            result = review_queues.review_type_conflicts(self.conn)

        self.assertEqual("criterion", result[0]["suggested_type"])
        self.assertFalse(result[0]["auto_changed"])
        self.assertEqual("criterion", store.get_entity(self.conn, entity.id).entity_type)
        review = self.conn.execute(
            "SELECT * FROM model_queue_reviews").fetchone()
        self.assertEqual("type_conflict", review["queue_type"])


class ContextualLinkTests(unittest.TestCase):
    """局部链接与全局别名是两个门槛，不能互相阻塞。

    「本次 mention 指向实体 X」比「这个字符串永远指向 X」弱得多。此前两者共用
    verified alias 一套门槛，结果一条尚未转正的别名会把整条 Claim 一起卡住。
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)
        source_id = store.upsert_source(
            self.conn, "book-a", "book-a", "textbook",
            independence_group="book:a")
        self.snapshot = store.add_source_snapshot(
            self.conn, source_id, "v1", content="牛顿法材料")
        self.run_id = store.create_run(self.conn, "test", "test-version")
        self.newton = store.add_entity(self.conn, "牛顿法", "solution")
        store.add_entity(self.conn, "二阶优化", "solution")

    def tearDown(self):
        self.conn.close()

    def _batch(self):
        return ObservationBatch(
            entities=(
                observation("牛顿-拉弗森法", "solution"),
                observation("二阶优化", "solution"),
            ),
            claims=(ClaimObservation(
                subject="二阶优化", relation="is_a", object="牛顿-拉弗森法",
                qualifiers={}, evidence_type="explicit_taxonomy",
                evidence="牛顿-拉弗森法是一种二阶优化。", location="§1", raw={}),),
            next_reading_targets=(), rejected=())

    def _materialize(self, confidence=0.97):
        with patch(
                "kg.entity_resolution._llm_normalize",
                return_value={
                    "decision": "existing",
                    "candidate_id": self.newton.id,
                    "canonical_name": "牛顿法",
                    "match_type": "translation_alias",
                    "confidence": confidence,
                    "reason": "原文括号注明二者同一",
                }):
            return claims.materialize(
                self.conn, self._batch(), source_snapshot_id=self.snapshot.id,
                run_id=self.run_id)

    def test_contextual_link_lands_the_claim_but_not_the_global_alias(self):
        result = self._materialize()

        self.assertEqual(1, len(result.claim_ids))
        self.assertIn(self.newton.id, result.entity_ids)
        alias = self.conn.execute(
            "SELECT status FROM aliases WHERE entity_id=?",
            (self.newton.id,)).fetchone()
        self.assertEqual("proposed", alias["status"])
        candidate = self.conn.execute(
            "SELECT status FROM entity_alignment_candidates").fetchone()
        self.assertEqual("suspected", candidate["status"])

    def test_below_auto_link_confidence_still_blocks(self):
        """降低的是全局别名的门槛，不是自动执行的门槛。"""
        result = self._materialize(confidence=0.85)

        self.assertEqual((), result.claim_ids)
        self.assertNotIn(self.newton.id, result.entity_ids)

    def test_materialization_trail_covers_entity_claim_and_evidence(self):
        result = self._materialize()
        event = self.conn.execute(
            "SELECT id FROM entity_resolution_events"
            " WHERE outcome='contextual_same_entity'").fetchone()

        trail = store.materializations(
            self.conn, resolution_event_id=event["id"])

        by_type = {}
        for row in trail:
            by_type.setdefault(row["target_type"], []).append(row["target_id"])
        self.assertEqual([self.newton.id], by_type["entity"])
        self.assertEqual(list(result.claim_ids), by_type["claim"])
        # 实体证据和 Claim 证据都要挂在这次判定下，撤销时才知道动哪些行。
        self.assertEqual(2, len(by_type["evidence"]))

    def test_revert_rejects_the_claim_and_reopens_the_observation(self):
        result = self._materialize()
        event = self.conn.execute(
            "SELECT id FROM entity_resolution_events"
            " WHERE outcome='contextual_same_entity'").fetchone()

        reverted = store.revert_materialization(
            self.conn, resolution_event_id=event["id"],
            reason="牛顿-拉弗森法与牛顿法在本书里是两节")

        self.assertEqual(list(result.claim_ids), reverted["reverted"]["claim"])
        # 证据行留着，实体没被拖下水——两者都要能从返回值里看出来。
        self.assertEqual(2, len(reverted["retained_evidence"]))
        self.assertEqual([self.newton.id], reverted["kept_entities"])
        self.assertEqual(
            "rejected",
            store.get_claim(self.conn, result.claim_ids[0]).status)
        # 命中的是既有实体，它有自己的来历，不跟着一次连错被拒。
        self.assertEqual(
            "proposed", store.get_entity(self.conn, self.newton.id).status)
        pending = {
            row["subject_text"] for row in self.conn.execute(
                "SELECT subject_text FROM observations WHERE status='pending'")}
        self.assertIn("牛顿-拉弗森法", pending)
        self.assertEqual("rejected", self.conn.execute(
            "SELECT status FROM aliases WHERE entity_id=?",
            (self.newton.id,)).fetchone()["status"])

    def test_revert_is_not_repeatable_and_leaves_a_trail(self):
        self._materialize()
        event = self.conn.execute(
            "SELECT id FROM entity_resolution_events"
            " WHERE outcome='contextual_same_entity'").fetchone()
        store.revert_materialization(
            self.conn, resolution_event_id=event["id"], reason="判错了")

        with self.assertRaisesRegex(ValueError, "没有可撤销的物化记录"):
            store.revert_materialization(
                self.conn, resolution_event_id=event["id"], reason="再来一次")
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM entity_resolution_events"
            " WHERE outcome='reverted'").fetchone())

    def test_created_entity_is_rejected_on_revert(self):
        """新建的实体没有别的来历，撤销时跟着走。"""
        batch = ObservationBatch(
            entities=(observation("拟牛顿法", "solution"),),
            claims=(), next_reading_targets=(), rejected=())
        with patch(
                "kg.entity_resolution._llm_normalize",
                return_value={
                    "decision": "new", "canonical_name": "拟牛顿法",
                    "confidence": 0.97, "reason": "语料新概念",
                }):
            result = claims.materialize(
                self.conn, batch, source_snapshot_id=self.snapshot.id,
                run_id=self.run_id)

        event = self.conn.execute(
            "SELECT id FROM entity_resolution_events"
            " WHERE outcome='created'").fetchone()
        store.revert_materialization(
            self.conn, resolution_event_id=event["id"], reason="不是独立概念")

        self.assertEqual(
            "rejected", store.get_entity(self.conn, result.entity_ids[0]).status)


class SeparationReviewTests(unittest.TestCase):
    """高风险名称对的反向复核：只能否决，不能支持。

    正向提问天然偏向找相同点，同词根异中心词正好是它最容易点头的地方。反向再问
    一次「优先寻找不能合并的理由」，两轮一致才允许当场落地。两轮都是同一个模型，
    所以只记进消歧事件，不进独立来源计数。
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)
        self.entity = store.add_entity(self.conn, "机器学习算法", "solution")
        source_id = store.upsert_source(
            self.conn, "book-a", "book-a", "textbook",
            independence_group="book:a")
        self.snapshot = store.add_source_snapshot(
            self.conn, source_id, "v1", content="机器学习材料")

    def tearDown(self):
        self.conn.close()

    def _resolve(self, separation, name="机器学习方法"):
        self.calls = []

        def verifier(observation, candidate):
            self.calls.append((observation.name, candidate["canonical_name"]))
            return separation

        return entity_resolution.resolve(
            self.conn, observation(name, "solution"),
            source_snapshot_id=self.snapshot.id,
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing",
                "candidate_id": self.entity.id,
                "canonical_name": "机器学习算法",
                "match_type": "name_variant",
                "confidence": 0.98,
                "reason": "模型认为只是换了个类别词",
            },
            llm_separation_verifier=verifier)

    def test_objection_blocks_the_link_and_records_no_alignment_evidence(self):
        result = self._resolve({
            "verdict": "should_stay_separate", "confidence": 0.85,
            "reason": "教材分两节讲：一节讲方法论，一节讲具体算法",
        })

        self.assertIsNone(result.entity_id)
        self.assertEqual("ambiguous", result.outcome)
        self.assertIn("反向复核", result.reason)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM entity_alignment_candidates").fetchone())

    def test_agreement_allows_the_contextual_link(self):
        result = self._resolve({
            "verdict": "no_stable_difference", "confidence": 0.9,
            "reason": "本书两处混用，没有可复用的区别",
        })

        self.assertEqual(self.entity.id, result.entity_id)
        self.assertEqual("contextual_same_entity", result.outcome)
        # 全局别名照旧要挣，反向复核只是没有拦它。
        self.assertEqual("proposed", self.conn.execute(
            "SELECT status FROM aliases").fetchone()["status"])

    def test_uncertain_falls_back_to_suspected_not_to_a_link(self):
        """两轮不一致就不算一致。uncertain 不否决，但也不放行。"""
        result = self._resolve({
            "verdict": "uncertain", "confidence": 0.4, "reason": "语料不足"})

        self.assertIsNone(result.entity_id)
        self.assertEqual("suspected_same_entity", result.outcome)

    def test_low_confidence_objection_is_not_binding(self):
        result = self._resolve({
            "verdict": "should_stay_separate", "confidence": 0.3,
            "reason": "说不太准"})

        self.assertEqual("suspected_same_entity", result.outcome)

    def test_verifier_failure_is_not_a_conclusion(self):
        def failing(_observation, _candidate):
            raise RuntimeError("连接被重置")

        result = entity_resolution.resolve(
            self.conn, observation("机器学习方法", "solution"),
            source_snapshot_id=self.snapshot.id,
            llm_normalizer=lambda _observation, _candidates: {
                "decision": "existing", "candidate_id": self.entity.id,
                "canonical_name": "机器学习算法", "match_type": "name_variant",
                "confidence": 0.98, "reason": "换了个类别词",
            },
            llm_separation_verifier=failing)

        self.assertEqual("suspected_same_entity", result.outcome)
        self.assertIn("反向复核调用失败", result.reason)

    def test_plain_containment_never_pays_for_a_second_call(self):
        """第二档已经由确定性规则确认，再问一次是白花 1~3 分钟。"""
        store.add_entity(self.conn, "反向传播", "solution")

        def fail_if_called(_observation, _candidate):
            self.fail("确定性快路命中时不应调用反向复核")

        result = entity_resolution.resolve(
            self.conn, observation("反向传播算法", "solution"),
            source_snapshot_id=self.snapshot.id,
            llm_normalizer=lambda _observation, candidates: {
                "decision": "existing",
                "candidate_id": next(
                    item["id"] for item in candidates
                    if item["canonical_name"] == "反向传播"),
                "canonical_name": "反向传播", "match_type": "name_variant",
                "confidence": 0.98, "reason": "仅多一个类别词",
            },
            llm_separation_verifier=fail_if_called)

        self.assertEqual("same_entity", result.outcome)

    def test_candidates_carry_real_usage_not_just_definitions(self):
        """候选侧不给上下文，语境判断就只剩一侧有语境。"""
        other = store.add_entity(self.conn, "梯度下降", "solution")
        store.add_evidence(
            self.conn, self.snapshot.id, "机器学习算法通过数据拟合参数。",
            "definition", entity_id=self.entity.id, mechanically_valid=True)
        store.add_claim(self.conn, other.id, "used_for", self.entity.id)
        seen = {}

        entity_resolution.resolve(
            self.conn, observation("机器学习流程", "solution"),
            source_snapshot_id=self.snapshot.id,
            llm_normalizer=lambda _observation, candidates: seen.update(
                {item["canonical_name"]: item for item in candidates})
            or {"decision": "ambiguous", "confidence": 0.1, "reason": "看看候选"})

        candidate = seen["机器学习算法"]
        self.assertEqual(
            ["机器学习算法通过数据拟合参数。"], candidate["excerpts"])
        self.assertEqual(
            ["梯度下降 -used_for-> 机器学习算法"], candidate["relations"])


class MentionStabilityTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)
        self.first = store.add_entity(self.conn, "梯度下降", "solution")
        self.second = store.add_entity(self.conn, "梯度上升", "solution")

    def tearDown(self):
        self.conn.close()

    def _event(self, name, entity_id, group, outcome="contextual_same_entity"):
        source_id = store.upsert_source(
            self.conn, f"src-{group}", group, "textbook",
            independence_group=group)
        snapshot = store.add_source_snapshot(
            self.conn, source_id, group, content=f"{group} 正文 {name}")
        store.add_resolution_event(
            self.conn, raw_name=name,
            deterministic_name=store.normalize_name(name),
            entity_id=entity_id, outcome=outcome, matched_by="llm_contextual",
            resolver_version=entity_resolution.RESOLVER_VERSION,
            source_snapshot_id=snapshot.id)

    def test_stable_name_across_independent_groups_is_surfaced(self):
        self._event("梯度下降法", self.first.id, "book:a")
        self._event("梯度下降法", self.first.id, "book:b")

        report = entity_resolution.mention_stability_report(self.conn)

        item = report["items"][0]
        self.assertEqual("梯度下降法", item["name"])
        self.assertEqual(1.0, item["dominant_target_ratio"])
        self.assertEqual(0, item["competing_target_count"])
        self.assertEqual(["book:a", "book:b"], item["independent_source_groups"])
        self.assertEqual(1, report["stable_but_unverified"])

    def test_competing_targets_are_ranked_first(self):
        """一个名字在两个实体之间摇摆，任一侧的累计分再高也不该转正。"""
        self._event("梯度法", self.first.id, "book:a")
        self._event("梯度法", self.second.id, "book:b")
        self._event("梯度下降法", self.first.id, "book:a")
        self._event("梯度下降法", self.first.id, "book:b")

        report = entity_resolution.mention_stability_report(self.conn)

        self.assertEqual("梯度法", report["items"][0]["name"])
        self.assertEqual(1, report["items"][0]["competing_target_count"])
        self.assertEqual(1, report["with_competing_targets"])
        self.assertEqual(1, report["stable_but_unverified"])

    def test_canonical_names_are_not_reported(self):
        self._event("梯度下降", self.first.id, "book:a")
        self._event("梯度下降", self.first.id, "book:b")

        self.assertEqual([], entity_resolution.mention_stability_report(
            self.conn)["items"])


if __name__ == "__main__":
    unittest.main()
