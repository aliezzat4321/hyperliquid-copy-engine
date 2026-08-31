\pset pager off
\set ON_ERROR_STOP on

SELECT 'DATABASE' AS section,
       current_database() AS database_name,
       pg_database_size(current_database()) AS bytes,
       pg_size_pretty(pg_database_size(current_database())) AS pretty_size;

SELECT 'ALL_DATABASES' AS section,
       datname AS database_name,
       pg_database_size(datname) AS bytes,
       pg_size_pretty(pg_database_size(datname)) AS pretty_size
FROM pg_database
WHERE datallowconn
ORDER BY pg_database_size(datname) DESC;

SELECT 'TABLES' AS section,
       schemaname,
       relname AS table_name,
       pg_total_relation_size(relid) AS total_bytes,
       pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
       pg_relation_size(relid) AS heap_bytes,
       pg_size_pretty(pg_relation_size(relid)) AS heap_size,
       pg_indexes_size(relid) AS index_bytes,
       pg_size_pretty(pg_indexes_size(relid)) AS index_size,
       n_live_tup,
       n_dead_tup,
       seq_scan,
       idx_scan,
       last_autovacuum,
       last_autoanalyze
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC;

SELECT 'INDEXES' AS section,
       schemaname,
       relname AS table_name,
       indexrelname AS index_name,
       pg_relation_size(indexrelid) AS index_bytes,
       pg_size_pretty(pg_relation_size(indexrelid)) AS index_size,
       idx_scan,
       idx_tup_read,
       idx_tup_fetch
FROM pg_stat_user_indexes
ORDER BY pg_relation_size(indexrelid) DESC
LIMIT 100;

SELECT 'TIME_COLUMNS' AS section,
       table_schema,
       table_name,
       column_name,
       data_type
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
  AND (
      column_name ILIKE '%time%'
      OR column_name ILIKE '%date%'
      OR column_name ILIKE '%created%'
      OR column_name ILIKE '%updated%'
      OR column_name ILIKE '%observed%'
      OR column_name ILIKE '%timestamp%'
  )
ORDER BY table_schema, table_name, ordinal_position;

SELECT 'SUMMARY' AS section,
       COUNT(*) AS user_tables,
       COALESCE(SUM(pg_total_relation_size(relid)), 0) AS relation_total_bytes,
       pg_size_pretty(COALESCE(SUM(pg_total_relation_size(relid)), 0)) AS relation_total_size,
       COALESCE(SUM(pg_relation_size(relid)), 0) AS heap_total_bytes,
       pg_size_pretty(COALESCE(SUM(pg_relation_size(relid)), 0)) AS heap_total_size,
       COALESCE(SUM(pg_indexes_size(relid)), 0) AS index_total_bytes,
       pg_size_pretty(COALESCE(SUM(pg_indexes_size(relid)), 0)) AS index_total_size,
       COALESCE(SUM(n_live_tup), 0)::bigint AS estimated_live_rows,
       COALESCE(SUM(n_dead_tup), 0)::bigint AS estimated_dead_rows
FROM pg_stat_user_tables;
