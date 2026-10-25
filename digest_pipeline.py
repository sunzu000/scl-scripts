"""Local TikTok post-processing. No API key or cloud transcription required."""
import argparse
import csv
import difflib
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unicodedata


class FileLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.handle.write(b'0')
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise RuntimeError('Another process is already running: ' + str(self.path))
        return self

    def __exit__(self, *unused):
        if self.handle:
            self.handle.close()


def save_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def run(command, cwd=None):
    logging.info('FFmpeg processing: %s', Path(command[-1]).name)
    command = [command[0], '-threads', '2', '-filter_threads', '1'] + list(command[1:])
    subprocess.run([str(x) for x in command], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=None)


def probe(ffmpeg, path):
    ffprobe = Path(ffmpeg).with_name('ffprobe.exe' if os.name == 'nt' else 'ffprobe')
    p = subprocess.run([str(ffprobe), '-v', 'error', '-show_streams', '-show_format',
                        '-of', 'json', str(path)], check=True, capture_output=True,
                       text=True, encoding='utf-8')
    data = json.loads(p.stdout)
    streams = data['streams']
    if not any(s['codec_type'] == 'video' for s in streams):
        raise ValueError('Video stream missing: ' + str(path))
    if not any(s['codec_type'] == 'audio' for s in streams):
        raise ValueError('Audio stream missing: ' + str(path))
    duration = float(data['format'].get('duration', 0))
    if duration <= 0:
        raise ValueError('Duration missing: ' + str(path))
    return duration, streams


def signature(streams):
    fields = ('codec_type', 'codec_name', 'profile', 'width', 'height', 'pix_fmt',
              'sample_aspect_ratio', 'r_frame_rate', 'time_base', 'sample_rate',
              'channels', 'channel_layout', 'extradata_size')
    return [[s.get(k) for k in fields] for s in streams if s['codec_type'] in ('video', 'audio')]


def concat_list(path, inputs):
    # Use generated simple relative filenames; avoid quoting user filenames in concat syntax.
    path.write_text(''.join("file '" + p.name + "'\n" for p in inputs), encoding='utf-8')


def pc_video_path(folder):
    """Return final PC recording path such as 20261018_PC.mp4."""
    folder = Path(folder)
    match = re.match(r'^(\d{8})', folder.name)
    if not match:
        raise RuntimeError(f'Cannot determine date from LIVE folder name: {folder.name}')
    return folder / f'{match.group(1)}_PC.mp4'


