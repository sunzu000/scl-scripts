# -*- coding: utf-8 -*-
import json
import logging
import os
import re
from pathlib import Path

def _load_segments(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, list):
        return data, data
    if isinstance(data, dict):
        for key in ("segments", "transcript", "items"):
            if isinstance(data.get(key), list):
                return data[key], data
    raise ValueError(f"Unsupported transcript.json structure: {path}")

def _plain_text(segments):
    return "\n".join(
        str(s.get("text") or "").strip()
        for s in segments
        if isinstance(s, dict) and str(s.get("text") or "").strip()
    )

def _duration(segments):
    ends = []
    for s in segments:
        if not isinstance(s, dict):
            continue
        try:
            ends.append(float(s.get("end")))
        except (TypeError, ValueError):
            pass
    return max(ends) if ends else None

def _date8(value):
    raw = re.sub(r"\D", "", str(value or ""))
    if len(raw) != 8:
        raise ValueError(f"Invalid LIVE date: {value!r}")
    return raw

def sync_transcript_to_supabase(folder, date, video=None, whisper_model=None):
    """Upload transcript.json to tiktok_lives and video_transcripts.

    Idempotent:
      tiktok_lives         -> upsert by live_date
      video_transcripts    -> upsert by (live_id, source_file)
    """
    import requests

    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = os.getenv("SUPABASE_SECRET_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SECRET_KEY is not configured")

    folder = Path(folder).resolve()
    transcript_path = folder / "transcript.json"
    if not transcript_path.is_file():
        logging.info("Transcript sync skipped: %s does not exist", transcript_path)
        return False

    raw_date = _date8(date)
    live_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
    segments, raw_json = _load_segments(transcript_path)
    text = _plain_text(segments)
    duration_seconds = _duration(segments)

    if video:
        source_file = Path(video).name
    else:
        expected = folder / f"{raw_date}_PC.mp4"
        source_file = expected.name

    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    }

    # Preserve existing LIVE information. Only merge transcript-related fields.
    live_payload = {
        "live_date": live_date,
        "source_folder": str(folder),
        "transcript_path": str(transcript_path),
        "transcript_text": text or None,
        "transcript_json": raw_json,
        "transcript_model": whisper_model,
    }
    live_payload = {k: v for k, v in live_payload.items() if v is not None}

    h = {**headers, "Prefer": "resolution=merge-duplicates,return=representation"}
    r = requests.post(
        url + "/rest/v1/tiktok_lives?on_conflict=live_date",
        headers=h, json=live_payload, timeout=90)
    if not r.ok:
        raise RuntimeError(f"tiktok_lives upsert HTTP {r.status_code}: {r.text[:500]}")
    rows = r.json()
    if not rows:
        raise RuntimeError("tiktok_lives upsert returned no row")
    live_id = rows[0]["live_id"]

    vt_payload = {
        "live_id": live_id,
        "source_file": source_file,
        "source_type": "tiktok_live",
        "duration_seconds": duration_seconds,
        "transcript_text": text or None,
        "transcript_json": raw_json,
        "whisper_model": whisper_model,
    }
    vt_payload = {k: v for k, v in vt_payload.items() if v is not None}

    r = requests.post(
        url + "/rest/v1/video_transcripts?on_conflict=live_id,source_file",
        headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=vt_payload, timeout=90)
    if not r.ok:
        raise RuntimeError(f"video_transcripts upsert HTTP {r.status_code}: {r.text[:500]}")

    logging.info(
        "Transcript sync completed: %s / live_id=%s / %d segments / %d chars",
        live_date, live_id, len(segments), len(text))
    return True
