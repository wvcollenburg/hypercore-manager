-- The public address put in guest console links (plans/PLAN-roles.md, decision 14, 2026-11-20).
--
-- Guests reach HCM from the internet through a reverse proxy that publishes only /guest/, often under a
-- different name than the internal one in mail links (public_url). Set under Settings -> Mail by an
-- administrator of the whole installation; empty: HCM_GUEST_BASE_URL from .env, else the address of the
-- granter's browser.

ALTER TABLE config.mail_settings ADD COLUMN IF NOT EXISTS guest_url TEXT;
