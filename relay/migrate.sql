-- One-time change for databases made before 2026-10-27 (see schema.sql). Run once, before
-- deploying the matching src/index.js:
--   node node_modules/wrangler/bin/wrangler.js d1 execute car-review-relay --remote --file migrate.sql
ALTER TABLE boxes ADD COLUMN bytes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE boxes ADD COLUMN day INTEGER NOT NULL DEFAULT 0;
ALTER TABLE boxes ADD COLUMN writes INTEGER NOT NULL DEFAULT 0;
UPDATE boxes SET bytes = (SELECT coalesce(sum(length(data)), 0) FROM files WHERE files.box = boxes.box);
