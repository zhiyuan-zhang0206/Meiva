ALTER TABLE public.agents_meta
    ADD COLUMN IF NOT EXISTS harness_configured_at TIMESTAMPTZ;
