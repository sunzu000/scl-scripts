# -*- coding: utf-8 -*-
import argparse,csv,json,re,statistics,subprocess,sys,hashlib,datetime
from pathlib import Path

ROOT=Path(r"F:\SCL\TikTok")
FF=Path(r"F:\SCL\TOOLS\ffmpeg\bin\ffmpeg.exe")
HERE=Path(__file__).resolve().parent
MC_KEEP=4.0

def norm(s):
    return re.sub(r"[\s　・･._\-〜～]+","",(s or "").strip().lower())

ALIASES={
 norm("アシアト"):norm("足跡"),
 norm("ダーリンBBB"):norm("Darling BBB"),
 norm("ダーリンb.b.b."):norm("Darling BBB"),
 norm("Star Heart Yellow Autumn"):norm("StarHeart～yellowredautumn～"),
}
def key(s):
    n=norm(s); return ALIASES.get(n,n)

teacher=json.loads((HERE/"teacher_boundaries.json").read_text(encoding="utf-8"))
MASTER=json.loads((HERE/"master_performance_seconds.json").read_text(encoding="utf-8"))
MASTER={key(k):float(v) for k,v in MASTER.items()}
TEACH={}
for x in teacher:
    TEACH.setdefault(key(x["title"]),[]).append(float(x["duration"]))

STRONG=("でした","でしたー","でした!","でした！","歌わせていただきました")
THANK=("ありがとう","ありがと","ナイス")
CONV=("リクエスト","コメント","ギフト","フォロー","次の","次は","みんな","ライブ")

def load(p): return json.loads(p.read_text(encoding="utf-8-sig"))

def playlist_file(folder):
    p=folder/"playlist.json"
    if p.exists(): return p
    a=sorted(folder.glob("playlist*.json")); return a[0] if a else None
def transcript_file(folder):
    p=folder/"transcript.json"
    if p.exists(): return p
    a=sorted(folder.glob("transcript*.json")); return a[0] if a else None

def marker_score(txt):
    s=0
    if any(x in txt for x in STRONG): s+=8
    if any(x in txt for x in THANK): s+=4
    if any(x in txt for x in CONV): s+=2
    if len(txt)>=14:s+=1
    return s

def follow_score(segs,i,window=9):
    st=float(segs[i].get("start",0)); n=0; chars=0
    for s in segs[i:]:
        if float(s.get("start",0))>st+window:break
        t=(s.get("text") or "").strip()
        if t:n+=1;chars+=len(t)
    return (1 if n>=2 else 0)+(1 if n>=3 else 0)+(1 if chars>=20 else 0)

def detect(title,start,next_start,segs):
    k=key(title)
    vals=TEACH.get(k,[])
    upper=(next_start-2) if next_start is not None else start+720
    if k in MASTER:
        # Verified master is the strongest prior: performance end + 4 sec MC.
        target=start+MASTER[k]+MC_KEEP
        return min(target,upper),"medium","master_duration_estimate_unverified",None,"",MASTER[k]+MC_KEEP
    if vals:
        # v4: repeated performances may have different accepted durations.
        # Pick among accepted durations only; transcript may choose the matching
        # performance variant, but can no longer move the end outside teacher data.
        med=statistics.median(vals)
        anchors=[start+v for v in vals]
        evidence=[]
        for i,s in enumerate(segs):
            ss=float(s.get("start",0)); txt=(s.get("text") or "").strip()
            if ss < min(anchors)-10 or ss > min(upper,max(anchors)+10): continue
            ms=marker_score(txt)
            fs=follow_score(segs,i)
            if ms:
                evidence.append((ms+fs,ss,txt))
        if evidence:
            # v5: choose a teacher variant only when transcript evidence is genuinely
            # close to that accepted boundary. Otherwise use the median accepted duration.
            ranked=[]
            for a in anchors:
                near=min(evidence,key=lambda e:abs(e[1]-a))
                dist=abs(near[1]-a); score=near[0]
                ranked.append((dist,-score,a,near))
            ranked.sort(key=lambda x:(x[0],x[1]))
            dist,negscore,chosen,near=ranked[0]
            if dist <= 5.0:
                return min(chosen,upper),"medium","teacher_variant_estimate_unverified",near[1],near[2],chosen-start
        # Ambiguous MC: use robust center of accepted performances.
        chosen=start+med
        return min(chosen,upper),"medium","teacher_median_estimate_unverified",None,"",med

    # Unknown title: search later half, prefer sustained post-song conversation.
    lo=start+120
    c=[]
    for i,s in enumerate(segs):
        ss=float(s.get("start",0)); txt=(s.get("text") or "").strip()
        if ss<lo or ss>upper:continue
        ms=marker_score(txt); fs=follow_score(segs,i)
        if ms and fs>=2:c.append((ms+fs,ss,txt))
    if c:
        # For unknown songs, choose the first strong sustained conversation,
        # not the lexically strongest later one.
        c.sort(key=lambda x:x[1])
        score,mc,txt=c[0]
        return min(mc+MC_KEEP,upper),"medium","unknown_sustained_mc",mc,txt,None
    if next_start is not None:
        return max(start+60,next_start-8),"low","next_song_safe_fallback",None,"",None
    return start+300,"low","last_song_fallback",None,"",None

