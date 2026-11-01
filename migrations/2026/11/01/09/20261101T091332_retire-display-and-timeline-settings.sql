-- Retire removed display and timeline preference keys from `user_settings`:
--   * `display.show_activity_line` (agent row activity line retired)
--   * `display.show_timezone` (timestamps never append timezone names)
--   * `display.expand_runs_mode` (timeline details mode fixed to settled-collapsed / live-expanded)
--   * `display.compact_history_sessions` (compact history retention fixed to unlimited)
--   * `display.context_meter_width` (context usage bar width fixed to comfortable)
--   * `display.composer_context_meter` (context window gauge shown only in Inspector)
--   * `display.color.*` (timeline color palette customization retired)
DELETE FROM user_settings
WHERE key IN (
    'display.show_activity_line',
    'display.show_timezone',
    'display.expand_runs_mode',
    'display.compact_history_sessions',
    'display.context_meter_width',
    'display.composer_context_meter'
)
   OR key LIKE 'display.color.%';
