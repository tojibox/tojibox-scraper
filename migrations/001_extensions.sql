-- 001_extensions.sql
-- Enable required PostgreSQL extensions

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";      -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "pgcrypto";        -- encode/decode, digest
CREATE EXTENSION IF NOT EXISTS "pg_trgm";         -- trigram indexes for address search
