# -*- coding: utf-8 -*-
"""
TikTok Daily Pipeline v8 - production
- derives monitored accounts from ACTIVE linked TikTok media
- excludes izumi
- discovers new TikTok IDs
- metadata + static-image gate
- requires evidence that 内田もあ is the performer
- matches only a unique, non-generic song/cover title
- AUTO_READY candidates are written to Supabase
Output: TikTok_DailyPipeline_v3_Result.csv
"""
import csv, json, re, subprocess, sys, tempfile
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "TikTok_DailyPipeline_v8_Result.csv"
LEGACY_RETRY = ROOT / "TikTok_DailyPipeline_Retry.csv"
STATE = ROOT / "TikTok_DailyPipeline_State.csv"
SECRET_FILE = ROOT / "TikTok_DailyPipeline.secret"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
FFMPEG = r"F:\SCL\TOOLS\ffmpeg\bin\ffmpeg.exe"
EXCLUDE_HANDLE = "izumi"

# Titles known to be unsafe for automatic substring matching.
GENERIC = {
    "ありがとう","light","Star","スタート","More","now","globe","HERO","ありのまま",
    "BABY","fairy","ライフ","バランス","それだけで","憧れ","塗り絵","ぴっ","足跡",
    "おもかげ","君といた夏","メロディー","littlestars","旅立ち","三日月"
}

URL = "https://anijyugrqffxlnlijtnb.supabase.co"
KEY = "sb_publishable_F5V_mBru9CdnxP4qoYypTQ_P6AG7aZA"

def secret_key():
    if not SECRET_FILE.exists():
        sys.exit("ERROR: C:\\Scripts\\TikTok_DailyPipeline.secret is missing. Run Setup_TikTok_DailyPipeline_Secret_v7.bat first.")
    k = SECRET_FILE.read_text(encoding="utf-8-sig").strip()
    if not k.startswith("sb_secret_"):
        sys.exit("ERROR: secret file does not contain a modern sb_secret_ key.")
    return k

SECRET_KEY = secret_key()

def api_get(table, select, filters=""):
    rows, offset = [], 0
    while True:
        u = f"{URL}/rest/v1/{table}?select={quote(select,safe=',*()')}&limit=1000&offset={offset}"
        if filters:
            u += "&" + filters
        req = Request(u, headers={"apikey": KEY, "Authorization": "Bearer " + KEY})
        with urlopen(req, timeout=60) as r:
            x = json.loads(r.read().decode("utf-8"))
        rows += x
        if len(x) < 1000:
            return rows
        offset += 1000

def api_secret_get(table, select, filters=""):
    u = f"{URL}/rest/v1/{table}?select={quote(select,safe=',*()')}"
    if filters:
        u += "&" + filters
    req = Request(u, headers={"apikey": SECRET_KEY})
    with urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))

