# -*- coding: utf-8 -*-
"""Route *new* validated LIVE digests to a review queue without copying media.

Approved/historical LIVE digests are never moved by this module. Do not use this
module to approve content; the user manually reviews, moves and renames videos.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat

LIVE_ROOT = Path(r'F:\SCL\TikTok')
UNREVIEWED_ROOT = Path(r'F:\SCL\Google Drive\TEMP\M\MOVIE\未視聴')
MANIFEST = 'digest_delivery.json'
SCHEMA = 'tiktok-digest-review-delivery-v1'


def _is_link(path):
    path = Path(path)
    try:
        if path.is_symlink():
            return True
        if os.name == 'nt':
            return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return False
    except FileNotFoundError:
        return False
    except (OSError, AttributeError):
        return True


def _check_paths(folder, date, live_root, pending_root):
    if not re.fullmatch(r'20\d{6}', date):
        raise ValueError('DATE_MUST_BE_YYYYMMDD')
    folder, live_root, pending_root = map(Path, (folder, live_root, pending_root))
    if not live_root.is_dir() or _is_link(live_root):
        raise RuntimeError('LIVE_ROOT_NOT_READY')
    if not folder.is_dir() or _is_link(folder) or not folder.resolve().is_relative_to(live_root.resolve()):
        raise RuntimeError('INVALID_LIVE_FOLDER')
    relative = folder.resolve().relative_to(live_root.resolve()).parts
    if relative != (date[:4], date[:6], date+'_LIVE'):
        raise RuntimeError('LIVE_FOLDER_DATE_MISMATCH')
    # Do not create arbitrary Drive roots or follow junctions. The review folder
    # is expected to have been created through the user's existing Drive setup.
    if not pending_root.is_dir() or _is_link(pending_root):
        raise RuntimeError('UNREVIEWED_FOLDER_NOT_READY')
    for path in (pending_root.parent, pending_root.parent.parent, pending_root.parent.parent.parent):
        if _is_link(path):
            raise RuntimeError('REVIEW_PARENT_IS_LINK')
    if os.stat(folder).st_dev != os.stat(pending_root).st_dev:
        raise RuntimeError('CROSS_VOLUME_MOVE_FORBIDDEN_NO_COPY')
    return folder, pending_root


def _read_manifest(folder):
    path = folder / MANIFEST
    if not path.is_file():
        return None
    if _is_link(path):
        raise RuntimeError('DELIVERY_MANIFEST_IS_LINK')
    result = json.loads(path.read_text(encoding='utf-8-sig'))
    if result.get('schema') != SCHEMA:
        raise RuntimeError('UNKNOWN_DELIVERY_MANIFEST_VERSION')
    return result


def _small_hash(path):
    path = Path(path)
    if not path.is_file() or _is_link(path):
        raise RuntimeError('PLAYLIST_MISSING_OR_LINK')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _save_json(path, payload):
    path = Path(path)
    if path.exists() and _is_link(path):
        raise RuntimeError('REFUSE_LINK_JSON')
    tmp = path.with_name(path.name + '.delivery-new.tmp')
    if tmp.exists():
        raise RuntimeError('STALE_DELIVERY_TEMP_FILE')
    with tmp.open('x', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def _candidate_path(data, folder, date, pending_root):
    value = data.get('pending_path')
    if not isinstance(value, str) or not value:
        raise RuntimeError('DELIVERY_PATH_MISSING')
    pending = Path(value)
    if pending.parent.resolve() != pending_root.resolve():
        raise RuntimeError('DELIVERY_OUTSIDE_REVIEW_FOLDER')
    if not re.fullmatch(re.escape(date)+r'_digest_UNREVIEWED_v\d{2}_[0-9a-f]{8}\.mp4', pending.name):
        raise RuntimeError('DELIVERY_FILENAME_INVALID')
    return pending


def find_existing(folder, date, *, live_root=LIVE_ROOT, pending_root=UNREVIEWED_ROOT):
    """Return an already-staged candidate. Refuse ambiguity and missing files."""
    folder, pending_root = _check_paths(folder, date, live_root, pending_root)
    existing = _read_manifest(folder)
    playlist_sha = _small_hash(folder/'playlist.json')
    final = folder / f'{date}_digest.mp4'
    if existing is None:
        # A crash between rename and manifest write must never trigger duplicate
        # rendering, even if a future caller has not created a manifest.
        others = list(pending_root.glob(f'{date}_digest_UNREVIEWED_v*.mp4'))
        if others and not final.exists():
            raise RuntimeError('UNTRACKED_REVIEW_CANDIDATE_REQUIRES_MANUAL_CHECK')
        return None
    if existing.get('status') not in {'awaiting_review', 'delivery_prepared'}:
        raise RuntimeError('UNEXPECTED_DELIVERY_STATE')
    if existing.get('date') != date or existing.get('playlist_sha256') != playlist_sha:
        raise RuntimeError('DELIVERY_PLAYLIST_CHANGED_REQUIRES_MANUAL_REVIEW')
    pending = _candidate_path(existing, folder, date, pending_root)
    pending_exists = pending.is_file() and not _is_link(pending)
    final_exists = final.is_file() and not _is_link(final)
    if pending_exists and final_exists:
        raise RuntimeError('DUPLICATE_LIVE_AND_UNREVIEWED_REQUIRES_MANUAL_CHECK')
    if existing.get('status') == 'delivery_prepared':
        raise RuntimeError('INTERRUPTED_DELIVERY_NEEDS_MANUAL_RECONCILIATION')
    if pending_exists:
        if pending.stat().st_size != existing.get('size_bytes'):
            raise RuntimeError('PENDING_FILE_SIZE_CHANGED')
        return pending
    if final_exists:
        # The user may have manually accepted/moved the pending file. Never
        # call this an automatic approval or modify the status on their behalf.
        return final
    raise RuntimeError('RECORDED_CANDIDATE_MISSING_REQUIRES_MANUAL_CHECK')


def deliver_new(folder, date, *, live_root=LIVE_ROOT, pending_root=UNREVIEWED_ROOT):
    """Atomically rename one newly rendered canonical LIVE digest to review.

    Caller must hold the existing processing.lock, must have validated the
    digest with ffprobe, and must invoke only when this run rendered it fresh.
    """
    folder, pending_root = _check_paths(folder, date, live_root, pending_root)
    if _read_manifest(folder) is not None:
        raise RuntimeError('DELIVERY_MANIFEST_ALREADY_EXISTS')
    playlist_sha = _small_hash(folder/'playlist.json')
    src = folder / f'{date}_digest.mp4'
    if not src.is_file() or _is_link(src) or src.stat().st_size < 2048:
        raise RuntimeError('NEW_DIGEST_MISSING_OR_TOO_SMALL')
    if list(pending_root.glob(f'{date}_digest_UNREVIEWED_v*.mp4')):
        raise RuntimeError('ANOTHER_CANDIDATE_EXISTS_NO_DUPLICATES')
    # Names include both date and version. The playlist hash distinguishes
    # candidates without hashing hundreds of MB of video.
    for version in range(1, 100):
        dest = pending_root / f'{date}_digest_UNREVIEWED_v{version:02d}_{playlist_sha[:8]}.mp4'
        if not dest.exists() and not _is_link(dest):
            break
    else:
        raise RuntimeError('REVIEW_VERSION_EXHAUSTED')
    stat_src = src.stat()
    state_path = folder/'state.json'
    if not state_path.is_file() or _is_link(state_path):
        raise RuntimeError('STATE_MISSING_OR_LINK')
    state = json.loads(state_path.read_text(encoding='utf-8-sig'))
    if not str(state.get('status', '')).startswith('completed') or state.get('review_required') is not True:
        raise RuntimeError('DIGEST_NOT_A_NEW_REVIEW_CANDIDATE')
    manifest = dict(
        schema=SCHEMA, status='delivery_prepared', date=date, review_required=True,
        pending_path=str(dest), pending_filename=dest.name,
        original_live_path=str(src), live_folder=str(folder),
        playlist_sha256=playlist_sha, size_bytes=stat_src.st_size,
        original_mtime_ns=stat_src.st_mtime_ns,
        prepared_at=dt.datetime.now().astimezone().isoformat(),
        note='User must listen, then manually move and rename to YYYYMMDD_digest.mp4 in the LIVE folder; no auto-approval.',
    )
    manifest_path = folder / MANIFEST
    _save_json(manifest_path, manifest)
    # os.rename does not replace an existing destination on Windows. Refuse
    # any preexisting path and avoid moving video across volumes or copying.
    if dest.exists() or _is_link(dest):
        raise RuntimeError('REVIEW_DESTINATION_COLLISION')
    os.rename(src, dest)
    if not dest.is_file() or dest.stat().st_size != stat_src.st_size:
        raise RuntimeError('DELIVERY_RENAME_VALIDATION_FAILED')
    manifest.update(status='awaiting_review', moved_at=dt.datetime.now().astimezone().isoformat())
    _save_json(manifest_path, manifest)
    # Update canonical JSON references, never video or approved playlist.
    render_path = folder/'render_result.json'
    if render_path.is_file() and not _is_link(render_path):
        result = json.loads(render_path.read_text(encoding='utf-8-sig'))
        result.update(output=str(dest), review_required=True, review_status='awaiting_review',
                      delivery_manifest=str(manifest_path))
        _save_json(render_path, result)
    state.update(output=str(dest), review_required=True, review_status='awaiting_review',
                 delivery_manifest=str(manifest_path))
    _save_json(state_path, state)
    return dest
