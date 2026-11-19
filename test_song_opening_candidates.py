"""Purely synthetic tests; never commit private opening-phrase values."""
import unittest
from pathlib import Path
import tempfile
import json
from song_opening_candidates import find_opening_candidates, normalize, write_candidate_report


class OpeningCandidateTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(normalize(" Example ALPHA! "), "examplealpha")
        self.assertEqual(normalize("か り の！？"), "かりの")

    def test_candidate_match_requires_literal_phrase(self):
        rows = [{"song_id":"X01", "song_title":"架空曲テスト",
                 "opening_phrase":"検証用の架空フレーズ", "verification_status":"candidate"}]
        segs = [{"start":10.0,"text":"では架空曲テストを始めます"},
                {"start":22.0,"text":"検証用の架空フレーズ テスト用の内容です"}]
        out = find_opening_candidates(segs, rows)
        self.assertEqual(len(out["matches"]), 1)
        self.assertTrue(out["matches"][0]["title_mentioned_within_120s"])
        self.assertEqual(out["matches"][0]["review_priority"], "title_context_present")
        self.assertEqual(out["matches"][0]["result_status"], "candidate_needs_review")

    def test_short_verified_without_title_does_not_report(self):
        rows = [{"song_id":"X02", "song_title":"架空曲乙", "opening_phrase":"仮の語",
                 "verification_status":"verified"}]
        self.assertEqual(find_opening_candidates([{"start":22,"text":"仮の語の説明をします"}],rows)["matches"],[])

    def test_short_verified_with_title_does_report(self):
        rows = [{"song_id":"X02", "song_title":"架空曲乙", "opening_phrase":"仮の語",
                 "verification_status":"verified"}]
        segs = [{"start":20,"text":"架空曲乙という曲です"},{"start":29,"text":"仮の語"}]
        self.assertEqual(len(find_opening_candidates(segs,rows)["matches"]),1)

    def test_unverified_fuzzy_not_allowed(self):
        rows = [{"song_id":"X03", "song_title":"架空曲丙", "opening_phrase":"架空の検証用フレーズ",
                 "verification_status":"candidate"}]
        self.assertEqual(find_opening_candidates([{"start":5,"text":"架空の検証用フレース"}],rows)["matches"],[])

    def test_without_title_context_is_low_priority(self):
        rows = [{"song_id":"X04", "song_title":"架空曲丁", "opening_phrase":"別の検証用長文フレーズ",
                 "verification_status":"candidate"}]
        out = find_opening_candidates([{"start":12,"text":"別の検証用長文フレーズ"}],rows)
        self.assertEqual(out["matches"][0]["review_priority"], "opening_only_low_confidence")
        self.assertFalse(out["matches"][0]["title_mentioned_within_120s"])

    def test_secret_cannot_be_sent_to_other_host(self):
        from song_opening_candidates import fetch_openings
        with self.assertRaisesRegex(RuntimeError, "invalid_supabase_url"):
            fetch_openings(key="synthetic_dummy", url="https://example.net")

    def test_redirect_is_disabled(self):
        from song_opening_candidates import _NoRedirect
        self.assertIsNone(_NoRedirect().redirect_request(None,None,302,"found",None,"https://example.net"))

    def test_report_no_network_with_injected_rows(self):
        with tempfile.TemporaryDirectory() as td:
            report = write_candidate_report(td, [{"start":42,"text":"架空テスト専用の長い文言"}],
                opener=lambda:[{"song_id":"X05","song_title":"仮テスト曲",
                               "opening_phrase":"架空テスト専用の長い文言","verification_status":"candidate"}])
            self.assertEqual(report["status"],"candidates_ready")
            loaded = json.loads((Path(td)/"song_opening_candidates.json").read_text(encoding="utf-8"))
            self.assertTrue(loaded["non_destructive"])
            self.assertEqual(len(loaded["matches"]),1)

    def test_failed_rpc_does_not_overwrite_prior_report(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)/"song_opening_candidates.json"
            output.write_text('{"keep":true}',encoding="utf-8")
            def bad_opener():
                raise RuntimeError("rpc_transport_failure")
            result = write_candidate_report(td,[],opener=bad_opener)
            self.assertEqual(result["status"],"skipped")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")),{"keep":True})

    def test_playlist_not_created_by_report(self):
        with tempfile.TemporaryDirectory() as td:
            write_candidate_report(td,[],opener=lambda:[])
            self.assertFalse((Path(td)/"playlist.json").exists())

if __name__ == "__main__": unittest.main()
