-- A single immutable extraction can be promoted only once.  Application-level
-- optimistic checks provide the normal stale-review response; this constraint is
-- the database backstop for concurrent writers and direct SQL clients.

ALTER TABLE verified_records
    ADD CONSTRAINT verified_records_extracted_id_key UNIQUE (extracted_id);
