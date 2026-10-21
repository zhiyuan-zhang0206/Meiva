CREATE OR REPLACE FUNCTION public.close_impersonation_event_admission(p_lease_id UUID)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $function$
BEGIN
    UPDATE public.agent_impersonations SET event_admission_closed_at=clock_timestamp()
    WHERE id=p_lease_id AND event_delivery_protocol_version = 2
      AND event_admission_closed_at IS NULL;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    -- A lease that already ended (termination closes admission after ending it)
    -- completes here when every source is sealed; otherwise this is a no-op.
    PERFORM public.finalize_impersonation_event_log(p_lease_id);
    RETURN TRUE;
END;
$function$;

CREATE OR REPLACE FUNCTION public.finalize_impersonation_event_log(p_lease_id UUID)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $function$
DECLARE
    lease public.agent_impersonations%ROWTYPE;
    entry_no BIGINT;
    participants BIGINT;
    sdk_count BIGINT;
    api_count BIGINT;
BEGIN
    SELECT * INTO lease FROM public.agent_impersonations WHERE id=p_lease_id FOR UPDATE;
    IF NOT FOUND OR lease.event_delivery_protocol_version IS DISTINCT FROM 2 THEN
        RETURN FALSE;
    END IF;
    IF lease.events_completed_at IS NOT NULL THEN
        RETURN TRUE;
    END IF;
    IF lease.ended_at IS NULL OR lease.event_admission_closed_at IS NULL THEN
        RETURN FALSE;
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.agent_impersonation_event_participants
        WHERE lease_id=p_lease_id AND state <> 'sealed'
    ) THEN
        RETURN FALSE;
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.agent_impersonation_event_participants p
        WHERE p.lease_id=p_lease_id AND p.item_count IS DISTINCT FROM (
            SELECT count(*) FROM public.agent_impersonation_entries e
            WHERE e.lease_id=p.lease_id AND e.source_key=p.source_key
        )
    ) THEN
        RAISE EXCEPTION 'Sealed source count differs from its recorded rows';
    END IF;
    UPDATE public.agent_impersonations
    SET events_completed_at=clock_timestamp(), handoff_document=NULL,
        event_delivery_pending_reason=NULL
    WHERE id=p_lease_id;
    UPDATE public.agent_impersonations SET next_entry=next_entry+1
    WHERE id=p_lease_id RETURNING next_entry-1 INTO entry_no;
    SELECT count(*) INTO participants FROM public.agent_impersonation_event_participants
    WHERE lease_id=p_lease_id;
    SELECT count(*) FILTER (WHERE kind='sdk_call'), count(*) FILTER (WHERE kind='api_event')
      INTO sdk_count, api_count
    FROM public.agent_impersonation_entries
    WHERE lease_id=p_lease_id AND source_key IS NOT NULL;
    INSERT INTO public.agent_impersonation_entries(lease_id,seq,kind,payload)
    VALUES(p_lease_id,entry_no,'lifecycle',jsonb_build_object(
        'event','event_delivery_complete',
        'event_count',sdk_count+api_count,
        'participant_count',participants,
        'sdk_call_count',sdk_count,
        'api_event_count',api_count
    ));
    RETURN TRUE;
END;
$function$;

-- Stop using the consent columns in this release; physical removal is a later contraction.
ALTER TABLE agent_impersonations ALTER COLUMN automatic SET DEFAULT TRUE;
UPDATE agent_impersonations SET handoff_applied_at=COALESCE(ended_at,clock_timestamp())
WHERE NOT automatic AND status NOT IN ('requested','accepted','active')
  AND handoff_applied_at IS NULL
  AND rejection_reason IS DISTINCT FROM 'aborted: manual consent retired; request a new automatic session';

-- Revoke unsupported takeovers without discarding their retained history or deltas.
SELECT close_impersonation_event_admission(l.id)
FROM agent_impersonations l JOIN agents_meta m ON m.id=l.agent_id
WHERE l.status IN ('requested','accepted','active')
  AND (NOT l.automatic OR m.harness <> 'meiva') AND l.event_delivery_protocol_version=2;
UPDATE agent_impersonations l SET status='expired',ended_at=clock_timestamp(),
    rejection_reason=CASE WHEN m.harness <> 'meiva' THEN 'aborted: target harness does not support impersonation'
        ELSE 'aborted: manual consent retired; request a new automatic session' END
FROM agents_meta m WHERE m.id=l.agent_id AND l.status IN ('requested','accepted','active')
  AND (NOT l.automatic OR m.harness <> 'meiva');
-- External harnesses have no native checkpoint handoff consumer.
UPDATE agent_impersonations l SET handoff_applied_at=COALESCE(l.handoff_applied_at,clock_timestamp())
FROM agents_meta m WHERE m.id=l.agent_id AND m.harness <> 'meiva';
UPDATE inbound_messages i SET status='done'
FROM agent_impersonations l WHERE i.agent_id=l.agent_id AND i.kind='reminder'
  AND i.status='pending' AND i.payload->>'lease_id'=l.id::text
  AND l.status NOT IN ('requested','accepted','active');

DROP INDEX IF EXISTS agent_impersonations_one_open;
CREATE UNIQUE INDEX agent_impersonations_one_open ON agent_impersonations(agent_id)
    WHERE status IN ('requested','accepted','active') OR delta_version > applied_version
        OR handoff_applied_at IS NULL;

CREATE OR REPLACE FUNCTION require_meiva_impersonation() RETURNS trigger AS $$
DECLARE
    target_harness TEXT;
BEGIN
    SELECT harness INTO target_harness FROM agents_meta WHERE id=NEW.agent_id FOR UPDATE;
    IF target_harness IS DISTINCT FROM 'meiva' THEN
        RAISE EXCEPTION 'Impersonation requires the Meiva harness';
    END IF;
    IF NOT NEW.automatic THEN
        RAISE EXCEPTION 'Manual impersonation consent is no longer supported';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS agent_impersonations_require_meiva ON agent_impersonations;
CREATE TRIGGER agent_impersonations_require_meiva
    BEFORE INSERT ON agent_impersonations
    FOR EACH ROW EXECUTE FUNCTION require_meiva_impersonation();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='meiva_runner') THEN
        REVOKE INSERT (automatic), UPDATE (consent_version) ON agent_impersonations FROM meiva_runner;
    END IF;
END $$;
