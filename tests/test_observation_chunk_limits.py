import unittest
from unittest.mock import patch

from kg.observations import extract


def payload(name: str, evidence: str) -> dict:
    return {
        "entities": [{
            "name": name,
            "entity_type": "concept",
            "definition": "",
            "aliases": [],
            "evidence": evidence,
            "location": "test",
        }],
        "claims": [],
    }


class ObservationChunkLimitTests(unittest.TestCase):
    @patch("kg.observations.split_text", return_value=["第一块证据", "第二块证据"])
    @patch("kg.observations.llm.pmap", side_effect=lambda fn, items: [fn(item) for item in items])
    @patch("kg.observations.llm.chat_json")
    def test_entity_limit_applies_per_chunk_not_per_document(
            self, chat_json, _pmap, _split_text):
        chat_json.side_effect = [
            payload("实体一", "第一块证据"),
            payload("实体二", "第二块证据"),
        ]

        batch = extract("任意原文", "ai", max_entities=1, max_claims=1)

        self.assertEqual(["实体一", "实体二"], [item.name for item in batch.entities])
        prompts = [call.args[0][0]["content"] for call in chat_json.call_args_list]
        self.assertTrue(all("本文本块最多 1 个实体、1 个 Claim" in prompt for prompt in prompts))


if __name__ == "__main__":
    unittest.main()