def source(folder,date):
    for n in (f"{date}_PC.mp4",f"{date}_HB.mp4"):
        p=folder/n
        if p.exists():return p
    a=[p for p in folder.glob("*.mp4") if "digest" not in p.name.lower()
       and "song" not in p.name.lower() and "review" not in p.name.lower()]
    return max(a,key=lambda p:p.stat().st_size) if a else None

def safe(s):return re.sub(r'[\\/:*?"<>|]','_',s).strip()

def analyze(folder,render=False):
    date=folder.name[:8]; pp=playlist_file(folder); tp=transcript_file(folder)
    if not pp or not tp:raise FileNotFoundError("playlist.json or transcript.json missing: "+str(folder))
    pl=load(pp); tr=load(tp)
    if isinstance(tr,dict):tr=tr.get("segments",[])
    if not isinstance(pl,list) or not pl:
        raise ValueError("Playlist empty or malformed: "+str(pp))
    if not isinstance(tr,list):
        raise ValueError("Transcript malformed: "+str(tp))
    previous=-1
    for item in pl:
        if not isinstance(item,dict) or "start" not in item or "title" not in item:
            raise ValueError("Playlist entry missing title/start")
        st=float(item["start"])
        if st<=previous or st<0:
            raise ValueError("Playlist starts not strictly increasing")
        previous=st
    src=source(folder,date) if render else None
    # Fail closed: legacy automatic invocation must never render unreviewed boundaries.
    if render:
        raise RuntimeError("Full-song v7 automatic render blocked: manual boundary review required. Run --date YYYYMMDD for report only.")
    rows=[]
    for i,x in enumerate(pl):
        st=float(x["start"]); nxt=float(pl[i+1]["start"]) if i+1<len(pl) else None
        en,conf,method,mc,txt,med=detect(x.get("title",""),st,nxt,tr)
        row={"date":date,"song_no":i+1,"title":x.get("title",""),"start":round(st,3),
             "end":round(en,3),"duration":round(en-st,3),"confidence":conf,"method":method,
             "mc_start":"" if mc is None else round(mc,3),
             "teacher_median":"" if med is None else round(med,3),"mc_text":txt}
        if en <= st:
            raise ValueError("Invalid end <= start for "+str(x.get("title")))
        rows.append(row)
        if render:
            if src is None:raise FileNotFoundError("source MP4 not found")
            outdir=folder/"song_clips"
            outdir.mkdir(exist_ok=True)
            dst=outdir/f"{date}_song{i+1:02d}_{safe(x.get('title',''))}.mp4"
            # IMPORTANT: never overwrite confirmed/existing clips.
            if dst.exists():
                print("[SKIP existing]",dst.name);continue
            cmd=[str(FF),"-hide_banner","-loglevel","error","-y","-ss",f"{st:.3f}","-i",str(src),
                 "-t",f"{en-st:.3f}","-c:v","libx264","-preset","veryfast","-crf","20",
                 "-c:a","aac","-b:a","192k","-movflags","+faststart",str(dst)]
            subprocess.run(cmd,check=True)
    return rows

def folders(date):
    if date:
        p=ROOT/date[:4]/date[:6]/f"{date}_LIVE";return [p] if p.exists() else []
    return sorted(ROOT.glob(r"20??\20????\????????_LIVE"))

def write(rows,p):
    fs=["date","song_no","title","start","end","duration","confidence","method","mc_start","teacher_median","mc_text"]
    with p.open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=fs);w.writeheader();w.writerows(rows)

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--date");ap.add_argument("--all",action="store_true");ap.add_argument("--render",action="store_true")
    a=ap.parse_args()
    if not a.date and not a.all:ap.error("--date or --all")
    rows=[]
    for f in folders(a.date):rows+=analyze(f,a.render)
    # WORK_CLOSURE_20261113_REPORT_POLICY: approved date reports belong to source LIVE.
    today=datetime.date.today()
    if a.date:
        report_day=datetime.datetime.strptime(a.date,"%Y%m%d").date()
        live_report_dir=Path(r"F:\SCL\TikTok")/report_day.strftime("%Y")/report_day.strftime("%Y%m")/(a.date+"_LIVE")
        if not live_report_dir.is_dir():
            raise FileNotFoundError("LIVE folder missing for daily report: "+str(live_report_dir))
        rp=live_report_dir/(f"{a.date}_full_song_auto_report_v7.csv")
    else:
        # ALL-mode summary is disposable; create its working directory if needed.
        work_report_dir=Path(r"C:\SCL\WORK\TikTok\FullSongReports")/today.strftime("%Y")/today.strftime("%Y%m")/today.strftime("%Y%m%d")
        work_report_dir.mkdir(parents=True,exist_ok=True)
        rp=work_report_dir/"full_song_auto_report_ALL_v7.csv"
    if rp.exists():
        rp=rp.with_name(rp.stem+"_"+datetime.datetime.now().strftime("%H%M%S_%f")+rp.suffix)
    write(rows,rp)
    print("[REPORT]",rp);print("[SUMMARY]",len(rows),"songs; low=",sum(r["confidence"]=="low" for r in rows))
if __name__=="__main__":main()