def merge_inputs(inputs, folder, ffmpeg):
    work = folder / 'work'
    work.mkdir(exist_ok=True)
    warnings = []
    parts, durations, signatures = [], [], []
    for index, source in enumerate(inputs):
        source = Path(source).resolve()
        if not source.is_file() or source.stat().st_size == 0:
            warnings.append('Missing/empty input: ' + str(source))
            continue
        part = work / f'part_{index:04d}.mp4'
        try:
            run([ffmpeg, '-hide_banner', '-loglevel', 'warning', '-y', '-i', source,
                 '-map', '0:v:0', '-map', '0:a:0', '-c', 'copy', '-movflags', '+faststart', part])
            duration, streams = probe(ffmpeg, part)
            parts.append(part)
            durations.append(duration)
            signatures.append(signature(streams))
        except (subprocess.CalledProcessError, ValueError) as exc:
            warnings.append(f'Unreadable input retained: {source}: {exc}')
    if not parts:
        raise RuntimeError('No readable recordings. Originals have been retained.')
    target = pc_video_path(folder)
    temporary = folder / (target.stem + '.pending.mp4')
    compatible = all(s == signatures[0] for s in signatures)
    concat_list(work / 'parts.txt', parts)
    try:
        if not compatible:
            raise ValueError('Different recording formats')
        run([ffmpeg, '-hide_banner', '-loglevel', 'warning', '-y', '-f', 'concat',
             '-safe', '0', '-i', 'parts.txt', '-map', '0:v:0', '-map', '0:a:0',
             '-c', 'copy', '-movflags', '+faststart', temporary.resolve()], cwd=work)
        duration, _ = probe(ffmpeg, temporary)
        if abs(duration - sum(durations)) > max(2, len(parts)):
            raise ValueError('Merged duration mismatch')
    except (subprocess.CalledProcessError, ValueError):
        normalized = []
        for i, part in enumerate(parts):
            normal = work / f'normal_{i:04d}.mp4'
            run([ffmpeg, '-hide_banner', '-loglevel', 'warning', '-y', '-i', part,
                 '-vf', 'scale=trunc(iw*sar/2)*2:ih,setsar=1,scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30',
                 '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-crf', '21', '-pix_fmt', 'yuv420p',
                 '-c:a', 'aac', '-ar', '48000', '-ac', '2', normal])
            normalized.append(normal)
        concat_list(work / 'normal.txt', normalized)
        run([ffmpeg, '-hide_banner', '-loglevel', 'warning', '-y', '-f', 'concat',
             '-safe', '0', '-i', 'normal.txt', '-c', 'copy', '-movflags', '+faststart',
             temporary.resolve()], cwd=work)
        duration, _ = probe(ffmpeg, temporary)
        if abs(duration - sum(durations)) > max(2, len(parts)):
            raise ValueError('Merged duration mismatch after normalization')
    os.replace(temporary, target)
    save_json(folder / 'source_parts.json', {'inputs': [str(p) for p in inputs],
              'included_durations': durations, 'warnings': warnings,
              'note': 'Missing time is not filled; overlaps are not automatically removed.'})
    removed_sources = cleanup_source_recordings(inputs, target)
    if removed_sources:
        logging.info('Merged video verified; removed %d source recording part(s)', len(removed_sources))
    return target, warnings


def stamp(seconds):
    n = max(0, int(seconds))
    return f'{n//3600:02}:{n%3600//60:02}:{n%60:02}'


def read_transcript(path):
    path = Path(path)
    text = path.read_text(encoding='utf-8-sig')

    # Native transcript.json generated by this pipeline.
    if path.suffix.lower() == '.json':
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError('Transcript JSON must contain a list of segments')
        segments = [
            item for item in data
            if isinstance(item, dict)
            and 'start' in item
            and 'end' in item
            and 'text' in item
        ]
        if not segments:
            raise ValueError('No timestamped transcript segments found in JSON')
        logging.info('Loaded transcript JSON: %s (%d segments)', path, len(segments))
        return segments

    # Backward compatibility with timestamped transcript.txt.
    segments = []
    for line in text.splitlines():
        m = re.match(r'\[(\d\d:\d\d:\d\d)\s*(?:->|→)\s*(\d\d:\d\d:\d\d)\]\s*(.*)', line)
        if m:
            def seconds(t):
                h, m, s = map(int, t.split(':'))
                return h*3600 + m*60 + s
            segments.append({'start': seconds(m[1]), 'end': seconds(m[2]), 'text': m[3]})
    if not segments:
        raise ValueError('No timestamped transcript lines found')
    return segments


def transcribe(video, folder, config, force=False):
    output = folder / 'transcript.json'
    if output.exists() and not force:
        logging.info('Existing transcript found: %s', output)
        return json.loads(output.read_text(encoding='utf-8'))
    from faster_whisper import WhisperModel
    duration, _ = probe(config['ffmpeg'], video)
    logging.info('Transcription start: %s (%s)', Path(video).name, stamp(duration))
    logging.info('Loading Whisper model: %s', config['model'])
    model = WhisperModel(config['model'], device='cpu', compute_type='int8',
                         cpu_threads=config.get('cpu_threads', 4), num_workers=1)
    generated, _ = model.transcribe(str(video), language='ja', condition_on_previous_text=False, word_timestamps=True)
    segments = []
    last_progress = -30.0
    with (folder / 'transcript.txt').open('w', encoding='utf-8') as f:
        for s in generated:
            item = {'start': s.start, 'end': s.end, 'text': s.text.strip(),
                    'no_speech_prob': s.no_speech_prob, 'avg_logprob': s.avg_logprob,
                    'words': [{'word': w.word, 'start': w.start, 'end': w.end,
                               'probability': w.probability} for w in (s.words or [])]}
            segments.append(item)
            line = f'[{stamp(s.start)} -> {stamp(s.end)}] {s.text.strip()}'
            f.write(line + '\n')
            f.flush()
            if s.end - last_progress >= 30 or s.end >= duration - 1:
                percent = min(100.0, (s.end / duration * 100.0) if duration else 0.0)
                logging.info('Transcribing: %s / %s (%.1f%%)', stamp(s.end), stamp(duration), percent)
                last_progress = s.end
    save_json(output, segments)
    logging.info('Transcription completed: %s (%d segments)', output, len(segments))
    return segments


