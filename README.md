# Botto NFT Metadata Extractor

A Python async CLI tool that extracts NFT metadata from the Alchemy API for the Botto AI art project, parses generative prompts from description fields using Regex to identify aesthetic/visual keywords, and stores all data in a local SQLite database for downstream analysis.

## Prerequisites

- Python 3.11+
- Alchemy API key

## Installation

```bash
git clone <repo>
cd botto-extractor
pip install -r requirements.txt
```

## Usage examples

**Basic extraction**
```bash
python extractor.py \
  --contract 0xF5e0E37862fB3FdBbE83a4374B22e5704f7aD4E8 \
  --start 1 --end 6000 \
  --api-key YOUR_KEY
```

**With custom concurrency and export**
```bash
python extractor.py \
  --contract 0xF5e0E37862fB3FdBbE83a4374B22e5704f7aD4E8 \
  --start 1 --end 6000 \
  --api-key YOUR_KEY \
  --concurrency 20 \
  --export csv
```

**Resume a previously interrupted run**
Just re-run the same command you started with! The extractor skips tokens that already exist in the database.
```bash
python extractor.py \
  --contract 0xF5e0E37862fB3FdBbE83a4374B22e5704f7aD4E8 \
  --start 1 --end 6000 \
  --api-key YOUR_KEY
```

## Customizing keywords

You can modify the aesthetic taxonomy by editing `keywords.json`.
Simply add, remove, or modify items within the `"keywords"` array to tweak what tags are extracted from descriptions.

## Output files

- `<contract_address>_nft.db`: A local SQLite database containing the extracted data (tokens and their extracted tags).
- `<contract_address>_export.csv`: Generated if `--export csv` is passed. Contains a CSV table joining the tokens and their concatenated tags.
- `<contract_address>_export.json`: Generated if `--export json` is passed. Contains a JSON array of all tokens and their array of tags.

## Database schema

The script builds a normalized two-table schema for advanced SQL queries:

```sql
CREATE TABLE IF NOT EXISTS tokens (
    token_id    INTEGER PRIMARY KEY,
    name        TEXT,
    description TEXT,
    image_url   TEXT,
    raw_json    TEXT,          -- Full API response for future re-parsing
    fetched_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tags (
    token_id INTEGER NOT NULL,
    tag      TEXT    NOT NULL,
    PRIMARY KEY (token_id, tag),
    FOREIGN KEY (token_id) REFERENCES tokens(token_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
```
