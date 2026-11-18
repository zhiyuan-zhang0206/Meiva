ALTER TABLE agents_meta ADD COLUMN IF NOT EXISTS last_turn_fatal_reason TEXT;

COMMENT ON COLUMN agents_meta.last_turn_fatal_reason IS
    'Sanitized, bounded summary of the most recent fatal turn error since the last '
    'completed turn, normal self-termination, or resurrect. Written alongside '
    'last_turn_fatal_at by stamp_turn_fatal; cleared on turn completion, self-terminate, '
    'and resurrect. NULL = no active fatal turn error.';