def normalize(text):
    text = unicodedata.normalize('NFKC', text).lower()
    return ''.join(c for c in text if c.isalnum())


def similarity(anchor, text):
    a, b = normalize(anchor), normalize(text)
    if not a or not b:
        return 0
    if a in b:
        return 1
    if len(a) < 5:
        return 0
    # Compare short sliding windows; tolerate transcription errors but preserve long evidence.
    return max((difflib.SequenceMatcher(None, a, b[i:i+len(a)]).ratio()
                for i in range(max(1, len(b)-len(a)+1))), default=0)


def detect_songs(segments, catalog, clip_seconds, threshold=0.78):
    detections = []
    for song in catalog:
        anchors = song.get('opening_lyrics', [])
        hits = []
        for i, segment in enumerate(segments):
            score = max((similarity(a, segment['text']) for a in anchors), default=0)
            if score >= song.get('threshold', threshold):
                # Short matching strings need a nearby title announcement for confidence.
                if all(len(normalize(a)) < 5 for a in anchors):
                    nearby = segments[max(0, i-15):i]
                    if not any(any(normalize(alias) in normalize(s['text']) for alias in song.get('aliases', [])) for s in nearby):
                        continue
                confirmations = song.get('confirm_lyrics', [])
                if confirmations:
                    following = [s['text'] for s in segments[i+1:i+9]
                                 if s['start'] - segment['end'] < 30]
                    if not any(similarity(a, text) >= 0.78 for a in confirmations for text in following):
                        continue
                hits.append((i, score))
        last = -100000
        for i, score in hits:
            s = segments[i]
            if s['start'] - last < song.get('repeat_window', 480):
                continue
            last = s['start']
            start = max(0, s['start'] + song.get('start_offset', 0))
            detections.append({'title': song['title'], 'start': round(start, 3),
                               'seconds': clip_seconds, 'status': 'candidate',
                               'evidence': s['text'], 'match_score': round(score, 3)})
    detections.sort(key=lambda x: x['start'])
    # Competing matches in the same line: retain strongest, flag ambiguity.
    selected = []
    for item in detections:
        if selected and abs(item['start']-selected[-1]['start']) < 2:
            old = selected[-1]
            best = item if item['match_score'] > old['match_score'] else old
            best['ambiguity'] = f"Competing title: {old['title'] if best is item else item['title']}"
            selected[-1] = best
        else:
            selected.append(item)
    return selected



