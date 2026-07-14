import unittest
from types import SimpleNamespace

from core.ai_reviewer import claim_ai_autoreply_once


class FakeLogs:
    def __init__(self, *, modified_count, find_results=()):
        self.modified_count = modified_count
        self.find_results = list(find_results)
        self.update_query = None
        self.update = None
        self.find_queries = []

    async def update_one(self, query, update):
        self.update_query = query
        self.update = update
        return SimpleNamespace(modified_count=self.modified_count)

    async def find_one(self, query, projection):
        self.find_queries.append((query, projection))
        return self.find_results.pop(0)


class AiAutoreplyClaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomically_claims_type_and_checks_legacy_log_marker(self):
        logs = FakeLogs(modified_count=1)

        claimed = await claim_ai_autoreply_once(
            logs,
            456,
            " Apply ",
            display_name="How can I apply?",
            bot_user_id=123,
        )

        self.assertTrue(claimed)
        self.assertEqual(logs.update, {"$addToSet": {"ai_autoreplies_sent": "apply"}})
        self.assertEqual(logs.update_query["channel_id"], "456")
        self.assertEqual(logs.update_query["ai_autoreplies_sent"], {"$ne": "apply"})
        legacy_match = logs.update_query["$nor"][0]["messages"]["$elemMatch"]
        self.assertEqual(legacy_match["author.id"], "123")
        self.assertIn(r"How\ can\ I\ apply\?", legacy_match["content"]["$regex"])

    async def test_existing_claim_suppresses_duplicate(self):
        logs = FakeLogs(modified_count=0, find_results=[{"_id": "ticket"}])

        claimed = await claim_ai_autoreply_once(
            logs,
            456,
            "apply",
            display_name="How can I apply?",
            bot_user_id=123,
        )

        self.assertFalse(claimed)
        duplicate_query = logs.find_queries[0][0]
        self.assertEqual(duplicate_query["channel_id"], "456")
        self.assertIn({"ai_autoreplies_sent": "apply"}, duplicate_query["$or"])

    async def test_missing_ticket_log_fails_closed(self):
        logs = FakeLogs(modified_count=0, find_results=[None, None])

        with self.assertRaises(RuntimeError):
            await claim_ai_autoreply_once(logs, 456, "apply")


if __name__ == "__main__":
    unittest.main()
