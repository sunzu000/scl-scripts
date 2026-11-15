"""Non-destructive, optional Supabase opening-phrase evidence report.

This module never edits playlist.json, media, source recordings, or Supabase.
It only creates <LIVE_folder>/song_opening_candidates.json for human review.
Private opening data is fetched with an existing *local secret* and never logged.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import tempfile
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import urlsplit

class _NoRedirect(HTTPRedirectHandler):
    """Prevent forwarding private API keys through unexpected HTTP redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


DEFAULT_PROJECT_URL = "https://anijyugrqffxlnlijtnb.supabase.co"
DEFAULT_SECRET_PATH = Path(r"C:\Scripts\TikTok_DailyPipeline.secret")
REPORT_FILENAME = "song_opening_candidates.json"


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(c for c in value if c.isalnum())


def read_local_key() -> str:
    key = (os.getenv("SUPABASE_SECRET_KEY") or "").strip()
    if key:
        return key
    try:
        if DEFAULT_SECRET_PATH.is_file():
            return DEFAULT_SECRET_PATH.read_text(encoding="utf-8-sig").strip()
    except OSError:
        pass
    return ""


def fetch_openings(*, key: str | None = None, url: str | None = None, timeout: float = 8.0) -> list[dict]:
    """RPC is SECURITY INVOKER and service_role-only on the server."""
    credential = key if key is not None else read_local_key()
    if not credential:
        raise RuntimeError("secret_not_configured")
    base_url = (url or os.getenv("SUPABASE_URL") or DEFAULT_PROJECT_URL).rstrip("/")
    parts = urlsplit(base_url)
    if (parts.scheme != "https" or parts.hostname != "anijyugrqffxlnlijtnb.supabase.co"
            or parts.port is not None or parts.username or parts.password
            or parts.path or parts.query or parts.fragment):
        raise RuntimeError("invalid_supabase_url")
    req = Request(
        base_url + "/rest/v1/rpc/internal_song_openings_v1",
        data=b"{}", method="POST",
        headers={"apikey": credential, "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
            rows = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError("rpc_http_" + str(exc.code)) from None
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("rpc_transport_failure") from None
    if not isinstance(rows, list):
        raise RuntimeError("unexpected_rpc_response")
    return [r for r in rows if isinstance(r, dict) and r.get("song_id")
            and r.get("opening_phrase") and r.get("verification_status") in ("verified", "candidate")]


def _score(anchor: str, segment: str, verified: bool) -> float:
    if not anchor or not segment:
        return 0.0
    if anchor in segment:
        return 1.0
    # Fuzzy matching is limited to user-verified entries.  ASR-derived phrases
    # are evidence candidates, and must match literally before being reported.
    if not verified or len(anchor) < 8:
        return 0.0
    length = len(anchor)
    max_len = max(1, len(segment) - length + 1)
    return max((SequenceMatcher(None, anchor, segment[i:i + length]).ratio()
                for i in range(max_len)), default=0.0)


def find_opening_candidates(segments: list[dict], rows: list[dict], *, max_hits: int = 200) -> dict:
    """Return candidates only. Never mark any match as a confirmed song."""
    normalized = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start"))
        except (ValueError, TypeError):
            continue
        if start < 0:
            continue
        normalized.append((start, normalize(seg.get("text", "")), str(seg.get("text", ""))))
    normalized.sort(key=lambda x: x[0])
    results = []
    short_skipped = 0
    for row in rows:
        anchor = normalize(row.get("opening_phrase", ""))
        status = row.get("verification_status")
        title = str(row.get("song_title") or "")
        n_title = normalize(title)
        if len(anchor) < 3:
            short_skipped += 1
            continue
        last = -100000.0
        for i, (start, text_norm, text_raw) in enumerate(normalized):
            score = _score(anchor, text_norm, verified=(status == "verified"))
            if score < (0.90 if status == "verified" else 1.00):
                continue
            if start - last < 180:
                continue
            title_context = len(n_title) >= 4 and any(
                n_title in earlier_text and 0 <= start - t0 <= 120
                for t0, earlier_text, _ in normalized[max(0, i - 28):i]
            )
            # A very short phrase is ambiguous unless a separate title intro
            # occurred nearby.  Short matches by themselves are not actionable.
            if len(anchor) < 8 and not title_context:
                short_skipped += 1
                continue
            results.append({
                "song_id": str(row["song_id"]),
                "song_title": title,
                "at_seconds": round(start, 2),
                "matched_text": text_raw[:120],
                "match_score": round(score, 3),
                "verification_status": status,
                "title_mentioned_within_120s": bool(title_context),
                "review_priority": "title_context_present" if title_context else "opening_only_low_confidence",
                "result_status": "candidate_needs_review",
            })
            last = start
    results.sort(key=lambda r: (r["at_seconds"], r["song_id"]))
    return {"matches": results[:max_hits], "total_matches_before_cap": len(results),
            "skipped_ambiguous_short_matches": short_skipped, "catalog_rows": len(rows)}


def write_candidate_report(folder: str | Path, segments: list[dict], *, opener=None) -> dict:
    """Best-effort report for a LIVE folder; any network error is nonfatal."""
    target_folder = Path(folder)
    target_folder.mkdir(parents=True, exist_ok=True)
    report = {"kind": "opening_phrase_matching_aid", "non_destructive": True,
              "generated_at": datetime.now(timezone.utc).isoformat(),
              "status": "skipped", "matches": []}
    try:
        rows = fetch_openings() if opener is None else opener()
        report.update(find_opening_candidates(segments, rows))
        report["status"] = "candidates_ready"
    except RuntimeError as exc:
        # No access tokens, values, or server responses are included in errors.
        report["reason"] = str(exc)[:90]
    except Exception:
        report["reason"] = "opening_lookup_failed"
    target = target_folder / REPORT_FILENAME
    # Do not replace a useful prior result with a skipped report when offline.
    if report["status"] == "skipped" and target.exists():
        return report
    fd, temp_path = tempfile.mkstemp(prefix=".song_opening_candidates_", suffix=".json", dir=target_folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        os.replace(temp_path, target)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate review-only song opening candidates")
    parser.add_argument("transcript", type=Path, help="Existing timestamped transcript.json")
    arguments = parser.parse_args()
    data = json.loads(arguments.transcript.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = data.get("segments", [])
    if not isinstance(data, list):
        raise ValueError("Expected timestamped transcript list")
    report = write_candidate_report(arguments.transcript.parent, data)
    print("Opening phrase evidence:", report["status"], len(report["matches"]), "matches")


if __name__ == "__main__":
    main()
