import io
import json
import unittest
import zipfile

from core.log_export import build_ticket_log_zip, ticket_log_filename


class TicketLogExportTests(unittest.TestCase):
    def test_builds_safe_zip_with_complete_json_log(self):
        log = {"key": "abc/../unsafe", "messages": [{"content": "Hello ✓"}]}
        filename = ticket_log_filename(log, 1)
        payload = json.dumps(log, ensure_ascii=False, default=str).encode("utf-8")
        archive_bytes = build_ticket_log_zip([(filename, payload)])

        self.assertNotIn("/", filename)
        self.assertNotIn("\\", filename)
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            self.assertEqual(archive.namelist(), [filename])
            self.assertEqual(json.loads(archive.read(filename)), log)


if __name__ == "__main__":
    unittest.main()
