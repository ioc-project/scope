CREATE TABLE IF NOT EXISTS %s.normalized_events
(
    event_id FixedString(64),
    tenant_id String,
    application_id String,
    event_time DateTime64(9, 'UTC'),
    ingested_at DateTime64(9, 'UTC') DEFAULT now64(9),
    event_type LowCardinality(String),
    session_id String,
    session_sequence UInt64,
    trace_id String,
    span_id String,
    call_id String,
    message_id String,
    fork_id String,
    service_name LowCardinality(String),
    schema_version LowCardinality(String),
    normalizer_version LowCardinality(String),
    payload String,
    INDEX trace_id_bloom trace_id TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX span_id_bloom span_id TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(event_time)
ORDER BY (tenant_id, application_id, session_id, event_time, event_id)