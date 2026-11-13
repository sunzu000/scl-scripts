"""Create monthly ZIPs from copied transcripts; leave other backup files alone."""
import argparse
import re
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path


def is_transcript(path):
    return path.name in {'transcript.txt', 'transcript.json'} or path.name.endswith('_transcript.txt')


def month_for(relative):
    for part in reversed(relative.parts):
        match = re.search(r'(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?:[0-3]\d)?(?!\d)', part)
        if match:
            return ''.join(match.groups())
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('backup', type=Path)
    args = parser.parse_args()
    if not args.source.is_dir() or not args.backup.is_dir():
        raise SystemExit('Source or backup folder is missing.')
    groups = defaultdict(list)
    for original in sorted(args.source.rglob('*')):
        if not original.is_file() or not is_transcript(original):
            continue
        relative = original.relative_to(args.source)
        copied = args.backup / relative
        if not copied.is_file():
            raise SystemExit(f'Copied transcript missing: {relative}')
        month = month_for(relative)
        if month is None:
            month = 'undated'
            print(f'WARNING: month not found: {relative}', flush=True)
        groups[month].append((copied, relative))
    output = args.backup / 'TranscriptZIP'
    output.mkdir(exist_ok=True)
    for month, files in sorted(groups.items()):
        target = output / f'{month}_transcripts.zip'
        with tempfile.NamedTemporaryFile(dir=output, suffix='.tmp', delete=False) as tmp:
            temporary = Path(tmp.name)
        try:
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for copied, relative in files:
                    archive.write(copied, relative.as_posix())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        print(f'Created: {target} ({len(files)} files)', flush=True)
    print('Monthly ZIP backup completed.', flush=True)


if __name__ == '__main__':
    main()