def api_post(table, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        f"{URL}/rest/v1/{table}",
        data=data,
        method="POST",
        headers={
            "apikey": SECRET_KEY,
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
    )
    with urlopen(req, timeout=60) as r:
        body = r.read().decode("utf-8")
        return json.loads(body) if body else []

def register_auto_ready(r):
    vid = str(r["video_id"])
    existing = api_secret_get("media_items", "media_id", "platform=eq.tiktok&platform_item_id=eq." + quote(vid))
    if existing:
        media_id = existing[0]["media_id"]
    else:
        payload = {
            "platform": "tiktok",
            "platform_item_id": vid,
            "title": r.get("title") or None,
            "url": r.get("url") or None,
            "media_type": "video",
            "channel_name": r.get("handle") or None,
            "duration_seconds": int(float(r["duration"])) if str(r.get("duration") or "").strip() else None,
            "description": r.get("description") or None,
            "notes": "auto_tiktok_daily_v8"
        }
        ins = api_post("media_items", payload)
        if not ins:
            raise RuntimeError("media_items insert returned no row")
        media_id = ins[0]["media_id"]

    if r["match_type"] == "ORIGINAL":
        table, idcol = "song_media_links", "song_id"
    else:
        table, idcol = "cover_song_media_links", "cover_song_id"

    filt = f"{idcol}=eq.{quote(str(r['match_id']))}&media_id=eq.{media_id}"
    linked = api_secret_get(table, f"{idcol},media_id", filt)
    if not linked:
        api_post(table, {
            idcol: r["match_id"],
            "media_id": media_id,
            "match_status": "matched",
            "match_method": "auto_tiktok_account_title",
            "match_score": 1.0,
            "notes": "auto_tiktok_daily_v8"
        })
    return media_id

def decode_bytes(b):
    for enc in ("utf-8", "cp932"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", "replace")

def run(cmd, timeout=120):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return p.returncode, decode_bytes(p.stdout), decode_bytes(p.stderr)

def norm(s):
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s.casefold()

def title_hits(text, masters):
    nt = norm(text)
    hits = []
    for mid, title in masters:
        if not title or title in GENERIC:
            continue
        n = norm(title)
        if len(n) < 3:
            continue
        if n in nt:
            hits.append((mid, title))
    # remove exact duplicates while preserving order
    seen, out = set(), []
    for x in hits:
        if x not in seen:
            seen.add(x); out.append(x)
    return out

# ---- DB snapshot ----
media = api_get("media_items", "media_id,platform_item_id,url,title,description,channel_name", "platform=eq.tiktok")
orig_links = api_get("song_media_links", "media_id,song_id,match_status")
cover_links = api_get("cover_song_media_links", "media_id,cover_song_id,match_status")
songs = api_get("songs", "song_id,song_title")
covers = api_get("cover_songs", "cover_song_id,song_title")

active = (
    {x["media_id"] for x in orig_links if x.get("match_status") != "exclude"} |
    {x["media_id"] for x in cover_links if x.get("match_status") != "exclude"}
)
known = {str(x.get("platform_item_id") or "") for x in media if x.get("platform_item_id")}

# Persistent state: every classified ID is skipped on later daily runs.
state_known = set()
if STATE.exists():
    try:
        with STATE.open("r", encoding="utf-8-sig", newline="") as sf:
            for r in csv.DictReader(sf):
                vid = str(r.get("video_id") or "").strip()
                decision = str(r.get("decision") or "").strip()
                if vid and decision:
                    state_known.add(vid)
    except Exception as e:
        print("STATE WARNING:", repr(e), flush=True)
known |= state_known

# The legacy technical HOLD queue was explicitly abandoned.
legacy_hold_ids = set()
if LEGACY_RETRY.exists():
    try:
        with LEGACY_RETRY.open("r", encoding="utf-8-sig", newline="") as rf:
            for r in csv.DictReader(rf):
                vid = str(r.get("video_id") or "").strip()
                if vid:
                    legacy_hold_ids.add(vid)
        known |= legacy_hold_ids
        print("LEGACY_HOLD_IGNORED:", len(legacy_hold_ids), flush=True)
    except Exception as e:
        print("LEGACY RETRY WARNING:", repr(e), flush=True)

song_master = [(x["song_id"], x.get("song_title") or "") for x in songs]
cover_master = [(x["cover_song_id"], x.get("song_title") or "") for x in covers]

# Only ACTIVE linked TikTok rows are allowed to seed monitored accounts.
handles = {}
for m in media:
    if m["media_id"] not in active:
        continue
    mm = re.search(r'tiktok\.com/@([^/]+)/video/', m.get("url") or "")
    if mm and EXCLUDE_HANDLE not in mm.group(1).lower():
        handles.setdefault(mm.group(1), m["url"])

print("HANDLES:", len(handles), "KNOWN:", len(known), flush=True)

# ---- discover new IDs ----
new = {}
for hi, (handle, sample) in enumerate(sorted(handles.items()), 1):
    print(f"ACCOUNT [{hi}/{len(handles)}] @{handle}", flush=True)
    rc, so, se = run(["yt-dlp","--user-agent",UA,"--skip-download","--print","%(channel_id)s",sample], 60)
    cid = next((x.strip() for x in so.splitlines() if x.strip() and x.strip() != "NA"), "")
    if not cid:
        print("  HOLD: channel_id unavailable", flush=True)
        continue
    best = []
    for _ in range(3):
        rc, so, se = run(["yt-dlp","--user-agent",UA,"--flat-playlist","--print","%(id)s",f"tiktokuser:{cid}"], 180)
        ids = [x.strip() for x in so.splitlines() if re.fullmatch(r"\d{15,22}", x.strip())]
        if len(ids) > len(best):
            best = ids
    for vid in best:
        if vid not in known:
            new.setdefault(vid, handle)

print("NEW:", len(new), flush=True)

fields = [
    "video_id","handle","url","title","description","uploader","duration",
    "static_check","static_ratio","avg_diff",
    "singer_evidence","performance_evidence",
    "match_type","match_id","match_title","decision","error"
]

counts = {}
with OUT.open("w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()

    for i, (video_id, handle) in enumerate(new.items(), 1):
        url = f"https://www.tiktok.com/@{handle}/video/{video_id}"
        print(f"VIDEO [{i}/{len(new)}] {video_id}", flush=True)
        row = {k: "" for k in fields}
        row.update(video_id=video_id, handle=handle, url=url)

        try:
            rc, so, se = run(
                ["yt-dlp","--user-agent",UA,"--skip-download","--no-warnings","--print-json",url],
                90
            )
            if rc or not so.strip():
                raise RuntimeError((se or so)[-350:])

            j = json.loads(so.splitlines()[-1])
            row["title"] = j.get("title") or ""
            row["description"] = j.get("description") or ""
            row["uploader"] = j.get("uploader") or ""
            row["duration"] = j.get("duration") or ""

            text = " ".join(str(row[k]) for k in ("title","description","uploader"))
            performance = bool(re.search(r"(歌ってみた|うたってみた|弾き語り|歌唱|本人歌唱|LIVE|ライブ|cover|カバー)", text, re.I))
            nonperformance = bool(re.search(r"(BGM|音源.*使|イラスト|人形|告知|出演決定|発売中|写真|スライド)", text, re.I))

            # Singer rule:
            # - official @moa_pi: performance evidence is enough
            # - other accounts: must explicitly mention 内田もあ AND have performance evidence
            is_official = handle.lower() == "moa_pi"
            mentions_moa = "内田もあ" in text
            singer_ok = performance and (is_official or mentions_moa) and not nonperformance
            row["singer_evidence"] = "CONFIRMED" if singer_ok else "AMBIGUOUS"
            row["performance_evidence"] = (
                "PERFORMANCE" if performance and not nonperformance
                else "NONPERFORMANCE" if nonperformance and not performance
                else "REVIEW"
            )

            # ---- static/image gate ----
            with tempfile.TemporaryDirectory(prefix="ttpipe2_", dir=str(ROOT)) as td0:
                td = Path(td0)
                rc, so, se = run(
                    ["yt-dlp","--user-agent",UA,"--no-playlist","-f","bv*+ba/b",
                     "--merge-output-format","mp4","-o",str(td/"item.%(ext)s"),url],
                    180
                )
                files = list(td.glob("item.*"))
                imgs = [p for p in files if p.suffix.lower() in (".jpg",".jpeg",".png",".webp",".avif")]
                vids = [p for p in files if p.suffix.lower() in (".mp4",".webm",".mkv",".mov")]

                if imgs and not vids:
                    row["static_check"] = "STATIC_IMAGE_POST"
                    row["decision"] = "EXCLUDE_STATIC"
                elif not vids:
                    row["static_check"] = "ERROR"
                    row["decision"] = "HOLD"
                    row["error"] = "no video/image download"
                else:
                    frames = td / "frames"
                    frames.mkdir()
                    rc, so, se = run(
                        [FFMPEG,"-hide_banner","-loglevel","error","-i",str(vids[0]),
                         "-vf","fps=1/2,scale=160:-2,format=gray","-frames:v","60",
                         str(frames/"f_%04d.pgm")],
                        120
                    )
                    fr = sorted(frames.glob("*.pgm"))

                    def pix(p):
                        b = p.read_bytes()
                        parts = b.split(b"\n", 3)
                        return parts[3] if len(parts) > 3 else b

                    if len(fr) < 2:
                        row["static_check"] = "ERROR"
                        row["decision"] = "HOLD"
                        row["error"] = "insufficient sampled frames"
                    else:
                        px = [pix(p) for p in fr]
                        diffs = [
                            sum(abs(a-b) for a,b in zip(px[k-1],px[k])) / max(1,len(px[k]))
                            for k in range(1,len(px))
                        ]
                        ratio = sum(x <= 1.25 for x in diffs) / len(diffs)
                        avg = sum(diffs) / len(diffs)
                        row["static_ratio"] = f"{ratio:.4f}"
                        row["avg_diff"] = f"{avg:.4f}"
                        row["static_check"] = "STATIC" if ratio >= .92 and avg <= 1.875 else "MOTION"

                        if row["static_check"] == "STATIC":
                            row["decision"] = "EXCLUDE_STATIC"
                        elif nonperformance:
                            row["decision"] = "EXCLUDE_NONPERFORMANCE"
                        elif not singer_ok:
                            row["decision"] = "EXCLUDE_SINGER_AMBIGUOUS"
                        else:
                            sh = title_hits(text, song_master)
                            ch = title_hits(text, cover_master)
                            all_hits = [("ORIGINAL",)+x for x in sh] + [("COVER",)+x for x in ch]

                            # Auto-ready only for one unambiguous DB title.
                            if len(all_hits) == 1:
                                typ, mid, mtitle = all_hits[0]
                                promo_only = bool(re.search(
                                    r"(new album|ニューアルバム|release|coming soon|チケット|ライブ開催|発売)",
                                    text, re.I)) and not bool(re.search(
                                    r"(歌ってみた|うたってみた|弾き語り|歌唱|配信切り抜き|ライブより|liveより)",
                                    text, re.I))
                                cover_context = bool(re.search(
                                    r"(cover|カバー|歌ってみた|うたってみた|弾き語り|歌唱)",
                                    text, re.I))
                                original_context = (
                                    is_official or
                                    bool(re.search(r"(作詞作曲|オリジナル|original)", text, re.I))
                                )
                                risky_context_titles = {"未来へ","言葉","u..."}
                                if mtitle in risky_context_titles:
                                    nt2 = norm(text)
                                    explicit = (
                                        ("#" + mtitle.casefold()) in nt2 or
                                        bool(re.search(re.escape(mtitle) + r".{0,20}(cover|カバー|歌ってみた|うたってみた)", text, re.I)) or
                                        bool(re.search(r"(cover|カバー).{0,20}" + re.escape(mtitle), text, re.I))
                                    )
                                else:
                                    explicit = True
                                if promo_only:
                                    row["decision"] = "EXCLUDE_NONPERFORMANCE"
                                elif not explicit:
                                    row["decision"] = "HOLD_SONG_AMBIGUOUS"
                                elif typ == "COVER" and not cover_context:
                                    row["decision"] = "HOLD_SONG_AMBIGUOUS"
                                elif typ == "ORIGINAL" and not original_context:
                                    row["decision"] = "HOLD_SONG_AMBIGUOUS"
                                else:
                                    row["match_type"] = typ
                                    row["match_id"] = mid
                                    row["match_title"] = mtitle
                                    row["decision"] = "AUTO_READY_" + typ
                            elif len(all_hits) == 0:
                                row["decision"] = "HOLD_SONG_UNKNOWN"
                            else:
                                row["decision"] = "HOLD_SONG_AMBIGUOUS"

        except Exception as e:
            row["decision"] = "HOLD"
            row["error"] = repr(e)

        counts[row["decision"]] = counts.get(row["decision"], 0) + 1
        w.writerow(row)
        f.flush()

text = OUT.read_text(encoding="utf-8-sig")
if "\ufffd" in text:
    sys.exit("ERROR: mojibake detected")

# Register AUTO_READY rows, then persist every classified ID.
state_rows = {}
if STATE.exists():
    try:
        with STATE.open("r", encoding="utf-8-sig", newline="") as sf:
            for r in csv.DictReader(sf):
                vid = str(r.get("video_id") or "").strip()
                if vid:
                    state_rows[vid] = r
    except Exception as e:
        print("STATE MERGE WARNING:", repr(e), flush=True)

if LEGACY_RETRY.exists():
    try:
        with LEGACY_RETRY.open("r", encoding="utf-8-sig", newline="") as rf:
            for r in csv.DictReader(rf):
                vid = str(r.get("video_id") or "").strip()
                if vid and vid not in state_rows:
                    r["decision"] = "ABANDONED_TECHNICAL_HOLD"
                    state_rows[vid] = r
    except Exception as e:
        print("LEGACY STATE MIGRATION WARNING:", repr(e), flush=True)

registered = []
with OUT.open("r", encoding="utf-8-sig", newline="") as rf:
    for r in csv.DictReader(rf):
        vid = str(r.get("video_id") or "").strip()
        decision = str(r.get("decision") or "").strip()
        if not vid:
            continue
        if decision in ("AUTO_READY_ORIGINAL", "AUTO_READY_COVER"):
            try:
                media_id = register_auto_ready(r)
                registered.append((r["match_type"], r["match_title"], vid, media_id))
            except Exception as e:
                print("DB_WRITE_ERROR:", vid, repr(e), flush=True)
                # Do not mark as processed; next daily run will retry this candidate.
                continue
        state_rows[vid] = r

with STATE.open("w", encoding="utf-8-sig", newline="") as sf:
    sw = csv.DictWriter(sf, fieldnames=fields)
    sw.writeheader()
    for vid in sorted(state_rows):
        sw.writerow({k: state_rows[vid].get(k, "") for k in fields})

if "\\ufffd" in STATE.read_text(encoding="utf-8-sig"):
    sys.exit("ERROR: mojibake detected in state")

print("DONE:", OUT, flush=True)
print("STATE:", STATE, "PROCESSED:", len(state_rows), flush=True)
for k in sorted(counts):
    print(f"{k}: {counts[k]}", flush=True)
print("DB_REGISTERED:", len(registered), flush=True)
for typ, title, vid, media_id in registered:
    print(f"REGISTERED: {typ} | {title} | media_id={media_id} | https://www.tiktok.com/video/{vid}", flush=True)
