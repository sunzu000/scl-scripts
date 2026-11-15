# Song opening candidates (internal / trial)

This is a **non-destructive matching aid** for PC94/PC4's TikTok LIVE transcripts. It is not an automatic song registration/confirmation feature.

- Supabase private table: `private.song_opening_phrases` (original compositions only).
- Read-only API: `public.internal_song_openings_v1()` with `SECURITY INVOKER`; `EXECUTE` granted **only** to `service_role`; anonymous and authenticated users cannot call it.
- Local credentials: environment `SUPABASE_SECRET_KEY`, or the *existing* `C:\Scripts\TikTok_DailyPipeline.secret`. Do not put any key in Git or transcripts.
- Local output: `<date>_LIVE/song_opening_candidates.json` (same LIVE folder, no files in WORK root).
- **Never modifies** `playlist.json`, recording files, clips, digest videos, or Supabase rows. All hits remain `candidate_needs_review`, including matches to `verified` phrase rows.
- Whisper-generated `candidate` phrases require exact normalized matches; user-verified phrases may match with a conservative threshold. The output includes `review_priority`: missing recent title context is `opening_only_low_confidence` and must not be treated as a valid song identification. Very short phrases require a title mention just before the hit.
- A failed/missing connection results in a `skipped` report with an error category, without failing the existing digest workflow.

## Standalone check

From `C:\Scripts` (after installing this feature):

```powershell
python song_opening_candidates.py "F:\SCL\TikTok\2026\202611\20261115_LIVE\transcript.json"
```

Use a path to an **existing** LIVE recording folder; the example folder/date might not exist. The report uses existing timestamped `transcript.json` only and does not re-transcribe or render anything.

Run unit tests without credentials:

```powershell
python -m unittest -v test_song_opening_candidates.py
```

Integration with `digest_pipeline.py` is **not implemented** in this draft. The standalone tool is report-only; **do not merge into the PC94 mainline before validating a real LIVE report on PC4**. Existing auto-detection (`digest_config.json`, `detect_songs`) and `playlist.json` behavior remain untouched. On preexisting completed LIVE recordings, call the helper standalone; do not re-run the full rendering pipeline just to generate this report.

## Security checks and limits

- Redirects are **disabled** on the credentialed RPC request. A redirect is handled as a nonfatal HTTP error, never followed.
- Unit tests use **fictional placeholder phrases only**; no actual private opening lyrics are embedded in public GitHub source.
- Offline database inspection of 12 Whisper transcript files across six dates found 18 literal matches, but only 10 had a recent title mention; these are review hints, not ground truth. Some remaining matches overlap differently named LIVE playlist entries.
- The client cannot verify actual PC4 access to the RPC, because the PC4 secret and filesystem are not accessible here. A credentialed PC4 test is required before merge.
