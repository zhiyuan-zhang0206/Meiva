-- Declared capability set for an external controller (task #5339).
--
-- A lease may declare which SDK surfaces its controller may use. Three stored
-- states are distinct:
--   * a JSON array - the granted capabilities, possibly empty (grants nothing);
--   * NULL - a lease issued before this vocabulary existed. It keeps the full
--     surface it was issued with. Nothing may read NULL as the minimal set;
--   * the row is malformed if the value is not an array, so the CHECK below
--     refuses it instead of letting a reader guess.
--
-- `capability_declared_by` records how the declaration arose (explicit,
-- caller-default, launcher-default, legacy) so an audit can tell a chosen set
-- from a defaulted one. It is nullable for rows written before this migration.
ALTER TABLE agent_impersonations
    ADD COLUMN IF NOT EXISTS capabilities JSONB,
    ADD COLUMN IF NOT EXISTS capability_declared_by TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'agent_impersonations'::regclass
          AND conname = 'agent_impersonations_capabilities_array'
    ) THEN
        ALTER TABLE agent_impersonations
            ADD CONSTRAINT agent_impersonations_capabilities_array
            CHECK (capabilities IS NULL OR jsonb_typeof(capabilities) = 'array');
    END IF;
END $$;

COMMENT ON COLUMN agent_impersonations.capabilities IS
    'Granted SDK capabilities for this lease''s controller; NULL is a pre-vocabulary lease and keeps its full issued surface.';
COMMENT ON COLUMN agent_impersonations.capability_declared_by IS
    'How the capability set arose: explicit, caller-default, launcher-default or legacy.';
