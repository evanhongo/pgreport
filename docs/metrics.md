# Metrics

## tup_returned & tup_fetched

|   | Description | Associated Scan Method |
| --- | --- | --- |
| `tup_returned` | Number of live rows read directly from data pages. | Sequential scans, Index-only scans. |
| `tup_fetched` | Number of live rows accessed via a pointer from an index. | Index scans, Bitmap index scans. |

## 計算 Table 的平均可用空間比率

```sql
CREATE EXTENSION pg_freespacemap;

SELECT 
 count(*) AS "number of pages",
 pg_size_pretty(cast(avg(avail) AS bigint)) AS "Av. freespace size",
 round(100 * avg(avail)/8192 ,2) AS "Av. freespace ratio"
FROM 
 pg_freespace('YOUR_TABLE');
```

## Table & Index Sizes

```sql
SELECT 
  relname AS table_name,
  pg_size_pretty(pg_relation_size(relid)) AS table_size,
  pg_size_pretty(pg_indexes_size(relid)) AS indexes_size,
  pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM 
  pg_catalog.pg_statio_user_tables
ORDER BY 
  pg_total_relation_size(relid) DESC;
```

## 查 Autovacuum 觸發門檻

```sql
WITH settings AS (
  SELECT
    current_setting('autovacuum_vacuum_threshold')::int AS threshold,
    current_setting('autovacuum_vacuum_scale_factor')::float AS scale_factor
),
table_stats AS (
  SELECT
    schemaname,
    relname,
    n_live_tup,
    n_dead_tup,
    last_autovacuum
  FROM pg_stat_user_tables
),
calculated AS (
  SELECT
    t.schemaname,
    t.relname,
    t.n_live_tup,
    t.n_dead_tup,
    s.threshold + s.scale_factor * t.n_live_tup AS vacuum_trigger_threshold,
    t.last_autovacuum
  FROM table_stats t, settings s
)
SELECT
  schemaname,
  relname AS table_name,
  n_live_tup,
  n_dead_tup,
  round(vacuum_trigger_threshold) AS vacuum_trigger_threshold,
  CASE
    WHEN n_dead_tup >= vacuum_trigger_threshold THEN '⚠️ NEEDS VACUUM'
    ELSE '✅ OK'
  END AS status,
  last_autovacuum
FROM calculated
ORDER BY n_dead_tup DESC
```

## 查 Index Bloat

```sql
CREATE EXTENSION pgstattuple;
SELECT * FROM pgstatindex('employee_pkey');
```

```sql
SELECT
    relname,
    pg_size_pretty(pg_relation_size(oid)) as file_size,
    pg_size_pretty((pgstattuple(oid)).tuple_len) as actual_data
FROM pg_class
WHERE relname IN ('employee', 'employee_pkey');
```

## 看 I/O 熱點

- `seq_scan` 次數很高但有 index 代表 index 沒用到
- `idx_scan` 太低代表浪費空間的 cold index
- `n_dead_tup` 很多代表 autovacuum 沒跟上

```sql
SELECT
 relname AS table_name,
 seq_scan, 
 idx_scan,
 n_dead_tup,
 n_live_tup,  
 n_tup_ins,
 n_tup_upd,
 n_tup_del,
 last_vacuum,
 last_autovacuum
FROM 
 pg_catalog.pg_stat_user_tables
WHERE 
 schemaname = 'public'
ORDER BY 2 DESC;
```

## 查 Slow Queries

```sql
SELECT
  query,
  calls,
  total_exec_time,
  mean_exec_time,
  rows
FROM 
  pg_stat_statements
ORDER BY 
  mean_exec_time DESC
LIMIT 10;
```

## Finding Queries with High Disk I/O

```sql
SELECT
    query,
    calls,
    shared_blks_read,
    shared_blks_hit,
    shared_blks_read::float / (shared_blks_hit + shared_blks_read) * 100 AS read_percentage
FROM pg_stat_statements
WHERE shared_blks_read + shared_blks_hit > 1000
ORDER BY shared_blks_read DESC
LIMIT 10;
```

## 檢查目前的查詢活動

```sql
SELECT 
  pid, 
  state, 
  query, 
  now() - query_start AS duration
FROM 
  pg_stat_activity
WHERE 
  state <> 'idle'
ORDER BY 
  duration DESC;
```

## 查被卡住的 SQL

```sql
SELECT blocked_locks.pid AS blocked_pid,
       blocking_locks.pid AS blocking_pid,
       blocked_activity.usename AS blocked_user,
       blocking_activity.usename AS blocking_user,
       blocked_activity.query_start AS blocked_since,
       blocking_activity.query_start AS blocking_since,
       blocked_activity.query AS blocked_query,
       blocking_activity.query AS blocking_query,
       blocked_locks.mode AS blocked_mode
FROM pg_locks blocked_locks
JOIN pg_stat_activity blocked_activity ON blocked_activity.pid = blocked_locks.pid
JOIN pg_locks blocking_locks ON blocking_locks.locktype = blocked_locks.locktype
    AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
    AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
    AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
    AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
    AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
    AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
    AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
    AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
    AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
    AND blocking_locks.pid != blocked_locks.pid
JOIN pg_stat_activity blocking_activity ON blocking_activity.pid = blocking_locks.pid;
```
