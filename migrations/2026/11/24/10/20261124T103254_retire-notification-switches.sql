-- Retire the notification category switches from `user_settings`:
--   * `notifications.categories` (the per-category switch map of the question and unread pushes;
--     both pushes are now always on and nothing reads the key any more)
DELETE FROM user_settings WHERE key = 'notifications.categories';
