#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

OUT = Path('/root/hyperliquid-audit/postgres-storage')
OUT.mkdir(parents=True, exist_ok=True)


def psql(db: str, sql: str) -> list[list[str]]:
    cmd = [
        'sudo', '-n', '-u', 'postgres', 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
        '-d', db, '-At', '-F', '\t', '-c', sql,
    ]
    # sudo may emit a harmless cwd warning because postgres cannot traverse /root.
    # Keep stderr out of machine-readable query output so it can never be parsed
    # as a database/table row.
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return [line.split('\t') for line in proc.stdout.splitlines() if line.strip()]


dbs = [r[0] for r in psql('postgres', "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY pg_database_size(datname) DESC")]
report: dict = {
    'mode': 'READ_ONLY_NO_DATABASE_CHANGES',
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'databases': [],
}

for db in dbs:
    size = int(psql(db, 'SELECT pg_database_size(current_database())')[0][0])
    table_rows = psql(db, """
        SELECT n.nspname, c.relname,
               pg_total_relation_size(c.oid),
               pg_relation_size(c.oid),
               pg_indexes_size(c.oid),
               COALESCE(s.n_live_tup,0), COALESCE(s.n_dead_tup,0),
               COALESCE(s.seq_scan,0), COALESCE(s.idx_scan,0),
               COALESCE(s.last_autovacuum::text,''), COALESCE(s.last_autoanalyze::text,'')
        FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_stat_user_tables s ON s.relid=c.oid
        WHERE c.relkind IN ('r','m')
          AND n.nspname NOT IN ('pg_catalog','information_schema')
        ORDER BY pg_total_relation_size(c.oid) DESC
    """)
    tables = []
    for r in table_rows:
        tables.append({
            'schema': r[0], 'table': r[1], 'total_bytes': int(r[2]),
            'heap_bytes': int(r[3]), 'index_bytes': int(r[4]),
            'live_rows_estimate': int(r[5]), 'dead_rows_estimate': int(r[6]),
            'seq_scans': int(r[7]), 'idx_scans': int(r[8]),
            'last_autovacuum': r[9], 'last_autoanalyze': r[10],
        })
    index_rows = psql(db, """
        SELECT schemaname, relname, indexrelname, pg_relation_size(indexrelid),
               COALESCE(idx_scan,0)
        FROM pg_stat_user_indexes
        ORDER BY pg_relation_size(indexrelid) DESC
    """)
    indexes = [
        {'schema': r[0], 'table': r[1], 'index': r[2], 'bytes': int(r[3]), 'idx_scans': int(r[4])}
        for r in index_rows
    ]
    report['databases'].append({'database': db, 'bytes': size, 'tables': tables, 'indexes': indexes})

out = OUT / 'postgres_storage_audit.json'
tmp = out.with_suffix('.json.tmp')
tmp.write_text(json.dumps(report, indent=2) + '\n')
tmp.replace(out)

print('========== POSTGRESQL STORAGE AUDIT ==========')
print('mode=READ_ONLY_NO_DATABASE_CHANGES')
for d in report['databases']:
    print(f"DATABASE {d['database']} size_gib={d['bytes']/1024**3:.3f}")
    for t in d['tables'][:20]:
        print(
            f"TABLE {t['schema']}.{t['table']} total_gib={t['total_bytes']/1024**3:.3f} "
            f"heap_gib={t['heap_bytes']/1024**3:.3f} indexes_gib={t['index_bytes']/1024**3:.3f} "
            f"live={t['live_rows_estimate']} dead={t['dead_rows_estimate']} "
            f"seq={t['seq_scans']} idx={t['idx_scans']}"
        )
    print('TOP INDEXES')
    for i in d['indexes'][:15]:
        print(f"INDEX {i['schema']}.{i['index']} table={i['table']} size_gib={i['bytes']/1024**3:.3f} scans={i['idx_scans']}")
print(f'manifest={out}')
print('DATABASE_CHANGES_PERFORMED=NO')