def match_opening_words(words, song, reference):
    """Locate the FIRST lyric word, not the start of a Whisper text segment.

    Matching is anchored at a word boundary. Searching for a substring in a
    long sentence could otherwise retain a preceding MC word as the onset.
    """
    anchors = song.get('opening_lyrics', [])
    candidates = []
    for i, word in enumerate(words):
        if float(word['end']) - float(word['start']) < 0.04:
            continue
        for anchor in anchors:
            anchor = normalize(anchor)
            if not anchor:
                continue
            phrase = ''
            best = None
            for j in range(i, min(len(words), i+24)):
                if j > i and float(words[j]['start']) - float(words[j-1]['end']) > 3:
                    break
                phrase += normalize(words[j]['word'])
                if not phrase:
                    continue
                # Whole prefix match, with no arbitrary text skipped at its start.
                score = difflib.SequenceMatcher(None, anchor, phrase).ratio()
                if score >= song.get('word_threshold', 0.78):
                    if best is None or score > best[0]:
                        best = (score, j)
                if len(phrase) > len(anchor)*1.5 + 3:
                    break
            if best:
                score, j = best
                span = words[i:j+1]
                prob = sum(float(w.get('probability', 0)) for w in span) / len(span)
                if prob < 0.25:
                    continue
                # For very short lyrics, require the following registered phrase.
                if len(anchor) < 5:
                    following = ''.join(w['word'] for w in words[j+1:j+20])
                    confirms = song.get('confirm_lyrics', [])
                    if not confirms or not any(similarity(x, following) >= 0.78 for x in confirms):
                        continue
                candidates.append({'start': float(word['start']), 'score': score,
                    'probability': prob, 'phrase': ''.join(w['word'] for w in span)})
    if not candidates:
        return None
    # Prefer closest high-quality prefix; retaining an earlier MC is penalized
    # by both the anchored phrase match and the transcription probability.
    candidates.sort(key=lambda x: (-round(x['score'], 2), abs(x['start']-reference)))
    return candidates[0]


def refine_onsets(video, folder, songs, config):
    """Re-transcribe only the opening neighborhood of each detected song.

    Local audio windows also work with older .txt transcripts lacking words.
    This is an estimate of vocal onset, not a music/voice separation model.
    """
    if not songs:
        return songs, []
    from faster_whisper import WhisperModel
    logging.info('Re-detecting lyric onset with word timestamps for all %d songs', len(songs))
    model = WhisperModel(config['model'], device='cpu', compute_type='int8',
                         cpu_threads=config.get('cpu_threads', 4), num_workers=1)
    work = folder / 'onset_work'
    work.mkdir(exist_ok=True)
    video_duration, _ = probe(config['ffmpeg'], video)
    catalog = {song['title']: song for song in config['songs']}
    warnings, details = [], []
    for i, item in enumerate(songs):
        old_start = float(item['start'])
        begin = max(0, old_start - config.get('onset_lookback_seconds', 8))
        length = min(video_duration-begin, config.get('onset_window_seconds', 38))
        audio = work / f'onset_{i:04d}.wav'
        run([config['ffmpeg'], '-hide_banner', '-loglevel', 'warning', '-y',
             '-ss', str(begin), '-i', video, '-t', str(length), '-vn',
             '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', audio])
        generated, _ = model.transcribe(str(audio), language='ja',
                        condition_on_previous_text=False, word_timestamps=True,
                        vad_filter=False, beam_size=5)
        words = []
        for seg in generated:
            for word in (seg.words or []):
                words.append({'word': word.word, 'start': word.start + begin,
                              'end': word.end + begin, 'probability': word.probability})
        song = catalog.get(item['title'])
        match = match_opening_words(words, song, old_start) if song else None
        item['segment_start'] = old_start
        item['seconds'] = config['clip_seconds']
        if match and begin + 0.15 < match['start'] < begin + length - 0.5:
            item['start'] = round(max(0, match['start'] - 0.03), 3)
            item['onset_method'] = 'lyric_word_alignment'
            item['onset_phrase'] = match['phrase']
            item['onset_probability'] = round(match['probability'], 3)
            logging.info('Onset %s: %.3f -> %.3f (%s)', item['title'], old_start,
                         item['start'], match['phrase'])
        else:
            item['onset_method'] = 'segment_fallback'
            warnings.append('歌い始め再検出に不確実さあり: ' + item['title'] +
                            '（区間開始時刻へ自動フォールバック）')
            logging.warning('Word onset not confidently matched: %s; retain segment estimate', item['title'])
        details.append({'title': item['title'], 'old_start': old_start,
                        'selected_start': item['start'], 'match': match, 'words': words})
        save_json(folder / 'onset_details.json', details)
    songs.sort(key=lambda x: x['start'])
    return songs, warnings


