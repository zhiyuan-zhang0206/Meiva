-- Strip empty string llm_model if present in config_overlay or birth_config
UPDATE agents_meta
SET config_overlay = config_overlay - 'llm_model'
WHERE config_overlay IS NOT NULL
  AND length(btrim(COALESCE(config_overlay->>'llm_model', ' '))) = 0
  AND (config_overlay ? 'llm_model');

UPDATE agents_meta
SET birth_config = birth_config - 'llm_model'
WHERE birth_config IS NOT NULL
  AND length(btrim(COALESCE(birth_config->>'llm_model', ' '))) = 0
  AND (birth_config ? 'llm_model');

-- For external harness rows without an explicit model in config_overlay or birth_config,
-- backfill from the latest observed model from agent_metric_observations when available.
WITH latest_obs AS (
    SELECT DISTINCT ON (agent_id) agent_id, model
    FROM agent_metric_observations
    WHERE model IS NOT NULL AND length(btrim(model)) > 0
    ORDER BY agent_id, occurred_at DESC
)
UPDATE agents_meta a
SET birth_config = jsonb_set(COALESCE(a.birth_config, '{}'::jsonb), '{llm_model}', to_jsonb(lo.model))
FROM latest_obs lo
WHERE a.id = lo.agent_id
  AND a.harness != 'meiva'
  AND (a.config_overlay IS NULL OR a.config_overlay->>'llm_model' IS NULL OR length(btrim(a.config_overlay->>'llm_model')) = 0)
  AND (a.birth_config IS NULL OR a.birth_config->>'llm_model' IS NULL OR length(btrim(a.birth_config->>'llm_model')) = 0);

-- Backfill any remaining rows missing llm_model in both config_overlay and birth_config
-- according to harness: 'meiva' -> 'deepseek-flash', 'claude_code' -> 'opus',
-- 'codex' -> 'gpt-5.4', 'antigravity' -> 'gemini-3-flash'.
UPDATE agents_meta
SET birth_config = jsonb_set(
    COALESCE(birth_config, '{}'::jsonb),
    '{llm_model}',
    to_jsonb(
        CASE harness
            WHEN 'claude_code' THEN 'opus'
            WHEN 'codex' THEN 'gpt-5.4'
            WHEN 'antigravity' THEN 'gemini-3-flash'
            ELSE 'deepseek-flash'
        END
    )
)
WHERE (config_overlay IS NULL OR config_overlay->>'llm_model' IS NULL OR length(btrim(config_overlay->>'llm_model')) = 0)
  AND (birth_config IS NULL OR birth_config->>'llm_model' IS NULL OR length(btrim(birth_config->>'llm_model')) = 0);
