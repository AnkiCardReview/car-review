-- Car Review relay storage. All file data is encrypted by the user's devices before upload.
CREATE TABLE IF NOT EXISTS boxes (
  box TEXT PRIMARY KEY,        -- one-way hash of the user's pairing secret
  write_hash TEXT NOT NULL,    -- SHA-256 of the box's write key, set by the first write
  created INTEGER NOT NULL     -- milliseconds since 1970
);

CREATE TABLE IF NOT EXISTS files (
  box TEXT NOT NULL,
  name TEXT NOT NULL,          -- e.g. "list", "audio/<hash>", "grades/<drive-id>"
  data BLOB NOT NULL,          -- encrypted bytes
  updated INTEGER NOT NULL,    -- milliseconds since 1970; rows older than 3 days are deleted
  PRIMARY KEY (box, name)
);

CREATE INDEX IF NOT EXISTS files_updated ON files (updated);
