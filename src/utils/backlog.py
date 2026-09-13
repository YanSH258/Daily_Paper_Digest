"""Read-only admission preview and explicit, backed-up state apply/restore."""
from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from processing import admission_decision

FIELDS = ('processing_status', 'processing_reason', 'admitted_at', 'queued_at', 'date_source')


def _connect(path, writable=False):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + ('?mode=rw' if writable else '?mode=ro'), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}


def _rows(conn):
    columns = _columns(conn, 'articles')
    wanted = ('id', 'pub_date', 'date_source', 'pub_date_source', 'processing_status',
              'processing_reason', 'admitted_at', 'queued_at', 'score_status', 'processed')
    selected = [c for c in wanted if c in columns]
    if 'id' not in selected:
        raise ValueError('articles table is unavailable')
    rows = [dict(r) for r in conn.execute('SELECT ' + ','.join(selected) + ' FROM articles ORDER BY id')]
    flags = {r['id']: {} for r in rows}
    for field in ('starred', 'note', 'tags'):
        if field in columns:
            for row in conn.execute(f"SELECT id FROM articles WHERE COALESCE({field}, '') NOT IN ('', '0', 0)"):
                flags[row[0]][field] = True
    for table in ('chat_messages', 'highlights', 'topic_papers', 'digest_entries', 'digest_items'):
        if 'article_id' in _columns(conn, table):
            for row in conn.execute(f'SELECT DISTINCT article_id FROM {table}'):
                if row[0] in flags:
                    flags[row[0]][table] = True
    for row in rows:
        row['relations'] = flags[row['id']]
    return rows


def _plan(conn, run_date, config):
    decisions = []
    for row in _rows(conn):
        status = row.get('processing_status') or 'unreviewed'
        if row.get('score_status') in ('ok', 'success') or row.get('processed'):
            decision = {'decision': 'preserve_scored', 'reason': '已有处理结果，保持不变'}
        elif status != 'unreviewed':
            decision = {'decision': status, 'reason': row.get('processing_reason') or '已有准入记录，保持不变'}
        else:
            decision = admission_decision(row, run_date, config)
        decisions.append({'id': row['id'], **decision, 'will_change': status == 'unreviewed' and decision['decision'] != 'preserve_scored',
                          'relations': row['relations'], 'user_related': bool(row['relations'])})
    return {'decisions': decisions, 'counts': dict(Counter(r['decision'] for r in decisions)),
            'change_count': sum(r['will_change'] for r in decisions)}


def preview(db, run_date, timezone_name='Asia/Shanghai', days=3):
    conn = _connect(db)
    try:
        return _plan(conn, run_date, {'scheduler': {'timezone': timezone_name}, 'fetcher': {'date_filter_days': days}})
    finally:
        conn.close()


def _backup(conn, path):
    target = Path(path).with_name(Path(path).stem + '.backlog-' + uuid.uuid4().hex + '.db')
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return str(target)


def _ensure_fields(conn):
    columns = _columns(conn, 'articles')
    for field in FIELDS:
        if field not in columns:
            definition = "TEXT DEFAULT 'unreviewed'" if field == 'processing_status' else 'TEXT'
            conn.execute(f'ALTER TABLE articles ADD COLUMN {field} {definition}')


def apply(db, run_date, timezone_name='Asia/Shanghai', days=3):
    conn = _connect(db, writable=True)
    try:
        config = {'scheduler': {'timezone': timezone_name}, 'fetcher': {'date_filter_days': days}}
        # Validate before creating any backup or changing the schema.
        admission_decision({}, run_date, config)
        backup_path = _backup(conn, db)
        batch = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        conn.execute('BEGIN IMMEDIATE')
        _ensure_fields(conn)
        plan = _plan(conn, run_date, config)
        conn.execute('CREATE TABLE IF NOT EXISTS backlog_batches (batch_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, date_str TEXT, backup_path TEXT NOT NULL, records_json TEXT NOT NULL)')
        records = []
        for item in plan['decisions']:
            if not item['will_change']:
                continue
            old = dict(conn.execute('SELECT ' + ','.join(FIELDS) + ' FROM articles WHERE id=?', (item['id'],)).fetchone())
            new = dict(old, processing_status=item['decision'], processing_reason=item['reason'],
                       admitted_at=now, queued_at=now if item['decision'] == 'eligible' else old['queued_at'],
                       date_source=item.get('date_source'))
            conn.execute('UPDATE articles SET ' + ','.join(f'{f}=?' for f in FIELDS) + ' WHERE id=?',
                         [new[f] for f in FIELDS] + [item['id']])
            records.append({'id': item['id'], 'old': old, 'new': new})
        conn.execute('INSERT INTO backlog_batches VALUES(?,?,?,?,?)', (batch, now, run_date, backup_path, json.dumps(records)))
        conn.commit()
        return {'batch_id': batch, 'backup_path': backup_path, 'changed': len(records)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def restore(db, batch_id):
    conn = _connect(db, writable=True)
    try:
        _backup(conn, db)
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT records_json FROM backlog_batches WHERE batch_id=?', (batch_id,)).fetchone()
        if row is None:
            raise ValueError('unknown migration batch')
        restored = 0
        columns = _columns(conn, 'articles')
        guard = " AND COALESCE(score_status,'') NOT IN ('ok','success')" if 'score_status' in columns else ''
        guard += ' AND COALESCE(processed,0)=0' if 'processed' in columns else ''
        for record in json.loads(row[0]):
            cur = conn.execute('UPDATE articles SET ' + ','.join(f'{f}=?' for f in FIELDS) +
                               ' WHERE id=? AND ' + ' AND '.join(f'{f} IS ?' for f in FIELDS) + guard,
                               [record['old'][f] for f in FIELDS] + [record['id']] + [record['new'][f] for f in FIELDS])
            restored += cur.rowcount
        conn.commit()
        return restored
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--date')
    parser.add_argument('--timezone', default='Asia/Shanghai')
    parser.add_argument('--days', type=int, default=3)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--restore')
    args = parser.parse_args(argv)
    if not args.restore and not args.date:
        parser.error('--date is required for preview/apply')
    if args.restore:
        result = {'restored': restore(args.db, args.restore)}
    elif args.apply:
        result = apply(args.db, args.date, args.timezone, args.days)
    else:
        result = preview(args.db, args.date, args.timezone, args.days)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
