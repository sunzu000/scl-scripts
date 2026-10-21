"""One-off archive transcription, serial and resumable. No video rendering."""
import argparse
import csv
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
import zipfile


def discover(root):
    return sorted((p for p in root.rglob('*.mp4') if re.search(r'(?<!\d)(20\d{6})(?!\d)',p.name)),key=lambda p:str(p).lower())


def fingerprint(source):
    stat=source.stat()
    return {'source':str(source.resolve()),'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}


def execute(args):
    sys.path.insert(0,str(Path(args.pipeline).resolve().parent))
    from digest_pipeline import FileLock, save_json, transcribe, probe
    config=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    config['cpu_threads']=min(4,int(config.get('cpu_threads',4)))
    source_root=Path(args.source).resolve()
    output=Path(args.output).resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(source_root)
    output.mkdir(parents=True,exist_ok=True)
    items=discover(source_root)
    if not items:
        raise ValueError('No dated MP4 files found')
    records=[]
    ready=[]
    with FileLock(output/'archive_batch.lock'):
        with (output/'batch.log').open('a',encoding='utf-8') as logfile:
            logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s',
                                handlers=[logging.StreamHandler(),logging.StreamHandler(logfile)])
            for number,source in enumerate(items,1):
                relative=source.relative_to(source_root)
                folder=output/'work'/relative.parent/source.stem
                folder.mkdir(parents=True,exist_ok=True)
                statefile=folder/'archive_state.json'
                exported=folder/(source.stem+'_transcript.txt')
                item={'video':str(source),'index':str(source.with_suffix('.txt')),'transcript':str(exported),'status':''}
                try:
                    if source.with_suffix('.txt').exists():
                        item['status']='existing_index_skipped'
                    elif any(word in source.name for word in ('無音','音無')):
                        item['status']='named_silent_skipped'
                    else:
                        current=fingerprint(source)
                        old=json.loads(statefile.read_text()) if statefile.exists() else {}
                        if old.get('status')=='completed' and old.get('fingerprint')==current and exported.exists():
                            item['status']='completed_skipped'
                        else:
                            # Only trust a completed fingerprint; retry partial or stale transcripts from scratch.
                            (folder/'transcript.json').unlink(missing_ok=True)
                            save_json(statefile,{'status':'running','fingerprint':current})
                            logging.info('[%d/%d] %s',number,len(items),source)
                            duration,_=probe(config['ffmpeg'],source)
                            transcribe(source,folder,config)
                            text=(folder/'transcript.txt').read_text(encoding='utf-8-sig')
                            exported.write_text('元動画: '+str(source)+'\n動画時間（秒）: '+str(duration)+'\n時刻はこのMP4の先頭基準\n\n'+text,encoding='utf-8-sig')
                            save_json(statefile,{'status':'completed','fingerprint':current,'transcript':str(exported)})
                            item['status']='completed'
                        ready.append((exported,relative.with_suffix('.txt')))
                except KeyboardInterrupt:
                    logging.info('Stopped. Run the same command to resume.')
                    item['status']='interrupted'
                    records.append(item)
                    break
                except Exception as exc:
                    item['status']='failed'
                    item['error']=str(exc)
                    logging.exception('Failed: %s',source)
                records.append(item)
                save_json(output/'batch_status.json',records)
            save_json(output/'batch_status.json',records)
    with (output/'batch_status.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['video','index','transcript','status','error'])
        writer.writeheader();writer.writerows(records)
    # A month per ZIP is small enough to review in separate batches.
    groups={}
    for path,relative in ready:
        groups.setdefault(str(relative.parent).replace('\\','_').replace('/','_'),[]).append((path,relative))
    uploads=output/'Upload'
    uploads.mkdir(exist_ok=True)
    for key,group in groups.items():
        with zipfile.ZipFile(uploads/(key+'_transcripts.zip'),'w',zipfile.ZIP_DEFLATED) as z:
            for path,relative in group:z.write(path,str(relative.with_name(relative.stem+'_transcript.txt')))
    print('Finished. Monthly transcript ZIPs: '+str(uploads))
    print('Status: '+str(output/'batch_status.csv'))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',default=r'F:\SCL\TikTok')
    parser.add_argument('--output',default=r'F:\Backup\Recordings\TikTok\ArchiveIndex')
    parser.add_argument('--pipeline',default=r'C:\Scripts\digest_pipeline.py')
    parser.add_argument('--config',default=r'C:\Scripts\digest_config.json')
    execute(parser.parse_args())


if __name__=='__main__':
    main()
