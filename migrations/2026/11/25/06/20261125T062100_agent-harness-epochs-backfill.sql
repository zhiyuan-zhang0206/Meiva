WITH parsed AS (
    SELECT i.agent_id,
           i.id,
           i.created_at AS at,
           f.found[1] AS from_harness,
           f.found[2] AS to_harness,
           CASE WHEN strpos(i.content, E'\n') > 0
                THEN NULLIF(btrim(left(substr(i.content, strpos(i.content, E'\n') + 1), 500)), '')
           END AS reason
    FROM inbound_messages i
    CROSS JOIN LATERAL (
        SELECT regexp_match(
            i.content,
            '^\[Harness switched from ''(meiva|claude_code|codex|antigravity|kimi_code)'' to ''(meiva|claude_code|codex|antigravity|kimi_code)''\]'
        ) AS found
    ) f
    WHERE i.kind = 'system_note' AND f.found IS NOT NULL
),
ordered AS (
    SELECT p.*,
           row_number() OVER w AS n,
           lag(p.to_harness) OVER w AS prev_to,
           lead(p.at) OVER w AS next_at
    FROM parsed p
    WINDOW w AS (PARTITION BY p.agent_id ORDER BY p.at, p.id)
),
replayable AS (
    SELECT o.agent_id
    FROM ordered o
    JOIN agents_meta m ON m.id = o.agent_id
    GROUP BY o.agent_id, m.harness
    HAVING bool_and(o.from_harness <> o.to_harness)
       AND bool_and(o.prev_to IS NULL OR o.prev_to = o.from_harness)
       AND (array_agg(o.to_harness ORDER BY o.n DESC))[1] = m.harness
),
stretches AS (
    SELECT o.agent_id, 0 AS seq, o.from_harness AS harness,
           LEAST(m.spawned_at, o.at) AS started_at, o.at AS ended_at, NULL::text AS reason
    FROM ordered o
    JOIN agents_meta m ON m.id = o.agent_id
    WHERE o.n = 1 AND o.agent_id IN (SELECT agent_id FROM replayable)
    UNION ALL
    SELECT o.agent_id, o.n::int, o.to_harness, o.at, o.next_at, o.reason
    FROM ordered o
    WHERE o.agent_id IN (SELECT agent_id FROM replayable)
    UNION ALL
    SELECT m.id, 0, m.harness, m.spawned_at, NULL, NULL
    FROM agents_meta m
    WHERE m.id NOT IN (SELECT agent_id FROM replayable)
)
INSERT INTO agent_harness_epochs
    (agent_id, seq, harness, model, started_at, ended_at, switched_by, reason)
SELECT s.agent_id,
       s.seq,
       s.harness,
       CASE WHEN s.ended_at IS NULL
            THEN COALESCE(m.config_overlay->>'llm_model', m.birth_config->>'llm_model')
       END,
       s.started_at,
       s.ended_at,
       'backfill',
       s.reason
FROM stretches s
JOIN agents_meta m ON m.id = s.agent_id
WHERE NOT EXISTS (SELECT 1 FROM agent_harness_epochs x WHERE x.agent_id = s.agent_id)
ON CONFLICT (agent_id, seq) DO NOTHING;
