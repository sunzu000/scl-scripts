# -*- coding: utf-8 -*-
"""Conservative LIVE media cleanup. Protect originals, results, all metadata.

- Top-level MP4s with the same byte size and modification timestamp as YYYYMMDD_PC/HB.mp4 are deduplicated (no hashing).
- Generated work/overlay_work/preview_work media and subtitles are removed
  only after a validated existing final digest AND successful processing state.
- Never delete candidate user clips/digests, originals, .part, folders outside LIVE,
  unrecognized files, or files reached through junctions/symlinks.
- Historical scan/apply: --all [--apply]. Automatic postprocess: --date YYYYMMDD --after-process --apply.
"""
from __future__ import annotations
import argparse
import csv
import datetime
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

LIVE_ROOT = Path(r'F:\SCL\TikTok')
AUDIT_ROOT = Path(r'C:\Scripts\Backup\TikTokLiveCleanup\reports')
DATE_RE = re.compile(r'^20\d{6}_LIVE$', re.I)
# Names of directories written by the known digest pipeline, NOT the permanent song_clips folder.
TEMP_DIRS = {'work','overlay_work','preview_work'}
FILE_GENERATED = re.compile(r'(?i)^(?:(?:part|normal|clip)_\d{4}\.mp4|list_\d{4}\.ass|(?:parts|normal|clips)\.txt)$')
# Direct-copy sources in the LIVE folder are candidates only if size and last-modified time match canonical PC/HB.
PROTECT_NAME = re.compile(r'(?i)(?:digest|song[_-]?clips|review|preview|published|final|full[_-]?song|thumbnail|cover)')


def is_link(p: Path) -> bool:
    try:
        if p.is_symlink(): return True
        return bool(p.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) if os.name=='nt' else False
    except (OSError, AttributeError):
        return True


def valid_dates(root):
    if not root.is_dir() or is_link(root): raise RuntimeError('INVALID_LIVE_ROOT '+str(root))
    for year in sorted(root.iterdir()):
        if not year.is_dir() or is_link(year) or not re.fullmatch(r'20\d\d',year.name):continue
        for ym in sorted(year.iterdir()):
            if not ym.is_dir() or is_link(ym) or not re.fullmatch(r'20\d{4}',ym.name):continue
            if not ym.name.startswith(year.name):continue
            for item in sorted(ym.iterdir()):
                if not item.is_dir() or is_link(item) or not DATE_RE.fullmatch(item.name):continue
                if not item.name.startswith(ym.name):continue
                yield item


def check_successful_final(folder, date):
    s=folder/'state.json'; digest=folder/(date+'_digest.mp4')
    if not s.is_file() or is_link(s) or not digest.is_file() or is_link(digest):return (False,'NO_SUCCESS_STATE_OR_LOCAL_DIGEST')
    try:
        state=json.loads(s.read_text(encoding='utf-8-sig'))
        if not str(state.get('status','')).startswith(('completed','done','success')):return (False,'STATE_NOT_COMPLETED')
        if digest.stat().st_size<2048:return (False,'FINAL_TOO_SMALL')
        ff=Path(r'F:\SCL\TOOLS\ffmpeg\bin\ffprobe.exe')
        if not ff.is_file():return (False,'FFPROBE_MISSING')
        z=subprocess.run([str(ff),'-v','error','-show_entries','format=duration','-of','default=nk=1:nw=1',str(digest)],capture_output=True,text=True,timeout=30)
        if z.returncode or float(z.stdout.strip())<2.0:return (False,'DIGEST_VALIDATION_FAILED')
        return (True,'FINAL_VERIFIED')
    except Exception as ex:return (False,'FINAL_VALIDATION_EXCEPTION_'+type(ex).__name__)


