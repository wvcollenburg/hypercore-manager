-- Uploads of ISOs and disk images in progress (1.9.2). An upload that does not arrive completely is void: its
-- half ISO (or 'uploading-' disk image) is deleted from the cluster. The backup service does that at once when the
-- stream breaks; a row left here after the service itself stopped mid-upload is cleaned up when it starts again.
CREATE TABLE IF NOT EXISTS backup.media_uploads (
    id          BIGSERIAL   PRIMARY KEY,
    cluster_id  UUID        NOT NULL REFERENCES config.clusters (cluster_id) ON DELETE CASCADE,
    kind        TEXT        NOT NULL CHECK (kind IN ('iso', 'disk')),
    name        TEXT        NOT NULL,
    hc_uuid     TEXT,
    size        BIGINT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