def write_playlist(folder, songs, video, warnings):
    save_json(folder / 'playlist.json', songs)
    with (folder / 'playlist.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['number', 'start', 'seconds', 'title', 'status', 'evidence'])
        for n, s in enumerate(songs, 1):
            writer.writerow([n, s['start'], s['seconds'], s['title'], s.get('status', 'confirmed'), s.get('evidence', '')])
    lines = ['歌唱一覧（自動抽出の候補・確認前）', f'元動画: {video}', '',
             '歌い始めを冒頭歌詞の単語時刻で再検出。時刻はWhisperの推定です。',
             '登録済み冒頭歌詞に一致した曲だけを抽出。未登録曲・認識漏れは含まれません。',
             'MCと歌声を音響判別していないため、冒頭歌詞を話した場合も候補になります。', '']
    lines.extend(f"{i:02d}. {stamp(s['start'])}  {s['title']} ({s['seconds']}秒)" for i, s in enumerate(songs, 1))
    lines += ['', '確認・修正: playlist.json の start（秒）/title/seconds を編集し、--render-only で再作成。',
              '元動画は削除しません。録画欠落を埋めたり重複部分を除いたりはしません。']
    if warnings:
        lines += ['', '入力動画の警告:'] + warnings
    (folder / 'review.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def ass_text(text):
    # Do not allow titles to become ASS override tags or line breaks.
    return str(text).replace('\\', '／').replace('{', '（').replace('}', '）').replace('\n', ' ').replace('\r', ' ')


def make_ass(path, songs, active, date, seconds, source_duration):
    if not 1 <= len(songs) <= 28:
        raise ValueError('This test layout supports 1 to 28 songs.')
    centi = round(seconds * 100)
    end = f'{centi//360000}:{centi%360000//6000:02d}:{centi%6000//100:02d}.{centi%100:02d}'
    content = '''[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Yu Gothic,24,&H00FFFFFF,&H000000FF,&H00101010,&H00000000,0,0,0,0,100,100,0,0,1,1.5,0.6,7,0,0,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    def add(tags, text, layer=1):
        nonlocal content
        content += f'Dialogue: {layer},0:00:00.00,{end},Default,,0,0,0,,{{{tags}}}{text}\n'
    def text(value, x, y, size, color='FFFFFF', bold=0):
        add(f'\\an7\\pos({x},{y})\\fs{size}\\c&H{color}&\\b{bold}', ass_text(value))
    x, top, row = 370, 24, min(34, 960 / len(songs))
    add('\\an9\\pos(650,58)\\fnMeiryo\\fs28\\b1\\c&HFFFFFF&', ass_text(date+'  LIVE DIGEST'))
    add('\\an7\\pos(360,1196)\\p1\\bord0\\shad0\\c&HD6C2FF&', 'm 0 0 l 290 0 290 2 0 2')
    add('\\an9\\pos(650,1204)\\fnMeiryo\\fs56\\b1\\bord2\\shad1\\c&HD6C2FF&', '内田もあ')
    add('\\an9\\pos(650,1120)\\fs32\\b1\\c&HFFFFFF&', '@moa_pi')
    for i, song in enumerate(songs):
        y = top+80+i*row
        title = str(song.get('title') or '').strip()
        if title.casefold() in {'不明', '曲名不明', '楽曲名不明', '未判別', 'unknown', '?', '？'}:
            title = ''
        size = min(26, int(325/max(1,len(title)+2)))
        size = max(18, size)
        color = '66E8FF' if i == active else 'FFFFFF'
        text(f'{i+1:02d}' + (f' {title}' if title else ''), x+14, y, size, color, 1)
    path.write_text(content, encoding='utf-8-sig')


def render(video, folder, songs, config, date, preview=False):
    source_duration, streams = probe(config['ffmpeg'], video)
    if not re.fullmatch(r'\d{8}', date):
        raise ValueError('Date must be YYYYMMDD')
    if not songs:
        raise ValueError('Playlist is empty')
    for song in songs:
        start, length = float(song['start']), float(song.get('seconds',20))
        if not all(map(math.isfinite,[start,length])) or start < 0 or length <= 0 or start+length > source_duration:
            raise ValueError('Invalid playlist entry: '+str(song))
    work = folder / ('preview_work' if preview else 'overlay_work')
    work.mkdir(parents=True, exist_ok=True)
    selected = songs[:1] if preview else songs
    clips = []
    for i,song in enumerate(selected):
        length = min(5,float(song.get('seconds',20))) if preview else float(song.get('seconds',20))
        ass = work / f'list_{i:04d}.ass'
        make_ass(ass,songs,i,f'{date[:4]}/{date[4:6]}/{date[6:]}',length,source_duration)
        clip = work / f'clip_{i:04d}.mp4'
        vf = ('scale=trunc(iw*sar/2)*2:ih,setsar=1,'
              'scale=720:1280:force_original_aspect_ratio=decrease,'
              'pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,'
              f'subtitles=filename={ass.name},fps=30')
        run([config['ffmpeg'],'-hide_banner','-loglevel','warning','-y','-ss',str(song['start']),'-i',video,
             '-t',str(length),'-map','0:v:0','-map','0:a:0','-vf',vf,'-c:v','libx264','-preset','veryfast',
             '-crf','20','-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-ac','2','-b:a','128k',
             '-af',f'afade=t=in:d=0.08,afade=t=out:st={max(0,length-.15)}:d=0.15',
             '-movflags','+faststart',clip.resolve()],cwd=work)
        actual,_ = probe(config['ffmpeg'],clip)
        if abs(actual-length) > .4:
            raise ValueError('Clip duration mismatch')
        clips.append(clip)
    if preview:
        final = folder / 'layout_preview.mp4'
        import shutil
        shutil.copyfile(clips[0],final)
        run([config['ffmpeg'],'-hide_banner','-loglevel','error','-y','-ss','1','-i',final,
             '-frames:v','1','-update','1',folder/'layout_preview.jpg'])
    else:
        concat_list(work/'clips.txt',clips)
        temp = folder / f'{date}_digest.pending.mp4'
        run([config['ffmpeg'],'-hide_banner','-loglevel','warning','-y','-f','concat','-safe','0',
             '-i','clips.txt','-c:v','copy','-af','aresample=async=1:first_pts=0','-c:a','aac',
             '-movflags','+faststart',temp.resolve()],cwd=work)
        actual,_=probe(config['ffmpeg'],temp)
        expected=sum(float(s.get('seconds',20)) for s in songs)
        if abs(actual-expected) > max(1,len(songs)*.1):
            raise ValueError('Digest duration mismatch')
        final=folder/f'{date}_digest.mp4'
        os.replace(temp,final)
    save_json(folder/'render_result.json',{'source':str(video),'source_duration':source_duration,
              'source_video_stream':next(s for s in streams if s['codec_type']=='video'),
              'output':str(final),'preview':preview,'songs':len(songs),
              'time_basis':'recording start; broadcast start and full LIVE duration not independently verified'})
    print('Created: '+str(final))
    return final



def cleanup_source_recordings(inputs, merged):
    """YYYYMMDD_PC.mp4 完成後、同一フォルダ内の元録画partだけ削除する。"""
    merged = Path(merged).resolve()
    folder = merged.parent
    removed = []
    for source in inputs:
        p = Path(source).resolve()
        if p == merged or p.parent != folder:
            continue
        name = p.name.lower()
        if "_part" not in name or not (name.endswith(".mp4") or name.endswith(".mp4.part")):
            continue
        if p.is_file():
            p.unlink()
            removed.append(str(p))
    return removed


def supabase_sync_live(folder, songs, date, video=None, digest=None):
    """Upsert one LIVE and its playlist to Supabase using user environment variables."""
    import requests

    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SECRET_KEY')
    if not url or not key:
        logging.warning('Supabase sync skipped: environment variables are not configured')
        return False

    raw_date = str(date or '').replace('-', '')
    if not re.fullmatch(r'\d{8}', raw_date):
        logging.warning('Supabase sync skipped: invalid date %r', date)
        return False
    live_date = f'{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}'

    headers = {
        'apikey': key,
        'Authorization': 'Bearer ' + key,
        'Content-Type': 'application/json',
    }

    def request(method, path, **kwargs):
        response = requests.request(method, url + path, headers=headers, timeout=60, **kwargs)
        if not response.ok:
            raise RuntimeError(f'Supabase HTTP {response.status_code}: {response.text[:500]}')
        return response

    folder = Path(folder).resolve()
    transcript_path = folder / 'transcript.json'
    playlist_path = folder / 'playlist.json'

    # Upsert LIVE header by live_date and return live_id.
    payload = {
        'live_date': live_date,
        'song_count': len(songs),
        'source_folder': str(folder),
        'transcript_path': str(transcript_path) if transcript_path.exists() else None,
        'playlist_path': str(playlist_path) if playlist_path.exists() else None,
        'digest_path': str(digest) if digest else None,
    }
    h = {**headers, 'Prefer': 'resolution=merge-duplicates,return=representation'}
    r = requests.post(
        url + '/rest/v1/tiktok_lives?on_conflict=live_date',
        headers=h, json=payload, timeout=60)
    if not r.ok:
        raise RuntimeError(f'Supabase LIVE upsert HTTP {r.status_code}: {r.text[:500]}')
    live_id = r.json()[0]['live_id']

    # Load original-song master. Only an exact, unique title match is linked.
    master = request('GET', '/rest/v1/songs?select=song_id,song_title&limit=1000').json()
    title_to_ids = {}
    for item in master:
        title_to_ids.setdefault(item['song_title'], []).append(item['song_id'])

    detail = []
    for song_no, song in enumerate(songs, 1):
        title = str(song.get('title') or '').strip() or '曲名不明'
        ids = title_to_ids.get(title, [])
        song_id = ids[0] if len(ids) == 1 else None
        detail.append({
            'live_id': live_id,
            'song_no': song_no,
            'title': title,
            'start_seconds': float(song['start']) if song.get('start') is not None else None,
            'song_id': song_id,
            'needs_review': title in {'不明', '曲名不明', '楽曲名不明', '未判別', 'unknown', '?', '？'},
        })

    if detail:
        r = requests.post(
            url + '/rest/v1/tiktok_live_songs?on_conflict=live_id,song_no',
            headers={**headers, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
            json=detail, timeout=60)
        if not r.ok:
            raise RuntimeError(f'Supabase song upsert HTTP {r.status_code}: {r.text[:500]}')

    logging.info('Supabase sync completed: %s / %d songs / live_id=%s',
                 live_date, len(detail), live_id)
    return True


def process(inputs, folder, config, transcript=None, playlist=None, date='', render_only=False):
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    with FileLock(folder / 'processing.lock'):
        state_path = folder / 'state.json'
        save_json(state_path, {'status': 'running'})
        try:
            merged = pc_video_path(folder)
            warnings = []
            if not render_only:
                fingerprint = hashlib.sha256(json.dumps([
                    [str(Path(p).resolve()), Path(p).stat().st_size, Path(p).stat().st_mtime_ns]
                    for p in inputs], ensure_ascii=False).encode()).hexdigest()
                old = folder / 'input_fingerprint.txt'
                if not merged.exists() or not old.exists() or old.read_text() != fingerprint:
                    merged, warnings = merge_inputs(inputs, folder, config['ffmpeg'])
                    (folder / 'transcript.json').unlink(missing_ok=True)
                    old.write_text(fingerprint)
                else:
                    warnings = json.loads((folder / 'source_parts.json').read_text(encoding='utf-8'))['warnings']
                if transcript:
                    segments = read_transcript(transcript)
                    save_json(folder / 'transcript.json', segments)
                    (folder / 'transcript.txt').write_text(Path(transcript).read_text(encoding='utf-8-sig'), encoding='utf-8')
                else:
                    segments = transcribe(merged, folder, config)
                if playlist:
                    songs = json.loads(Path(playlist).read_text(encoding='utf-8-sig'))
                else:
                    songs = detect_songs(segments, config['songs'], config['clip_seconds'])
                    songs, onset_warnings = refine_onsets(merged, folder, songs, config)
                    warnings.extend(onset_warnings)
                write_playlist(folder, songs, merged, warnings)
            else:
                songs = json.loads((folder / 'playlist.json').read_text(encoding='utf-8-sig'))
                warnings = ['Manual re-render; see source_parts.json for original input warnings.']
                # Do not overwrite manual corrections in playlist.json.
                write_playlist(folder, songs, merged, warnings)
            final = render(merged.resolve(), folder, songs, config, date or folder.name)
            try:
                supabase_sync_live(folder, songs, date or folder.name[:8], merged, final)
            except Exception as sync_exc:
                # A cloud/database problem must not destroy a successfully created digest.
                warnings.append('Supabase sync failed: ' + str(sync_exc))
                logging.exception('Supabase sync failed; digest is retained')
            save_json(state_path, {'status': 'completed_candidates', 'output': str(final),
                                  'songs': len(songs), 'warnings': warnings,
                                  'review_required': True})
            return final
        except Exception as exc:
            save_json(state_path, {'status': 'failed', 'error': str(exc)})
            raise


def queue_worker(root):
    root = Path(root).resolve()
    # A new producer may arrive just as the previous worker exits. Briefly retry
    # the process lock; an active worker will discover the durable pending job.
    worker_lock = None
    for attempt in range(4):
        try:
            worker_lock = FileLock(root / 'postprocess.lock')
            worker_lock.__enter__()
            break
        except RuntimeError:
            worker_lock = None
            time.sleep(1)
    if worker_lock is None:
        logging.info('An active worker owns the queue')
        return
    try:
        seen = set()
        while True:
            jobs = []
            for path in sorted(root.glob('*/session.json')):
                if path in seen:
                    continue
                job = json.loads(path.read_text(encoding='utf-8'))
                if job.get('status') == 'pending':
                    jobs.append((path, job))
            if not jobs:
                return
            for path, job in jobs:
                seen.add(path)
                job['status'] = 'processing'
                save_json(path, job)
                try:
                    config = json.loads(Path(job['config']).read_text(encoding='utf-8-sig'))
                    process(job['inputs'], path.parent, config, date=job['date'])
                    job['status'] = 'completed_candidates'
                except Exception as exc:
                    logging.exception('Post-processing failed: %s', path)
                    job['status'] = 'failed'
                    job['error'] = str(exc)
                save_json(path, job)
    finally:
        worker_lock.__exit__(None, None, None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--queue-root')
    parser.add_argument('--input', nargs='+')
    parser.add_argument('--output')
    parser.add_argument('--config', default=str(Path(__file__).with_name('digest_config.json')))
    parser.add_argument('--transcript')
    parser.add_argument('--playlist')
    parser.add_argument('--date', default='')
    parser.add_argument('--render-only', action='store_true')
    parser.add_argument('--transcribe-only', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.queue_root:
        try:
            queue_worker(args.queue_root)
        except RuntimeError as exc:
            logging.info('%s', exc)
        return
    if not args.output:
        parser.error('--output is required')
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    if args.transcribe_only:
        if args.input:
            parser.error('--transcribe-only uses the existing YYYYMMDD_PC.mp4; do not specify --input')
        folder = Path(args.output).resolve()
        video = pc_video_path(folder)
        if not video.is_file():
            parser.error('PC video not found: ' + str(video))
        with FileLock(folder / 'processing.lock'):
            transcribe(video, folder, config, force=True)
        return
    if not args.render_only and not args.input:
        parser.error('--input is required unless --render-only or --transcribe-only is used')
    process(args.input or [], args.output, config, args.transcript, args.playlist, args.date, args.render_only)


if __name__ == '__main__':
    main()