def run(root, folder, applied, after, rows):
    date=folder.name[:8]
    canon=[]
    for suff in ('_PC.mp4','_HB.mp4'):
        x=folder/(date+suff)
        if x.is_file() and not is_link(x) and x.stat().st_size>0:canon.append(x)
    if not canon:
        rows.append({'date':date,'path':str(folder),'action':'SKIP','reason':'CANONICAL_PC_HB_MISSING','bytes':0})
        return
    # Only stat metadata is read; video contents are never hashed.
    sizes={c.stat().st_size for c in canon}
    successful,why=check_successful_final(folder,date)
    # Date-specific postprocessing must have a valid final. Historical dedup only needs a canonical copy.
    for base,dirs,files in os.walk(folder,topdown=True,followlinks=False):
        base=Path(base)
        dirs[:]=sorted([d for d in dirs if not is_link(base/d)])
        rel=base.relative_to(folder)
        parts={x.casefold() for x in rel.parts}
        in_temp=bool(parts.intersection(TEMP_DIRS)) and (len(rel.parts)>0 and rel.parts[0].casefold() in TEMP_DIRS)
        permanent=('song_clips' in parts or 'song_clips_v8' in parts or any(x.startswith('_historical_') for x in parts))
        for name in sorted(files):
            x=base/name
            if is_link(x):continue
            try:size=x.stat().st_size
            except OSError:continue
            action='KEEP';reason='UNRECOGNIZED_OR_PERMANENT'
            if x in canon or name.casefold() in (date+'_pc.mp4',date+'_hb.mp4'):
                reason='ORIGINAL_PC_OR_HB'
            elif permanent or PROTECT_NAME.search(name):
                reason='PUBLISHED_OR_REVIEW_OR_SONG_OUTPUT'
            elif in_temp and successful and FILE_GENERATED.fullmatch(name):
                action='DELETE_GENERATED_INTERMEDIATE';reason='VERIFIED_FINAL_DIGEST_AND_COMPLETED_STATE'
            elif not rel.parts and x.suffix.casefold()=='.mp4' and size in sizes:
                # User policy: equal file size and LastWriteTime => remove redundant MP4.
                # Limit to the LIVE root and retain permanent, published, and original media.
                try:
                    original_mtime=x.stat().st_mtime_ns
                    copies=[str(c) for c in canon if c.stat().st_size==size and c.stat().st_mtime_ns==original_mtime]
                    if copies:
                        action='DELETE_METADATA_MATCH';reason='SIZE_AND_MTIME_EQUALS_CANONICAL '+copies[0]
                    else:reason='SAME_SIZE_BUT_MTIME_DIFFERS'
                except OSError as exc:
                    action='KEEP';reason='METADATA_ERROR '+type(exc).__name__
            elif in_temp:
                reason='TEMP_NOT_VERIFIED '+why
            elif x.suffix.casefold() in {'.mp4','.mov','.mkv','.vob'}:
                reason='DIFFERENT_SIZE_NOT_PROVEN_DUPLICATE'
            if action.startswith('DELETE') and applied:
                try:
                    # Revalidate no concurrent change just before deletion.
                    if is_link(x) or not x.is_file() or x.stat().st_size!=size: raise OSError('FILE_CHANGED')
                    if action=='DELETE_METADATA_MATCH':
                        candidate_stat=x.stat()
                        if not any(c.stat().st_size==size and c.stat().st_mtime_ns==candidate_stat.st_mtime_ns for c in canon):
                            raise OSError('METADATA_CHANGED')
                    x.unlink()
                    action='DELETED'
                except OSError as exc:
                    action='ERROR';reason=type(exc).__name__+' '+str(exc)[:150]
            rows.append({'date':date,'path':str(x),'action':action,'reason':reason,'bytes':size})
        # Do not delete directories here, so traversal remains stable.
    if applied and successful:
        for base in (folder/'work',folder/'overlay_work',folder/'preview_work'):
            if is_link(base) or not base.is_dir():continue
            for foldername,dirs,files in os.walk(base,topdown=False,followlinks=False):
                p=Path(foldername)
                if is_link(p):continue
                if not list(p.iterdir()):
                    try:p.rmdir()
                    except OSError:pass


def main():
    p=argparse.ArgumentParser()
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--all',action='store_true')
    g.add_argument('--date',type=str)
    p.add_argument('--apply',action='store_true')
    p.add_argument('--after-process',action='store_true',help='Called by successful digest pipeline; delete only safe intermediates')
    p.add_argument('--root',type=Path,default=LIVE_ROOT)
    p.add_argument('--audit-root',type=Path,default=AUDIT_ROOT)
    a=p.parse_args()
    if a.after_process and (not a.apply or not a.date):p.error('--after-process needs --apply --date')
    if a.date and not re.fullmatch(r'20\d{6}',a.date):p.error('date must be YYYYMMDD')
    root=a.root.resolve()
    if os.name=='nt' and root!=LIVE_ROOT.resolve():p.error('Windows run refuses custom LIVE root')
    candidates=list(valid_dates(root))
    if a.date:candidates=[q for q in candidates if q.name==a.date+'_LIVE']
    if a.date and not candidates:raise RuntimeError('DATE_LIVE_NOT_FOUND')
    rows=[]
    for folder in candidates:
        # No historical cleanup of an in-progress LIVE from today. The postprocess
        # hook alone may clean its just-completed date after validating the digest.
        d=datetime.datetime.strptime(folder.name[:8],'%Y%m%d').date()
        if not a.after_process and d>=datetime.datetime.now().date():
            rows.append({'date':folder.name[:8],'path':str(folder),'action':'SKIP',
                         'reason':'CURRENT_DAY_MAY_STILL_BE_RECORDING','bytes':0})
            continue
        run(root,folder,a.apply,a.after_process,rows)
    a.audit_root.mkdir(parents=True,exist_ok=True)
    stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    report=a.audit_root/f'{stamp}_{"applied" if a.apply else "scan"}.csv'
    with report.open('x',encoding='utf-8-sig',newline='') as h:
        w=csv.DictWriter(h,fieldnames=['date','path','action','reason','bytes']);w.writeheader();w.writerows(rows)
    from collections import Counter
    counts=Counter(x['action'] for x in rows)
    print('LIVE_CLEANUP_'+('APPLIED' if a.apply else 'SCAN_COMPLETE'))
    print('FOLDERS',len(candidates),'REPORT',report)
    for k in sorted(counts):print('ACTION',k,counts[k])
    print('PROTECTED_PC_HB_DIGEST_AND_METADATA_NOT_DELETED')
    if counts['ERROR']:return 3
    return 0

if __name__=='__main__':
    try:sys.exit(main())
    except Exception as e:
        print('LIVE_CLEANUP_ABORTED',type(e).__name__,str(e),file=sys.stderr)
        sys.exit(2)
