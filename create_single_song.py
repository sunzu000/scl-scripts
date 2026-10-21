import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, r'C:\Scripts')
import digest_pipeline as pipeline


def single_ass(original):
    def make(path, songs, active, date, seconds, duration):
        original(path, songs, active, date, seconds, duration)
        text = path.read_text(encoding='utf-8-sig')
        title = pipeline.ass_text(songs[0]['title'])
        rows = text.splitlines()
        for i, row in enumerate(rows):
            if row.endswith('}01 ' + title):
                fields = row.split(',', 9)
                fields[9] = r'{\an9\pos(650,104)\fs48\b1\c&H66E8FF&}' + title
                rows[i] = ','.join(fields)
        path.write_text('\n'.join(rows) + '\n', encoding='utf-8-sig')
    return make


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--date', required=True)
    p.add_argument('--title', required=True)
    p.add_argument('--start', type=float, required=True)
    p.add_argument('--end', type=float, required=True)
    args = p.parse_args()
    if args.end <= args.start or args.start < 0:
        p.error('Invalid start or end.')
    if not re.fullmatch(r'\d{8}', args.date):
        p.error('Date must be YYYYMMDD.')
    args.output.mkdir(parents=True, exist_ok=True)
    config = json.loads(Path(r'C:\Scripts\digest_config.json').read_text(encoding='utf-8-sig'))
    songs = [{'title': args.title, 'start': args.start, 'seconds': args.end - args.start}]
    original = pipeline.make_ass
    with pipeline.FileLock(args.output / 'processing.lock'):
        pipeline.make_ass = single_ass(original)
        try:
            rendered = pipeline.render(args.input.resolve(), args.output.resolve(), songs, config, args.date)
        finally:
            pipeline.make_ass = original
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', args.title).rstrip(' .')
        final = args.output / f'{args.date}_{name}.mp4'
        rendered.replace(final)
        pipeline.save_json(args.output / 'render_result.json', {
            'source': str(args.input), 'title': args.title, 'start': args.start,
            'end': args.end, 'output': str(final), 'review_required': True,
        })
        print('Single song created: ' + str(final))


if __name__ == '__main__':
    main()
