"""Offline unittest verification: never loads .env or contacts Google."""
import contextlib
import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
import main


def response(body=None, status=200, headers=None):
    result = MagicMock(status_code=status, headers=headers or {})
    result.json.return_value = body if body is not None else {}
    result.__enter__.return_value = result
    return result


class CollectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "businesses.csv"
        self.usage = Path(directory.name) / "usage.json"
        session_patch = patch("main.requests.Session")
        self.session = session_patch.start().return_value.__enter__.return_value
        self.addCleanup(session_patch.stop)
        guard = patch("requests.sessions.Session.request", side_effect=AssertionError("External HTTP forbidden in tests"))
        guard.start()
        self.addCleanup(guard.stop)
        sleep = patch("main.time.sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        self.log = io.StringIO()
        self.month = main.utc_now().strftime("%Y-%m")

    def run_collection(self, pages, keywords=None, **limits):
        self.session.post.side_effect = pages
        with contextlib.redirect_stdout(self.log):
            return main.collect("fake-test-key", keywords or ["nail salon Yerevan"], self.output,
                self.usage, limits.get("max_results", 60), limits.get("run_limit", 10),
                limits.get("monthly_limit", 100))

    def rows(self):
        with self.output.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(reader.fieldnames, main.COLUMNS)
            return list(reader)

    def count(self):
        return main.read_usage(self.usage).get(self.month, 0)

    def test_pagination_dedup_and_all_matching_keywords(self):
        self.assertEqual(self.run_collection([
            response({"places": [{"id": "a", "displayName": {"text": "Branch"}}], "nextPageToken": "p2"}),
            response({"places": [{"id": "b", "displayName": {"text": "Branch"}}]}),
            response({"places": [{"id": "a", "nationalPhoneNumber": "010 123456"}]}),
        ], ["nail salon Yerevan", "beauty salon Yerevan"]), 0)
        rows = self.rows()
        self.assertEqual(len(rows), 2)  # Same name does not merge branches.
        self.assertEqual(rows[0]["search_keywords"], "nail salon Yerevan | beauty salon Yerevan")
        self.assertEqual(rows[0]["phone_number"], "010 123456")
        calls = self.session.post.call_args_list
        self.assertEqual(calls[1].kwargs["json"], {"textQuery": "nail salon Yerevan", "pageSize": 20, "pageToken": "p2"})
        self.assertEqual(calls[0].kwargs["headers"]["X-Goog-FieldMask"], main.FIELD_MASK)
        self.assertNotIn(" ", main.FIELD_MASK)
        self.assertNotIn("*", main.FIELD_MASK)
        self.assertFalse(calls[0].kwargs["allow_redirects"])
        self.assertEqual(calls[0].kwargs["timeout"], (5, 30))
        self.assertEqual(self.count(), 3)

    def test_optional_fields_unicode_phone_and_website_flags(self):
        self.run_collection([response({"places": [
            {"id": "a", "displayName": {"text": "Երևան Салон"}, "websiteUri": "https://example.com",
             "internationalPhoneNumber": "+374 10 123456", "nationalPhoneNumber": "fallback"},
            {"id": "b"}, {"id": "c", "websiteUri": ""}, {"displayName": {"text": "No identity"}},
        ]})])
        rows = self.rows()
        self.assertTrue(self.output.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(rows[0]["business_name"], "Երևան Салон")
        self.assertEqual(rows[0]["phone_number"], "'+374 10 123456")
        self.assertEqual([r["has_website"] for r in rows], ["TRUE", "FALSE", "FALSE"])
        self.assertEqual(rows[1]["address"], "")
        self.assertIsNotNone(datetime.fromisoformat(rows[0]["collected_at"]).tzinfo)
        self.assertIn("missing its required place_id", self.log.getvalue())

    def test_formula_safety_beyond_whitespace_and_controls(self):
        for cell in ["=cmd()", "+37410123456", "-2+3", "@SUM(A1)", " \t\r\x00\u200b=1", "\n+1", "＝1"]:
            self.assertEqual(main.safe_cell(cell), "'" + cell)
        self.assertEqual(main.safe_cell("Երևան"), "Երևան")
        self.run_collection([response({"places": [{"id": "a", "displayName": {"text": " \t=1"},
            "formattedAddress": "@danger", "nationalPhoneNumber": "+374123456"}]})])
        row = self.rows()[0]
        self.assertEqual(row["business_name"], "' \t=1")
        self.assertEqual(row["address"], "'@danger")
        self.assertNotIn('="', self.output.read_text(encoding="utf-8-sig"))

    def test_website_uri_hostname_classification_and_csv_columns(self):
        cases = [
            ("https://instagram.com/studio", "instagram_url"),
            ("https://WWW.InStAgRaM.CoM/studio?ref=Original", "instagram_url"),
            ("https://a.b.instagram.com/studio", "instagram_url"),
            ("https://facebook.com/studio", "facebook_url"),
            ("https://M.FACEBOOK.COM/studio?ref=Original", "facebook_url"),
            ("https://a.b.facebook.com/studio", "facebook_url"),
            ("https://example.com/instagram", "website"),
            ("https://example.com/facebook.com", "website"),
            ("https://fakeinstagram.com/studio", "website"),
            ("https://fakefacebook.com/studio", "website"),
            ("https://instagram.com.example.com/studio", "website"),
            ("https://facebook.com@example.com/studio", "website"),
            ("https://example.com/?url=instagram.com", "website"),
            (None, None), ("", None), ("   ", None),
        ]
        places = [{"id": str(index), "websiteUri": url} for index, (url, _) in enumerate(cases)]
        places.append({"id": "missing"})
        self.assertEqual(self.run_collection([response({"places": places})]), 0)
        rows = self.rows()
        for row, (url, destination) in zip(rows, cases):
            with self.subTest(url=url):
                for column in ("website", "instagram_url", "facebook_url"):
                    self.assertEqual(row[column], url if column == destination else "")
                self.assertEqual(row["has_website"], "TRUE" if destination == "website" else "FALSE")
        self.assertEqual([rows[-1][key] for key in ("website", "instagram_url", "facebook_url", "has_website")],
                         ["", "", "", "FALSE"])

    def test_social_urls_preserve_dedup_keywords_and_csv_safety(self):
        self.run_collection([
            response({"places": [{"id": "a", "websiteUri": "https://Instagram.com/Original"}]}),
            response({"places": [{"id": "a", "websiteUri": "https://facebook.com/Original"}]}),
            response({"places": [{"id": "a", "websiteUri": "https://example.com/"},
                                 {"id": "unsafe", "websiteUri": " \t=HYPERLINK(A1)"}]}),
        ], ["first", "second", "third"])
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["search_keywords"], "first | second | third")
        self.assertEqual(rows[0]["instagram_url"], "https://Instagram.com/Original")
        self.assertEqual(rows[0]["facebook_url"], "https://facebook.com/Original")
        self.assertEqual(rows[0]["website"], "https://example.com/")
        self.assertEqual(rows[0]["has_website"], "TRUE")
        self.assertEqual(rows[1]["website"], "' \t=HYPERLINK(A1)")

    def test_retry_counts_persist_before_every_http_attempt(self):
        responses = iter([response(status=503), requests.Timeout("secret provider error"), response({"places": []})])
        def post(*args, **kwargs):
            self.assertEqual(self.count(), self.session.post.call_count)
            result = next(responses)
            if isinstance(result, Exception):
                raise result
            return result
        self.session.post.side_effect = post
        with contextlib.redirect_stdout(self.log):
            code = main.collect("fake-test-key", ["salon"], self.output, self.usage, 60, 10, 100)
        self.assertEqual(code, 0)
        self.assertEqual(self.count(), 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2])
        self.assertNotIn("secret provider error", self.log.getvalue())

    def test_run_limit_includes_retry(self):
        code = self.run_collection([response(status=503), response(status=503), response()], run_limit=2)
        self.assertEqual(code, 1)
        self.assertEqual(self.session.post.call_count, 2)
        self.assertEqual(self.count(), 2)
        self.assertFalse(self.output.exists())

    def test_monthly_limit_includes_pagination(self):
        self.usage.write_text(json.dumps({self.month: 99}))
        code = self.run_collection([response({"places": [{"id": "a"}], "nextPageToken": "p2"}), response()])
        self.assertEqual(code, 1)
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(self.count(), 100)
        self.assertEqual(len(self.rows()), 1)

    def test_zero_request_limit_preserves_existing_output(self):
        self.output.write_bytes(b"previous valid CSV")
        self.run_collection([], run_limit=0)
        self.session.post.assert_not_called()
        self.assertEqual(self.output.read_bytes(), b"previous valid CSV")

    def test_corrupt_counter_never_resets_or_calls_google(self):
        for data in ['not json', '[]', '{"2026-13":2}', '{"2026-01":-1}',
                     '{"2026-01":true}', '{"2026-01":9,"2026-01":0}']:
            with self.subTest(data=data):
                self.usage.write_text(data)
                self.assertEqual(self.run_collection([]), 1)
                self.assertEqual(self.usage.read_text(), data)
                self.session.post.assert_not_called()

    def test_usage_write_failure_stops_before_request(self):
        with patch("main.atomic_write", side_effect=PermissionError("secret local information")):
            self.assertEqual(self.run_collection([]), 1)
        self.session.post.assert_not_called()
        self.assertIn("Cannot update usage.json", self.log.getvalue())
        self.assertNotIn("secret local information", self.log.getvalue())

    def test_partial_pages_survive_later_keyword_failure(self):
        code = self.run_collection([
            response({"places": [{"id": "a"}], "nextPageToken": "p2"}), response(status=400),
            response({"places": [{"id": "b"}]}),
        ], ["salon", "dentist"])
        self.assertEqual(code, 1)
        self.assertEqual([row["place_id"] for row in self.rows()], ["a", "b"])
        self.assertIn("salon: failed:", self.log.getvalue())
        self.assertIn("dentist: complete:", self.log.getvalue())

    def test_no_success_keeps_existing_output_and_hides_error_details(self):
        self.output.write_bytes(b"previous CSV")
        code = self.run_collection([response({"error": {"message": "API key fake-test-key invalid"}}, status=403)])
        self.assertEqual(code, 1)
        self.assertEqual(self.output.read_bytes(), b"previous CSV")
        self.assertNotIn("fake-test-key", self.log.getvalue())
        self.assertEqual(self.session.post.call_count, 1)

    def test_successful_empty_page_replaces_previous_csv_with_header(self):
        self.output.write_bytes(b"previous CSV")
        self.assertEqual(self.run_collection([response()]), 0)
        self.assertEqual(self.rows(), [])

    def test_fresh_run_replaces_old_records_instead_of_appending(self):
        self.run_collection([response({"places": [{"id": "old"}]})])
        self.run_collection([response({"places": [{"id": "new"}]})])
        self.assertEqual([row["place_id"] for row in self.rows()], ["new"])

    def test_locked_csv_keeps_previous_snapshot_and_stops(self):
        self.run_collection([response({"places": [{"id": "old"}]})])
        previous = self.output.read_bytes()
        replace = main.os.replace
        def locked(source, destination):
            if destination == self.output:
                raise PermissionError("Excel has it open")
            return replace(source, destination)
        with patch("main.os.replace", side_effect=locked):
            self.assertEqual(self.run_collection([response({"places": [{"id": "new"}]})]), 1)
        self.assertEqual(self.output.read_bytes(), previous)
        self.assertIn("Close it in Excel", self.log.getvalue())
        self.assertEqual(list(self.output.parent.glob("*.tmp")), [])

    def test_repeated_pagination_token_is_bounded_and_retains_pages(self):
        code = self.run_collection([
            response({"places": [{"id": "a"}], "nextPageToken": "same"}),
            response({"places": [{"id": "b"}], "nextPageToken": "same"}),
        ])
        self.assertEqual(code, 1)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.session.post.call_count, 2)
        self.assertIn("repeated pagination token", self.log.getvalue())

    def test_result_limit_has_distinct_incomplete_reason(self):
        self.assertEqual(self.run_collection([response({"places": [{"id": "a"}, {"id": "b"}],
            "nextPageToken": "p2"})], max_results=1), 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.session.post.call_args.kwargs["json"]["pageSize"], 1)
        self.assertIn("result limit reached", self.log.getvalue())

    def test_retry_after_seconds_date_and_excessive_delay(self):
        self.run_collection([response(status=429, headers={"Retry-After": "5"}), response()])
        self.sleep.assert_called_once_with(5)
        now = datetime(2026, 10, 16, tzinfo=timezone.utc)
        with patch("main.utc_now", return_value=now):
            date = (now + timedelta(seconds=7)).strftime("%a, %d %b %Y %H:%M:%S GMT")
            self.assertEqual(main.retry_delay(date, 0), 7)
        self.sleep.reset_mock()
        self.assertEqual(self.run_collection([response(status=429, headers={"Retry-After": "60"})]), 1)
        self.sleep.assert_not_called()

    def test_retries_never_exceed_two_and_quota_stops_run(self):
        self.assertEqual(self.run_collection([response(status=429)]*3, ["salon", "dentist"]), 1)
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.session.post.call_count, 3)
        self.assertIn("quota/rate limit", self.log.getvalue())
        self.assertIn("dentist: incomplete: not started", self.log.getvalue())

    def test_config_errors_stop_without_retry_and_without_response_dump(self):
        for message, diagnostic in [("billing disabled secret", "billing"),
                ("SERVICE_DISABLED secret", "Places API (New) is disabled"),
                ("API_KEY_INVALID secret", "API key invalid")]:
            self.session.post.reset_mock()
            self.run_collection([response({"error": {"message": message}}, status=403)], ["salon", "dentist"])
            self.assertEqual(self.session.post.call_count, 1)
            self.assertIn(diagnostic, self.log.getvalue())
            self.assertNotIn(message, self.log.getvalue())

    def test_ctrl_c_preserves_successful_page(self):
        self.assertEqual(self.run_collection([
            response({"places": [{"id": "a"}], "nextPageToken": "p2"}), KeyboardInterrupt(),
        ]), 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.count(), 2)
        self.assertIn("Interrupted", self.log.getvalue())

    def test_unreadable_success_body_is_not_an_empty_success(self):
        self.output.write_bytes(b"previous CSV")
        self.assertEqual(self.run_collection([response({"places": "invalid"})]), 1)
        self.assertEqual(self.output.read_bytes(), b"previous CSV")

    def test_utc_month_rollover_keeps_history(self):
        self.usage.write_text('{"2026-09":100}')
        with patch("main.utc_now", return_value=datetime(2026, 10, 1, tzinfo=timezone.utc)):
            self.run_collection([response()])
        self.assertEqual(main.read_usage(self.usage), {"2026-09": 100, "2026-10": 1})

    def test_missing_key_does_not_send_or_change_existing_csv(self):
        self.output.write_bytes(b"previous CSV")
        with contextlib.redirect_stdout(self.log):
            code = main.collect("", ["salon"], self.output, self.usage, 60, 10, 100)
        self.assertEqual(code, 1)
        self.session.post.assert_not_called()
        self.assertEqual(self.output.read_bytes(), b"previous CSV")
        self.assertIn("Missing PLACES_API_KEY", self.log.getvalue())

    def test_main_resolves_paths_without_reading_real_env(self):
        # Mock .env loading and getenv: this test never opens the user's real key.
        with patch("main.BASE_DIR", self.output.parent), patch("main.load_dotenv") as load, \
                patch("main.os.getenv", return_value="fake-test-key"), patch("main.collect", return_value=0) as collect:
            self.assertEqual(main.main(), 0)
        load.assert_called_once_with(self.output.parent / ".env", override=False)
        self.assertEqual(collect.call_args.args[2:4], (self.output, self.usage))

    def test_exhausted_month_never_sends_request(self):
        self.usage.write_text(json.dumps({self.month: 100}))
        self.run_collection([])
        self.session.post.assert_not_called()
        self.assertEqual(self.count(), 100)


if __name__ == "__main__":
    unittest.main()
