import sqlite3
import unittest

from kg import alias_evidence, schema, store


DOC_SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_sections (
    id INTEGER PRIMARY KEY, book TEXT, sec_id TEXT, ord INTEGER,
    title TEXT, text TEXT DEFAULT '', content_hash TEXT DEFAULT '',
    orig_lang TEXT DEFAULT 'zh', url TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS corpus (
    id INTEGER PRIMARY KEY, lang TEXT, title TEXT, page_id INTEGER,
    revision_id INTEGER, text TEXT DEFAULT '');
"""


class DeclarationPatternTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)
        self.conn.executescript(DOC_SCHEMA)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _section(self, book, text, sec_id="1"):
        self.conn.execute(
            "INSERT INTO doc_sections(book,sec_id,ord,title,text,content_hash)"
            " VALUES (?,?,1,?,?,?)", (book, sec_id, book, text, f"h{book}{sec_id}"))
        self.conn.commit()

    def test_parenthetical_declaration(self):
        self._section("nndl", "本节介绍支持向量机（Support Vector Machine，SVM）的原理。")

        found = alias_evidence.find_declarations(self.conn, "SVM", "支持向量机")

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].pattern, "括号注释")
        self.assertEqual(found[0].independence_group, "book:nndl")

    def test_also_known_as_declaration(self):
        self._section("nndl", "Logistic 回归也称为对数几率回归。")

        found = alias_evidence.find_declarations(
            self.conn, "对数几率回归", "Logistic 回归")

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].pattern, "又称句式")

    def test_mere_cooccurrence_is_not_a_declaration(self):
        self._section("nndl", "支持向量机很常用。另外一段里我们也提到了 SVM 的求解。")

        self.assertEqual(
            alias_evidence.find_declarations(self.conn, "SVM", "支持向量机"), [])

    def test_same_name_is_not_a_declaration(self):
        self._section("nndl", "支持向量机（支持向量机）")

        self.assertEqual(
            alias_evidence.find_declarations(self.conn, "支持向量机", "支持向量机"), [])

    def test_counts_independent_groups(self):
        self._section("nndl", "支持向量机（Support Vector Machine，SVM）")
        self._section("cs231n", "支持向量机（Multiclass Support Vector Machine，SVM）")
        self._section("nndl", "再次提到支持向量机（SVM）", sec_id="2")

        found = alias_evidence.find_declarations(self.conn, "SVM", "支持向量机")
        groups = alias_evidence.independent_groups(found)

        self.assertEqual(groups, {"book:nndl", "book:cs231n"})


class AccumulationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.ensure(self.conn)
        self.conn.executescript(DOC_SCHEMA)
        self.conn.commit()
        self.entity = store.add_entity(self.conn, "支持向量机", "model")
        self.alias_id = store.add_alias(
            self.conn, self.entity.id, "SVM", status="proposed",
            alias_type="abbreviation")

    def tearDown(self):
        self.conn.close()

    def _section(self, book, text):
        self.conn.execute(
            "INSERT INTO doc_sections(book,sec_id,ord,title,text,content_hash)"
            " VALUES (?,'1',1,?,?,?)", (book, book, text, f"hash-{book}"))
        self.conn.commit()

    def _record(self):
        return alias_evidence.record(
            self.conn, self.alias_id, policy_version="test-policy",
            resolver_version="test-resolver")

    def test_single_source_declaration_does_not_verify(self):
        self._section("nndl", "支持向量机（Support Vector Machine，SVM）")

        result = self._record()

        self.assertEqual(result["independent_groups"], 1)
        self.assertEqual(result["status"], "proposed")

    def test_two_independent_declarations_verify(self):
        self._section("nndl", "支持向量机（Support Vector Machine，SVM）")
        self._section("cs231n", "支持向量机（Multiclass Support Vector Machine，SVM）")

        result = self._record()

        self.assertEqual(result["independent_groups"], 2)
        self.assertEqual(result["status"], "verified")

    def test_repeats_in_one_book_stay_one_group(self):
        self._section("nndl", "支持向量机（Support Vector Machine，SVM）")
        self.conn.execute(
            "INSERT INTO doc_sections(book,sec_id,ord,title,text,content_hash)"
            " VALUES ('nndl','2',2,'nndl','又见支持向量机（SVM）','hash-nndl-2')")
        self.conn.commit()

        result = self._record()

        self.assertEqual(result["independent_groups"], 1)
        self.assertEqual(result["status"], "proposed")

    def test_rerunning_does_not_double_count(self):
        self._section("nndl", "支持向量机（Support Vector Machine，SVM）")

        first = self._record()
        second = self._record()

        self.assertEqual(first["alignment_score"], second["alignment_score"])
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM entity_alignment_evidence").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_no_declaration_leaves_alias_untouched(self):
        self._section("nndl", "支持向量机很常用，SVM 也在别处出现。")

        result = self._record()

        self.assertEqual(result["declarations"], 0)
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) FROM entity_alignment_evidence").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
