\pset pager off
\set ON_ERROR_STOP on

SELECT 'DATABASE' AS section,
       current_database() AS database_name,
       pg_database_size(current_database()) AS bytes,
       pg_size_pretty(pg_database_size(current_database())) AS pretty_size;

SELECT 'TABLES' AS section,
       schemaname,
       relname AS table_name,
       pg_total_relation_size(relid) AS total_bytes,
       pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
       pg_relation_size(relid) AS table_bytes,
       pg_size_pretty(pg_relation_size(relid)) AS table_size,
       pg_indexes_size(relid) AS index_bytes,
       pg_size_pretty(pg_indexes_size(relid)) AS index_size,
       n_live_tup,
       n_dead_tup,
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

SELECT 'TABLE_COLUMNS' AS section,
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
       SUM(pg_total_relation_size(relid)) AS relation_total_bytes,
       pg_size_pretty(SUM(pg_total_relation_size(relid))) AS relation_total_size,
       SUM(pg_relation_size(relid)) AS heap_total_bytes,
       pg_size_pretty(SUM(pg_relation_size(relid))) AS heap_total_size,
       SUM(pg_indexes_size(relid)) AS index_total_bytes,
       pg_size_pretty(SUM(pg_indexes_size(relid))) AS index_total_size,
       SUM(n_live_tup)::bigint AS estimated_live_rows,
       SUM(n_dead_tup)::bigint AS estimated_dead_rows
FROM pg_stat_user_tables;
