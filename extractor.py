import argparse
import asyncio
import aiohttp
import aiosqlite
import json
import csv
import logging
import re
import signal
import sys
import time
import random
from pathlib import Path

# Section 1: Constants
DEFAULT_CONCURRENCY = 15
MAX_RETRIES = 5
BASE_BACKOFF = 1.0
MAX_BACKOFF = 60.0
BATCH_SIZE = 50
REQUEST_TIMEOUT = 30
QUEUE_MAXSIZE = 200

# Section 2: Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("botto-extractor")

# Section 8: Graceful Shutdown setup
shutdown_event = None

def handle_signal():
    log.warning("Shutdown signal received — finishing in-flight requests...")
    if shutdown_event:
        shutdown_event.set()

# Section 3: CLI Argument Parser
def parse_args() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(description="Extract NFT metadata and parse visual tags.")
    parser.add_argument("--contract", type=str, required=True, help="Contract address (e.g., 0xF5...)")
    parser.add_argument("--start", type=int, required=True, help="First token ID to fetch (inclusive)")
    parser.add_argument("--end", type=int, required=True, help="Last token ID to fetch (inclusive)")
    parser.add_argument("--api-key", type=str, required=True, help="Alchemy API key")
    parser.add_argument("--network", type=str, default="eth-mainnet", help="Alchemy network identifier")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="Max concurrent API requests")
    parser.add_argument("--keywords-file", type=str, default="keywords.json", help="Path to the keyword taxonomy file")
    parser.add_argument("--export", type=str, choices=["csv", "json"], help="Export format after extraction: csv or json")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return parser, parser.parse_args()

def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace):
    if args.start > args.end:
        parser.error("--start must be <= --end")
    if not args.contract.startswith("0x") or len(args.contract) != 42 or not all(c in '0123456789abcdefABCDEF' for c in args.contract[2:]):
        raise ValueError("--contract must start with '0x' and be a 42-character valid hex address.")
    if not Path(args.keywords_file).is_file():
        raise FileNotFoundError(f"--keywords-file '{args.keywords_file}' does not exist.")

# Section 4: Database Layer
async def init_db(contract_address: str) -> aiosqlite.Connection:
    db_name = f"{contract_address}_nft.db"
    db = await aiosqlite.connect(db_name)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA foreign_keys=ON")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            token_id    INTEGER PRIMARY KEY,
            name        TEXT,
            description TEXT,
            image_url   TEXT,
            raw_json    TEXT,
            fetched_at  TEXT DEFAULT (datetime('now'))
        );
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            token_id INTEGER NOT NULL,
            tag      TEXT    NOT NULL,
            PRIMARY KEY (token_id, tag),
            FOREIGN KEY (token_id) REFERENCES tokens(token_id) ON DELETE CASCADE
        );
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);")
    await db.commit()
    return db

async def get_fetched_ids(db: aiosqlite.Connection, start: int, end: int) -> set[int]:
    cursor = await db.execute(
        "SELECT token_id FROM tokens WHERE token_id BETWEEN ? AND ?",
        (start, end)
    )
    rows = await cursor.fetchall()
    return {row[0] for row in rows}

async def write_batch(db: aiosqlite.Connection, batch: list[dict]) -> int:
    if not batch:
        return 0

    try:
        for item in batch:
            await db.execute(
                "INSERT OR IGNORE INTO tokens (token_id, name, description, image_url, raw_json) VALUES (?, ?, ?, ?, ?)",
                (item["token_id"], item["name"], item["description"], item["image_url"], item["raw_json"])
            )
            for tag in item.get("tags", []):
                await db.execute(
                    "INSERT OR IGNORE INTO tags (token_id, tag) VALUES (?, ?)",
                    (item["token_id"], tag)
                )
        await db.commit()
        log.info(f"Flushed {len(batch)} tokens to database")
        return len(batch)
    except Exception as e:
        await db.rollback()
        log.error(f"Failed to flush batch to database: {e}")
        return 0

# Section 5: Alchemy API Client
async def fetch_nft_metadata(
    session: aiohttp.ClientSession,
    api_url: str,
    contract: str,
    token_id: int,
    stats: dict,
) -> dict | None:
    url = f"{api_url}?contractAddress={contract}&tokenId={token_id}&refreshCache=false"

    for attempt in range(MAX_RETRIES + 1):
        if shutdown_event.is_set():
            return None

        delay = BASE_BACKOFF
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    try:
                        data = await response.json()
                        stats["fetched"] += 1
                        return data
                    except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
                        log.warning(f"Token {token_id}: 200 OK but malformed JSON: {e}")
                        stats["errors"] += 1
                        return None
                elif response.status == 429:
                    stats["retries"] += 1
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = int(retry_after)
                        except ValueError:
                            delay = min(BASE_BACKOFF * (2 ** attempt) + random.uniform(0, 1), MAX_BACKOFF)
                    else:
                        delay = min(BASE_BACKOFF * (2 ** attempt) + random.uniform(0, 1), MAX_BACKOFF)
                elif response.status in {500, 502, 503, 504}:
                    stats["retries"] += 1
                    delay = min(BASE_BACKOFF * (2 ** attempt) + random.uniform(0, 1), MAX_BACKOFF)
                elif response.status in {400, 404}:
                    log.warning(f"Token {token_id} permanently failed with status {response.status}")
                    stats["errors"] += 1
                    return None
                else:
                    stats["retries"] += 1
                    delay = min(BASE_BACKOFF * (2 ** attempt) + random.uniform(0, 1), MAX_BACKOFF)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            stats["retries"] += 1
            delay = min(BASE_BACKOFF * (2 ** attempt) + random.uniform(0, 1), MAX_BACKOFF)
        except Exception as e:
            log.error(f"Unexpected error fetching token {token_id}: {e}")
            stats["errors"] += 1
            return None

        if attempt < MAX_RETRIES:
            await asyncio.sleep(delay)
        else:
            log.error(f"Max retries reached for token {token_id}.")
            stats["errors"] += 1
            return None

def parse_nft_response(response: dict, token_id: int) -> dict | None:
    name = response.get("name") or ""
    description = response.get("description")

    if not description:
        description = (response.get("metadata") or {}).get("description")

    if not description:
        raw = response.get("raw") or {}
        metadata = raw.get("metadata") or {}
        description = metadata.get("description")

    if not description:
        return None

    image_url = (response.get("image") or {}).get("cachedUrl") or ""
    return {
        "token_id": token_id,
        "name": name,
        "description": description,
        "image_url": image_url,
        "raw_json": json.dumps(response)
    }

# Section 6: Keyword Extraction Engine
def load_keywords(filepath: str) -> list[str]:
    with open(filepath, 'r') as f:
        data = json.load(f)
        keywords = data.get("keywords", [])
    if not keywords or not isinstance(keywords, list):
        raise ValueError("Keywords file must contain a non-empty list of strings under 'keywords'.")
    # Sort by length descending
    return sorted(keywords, key=len, reverse=True)

def compile_keyword_pattern(keywords: list[str]) -> re.Pattern:
    escaped = [re.escape(kw) for kw in keywords]
    pattern = re.compile(
        r'\b(' + '|'.join(escaped) + r')\b',
        re.IGNORECASE
    )
    return pattern

def extract_tags(description: str, pattern: re.Pattern) -> list[str]:
    matches = pattern.findall(description)
    tags = [m.lower() for m in matches]
    return list(dict.fromkeys(tags))

# Section 7: Producer-Consumer Orchestrator
async def producer(
    session: aiohttp.ClientSession,
    queue: asyncio.Queue,
    api_url: str,
    contract: str,
    token_ids: list[int],
    keyword_pattern: re.Pattern,
    stats: dict,
    concurrency: int,
):
    total = len(token_ids)
    stats["processed"] = 0

    work_queue = asyncio.Queue()
    for tid in token_ids:
        work_queue.put_nowait(tid)

    async def worker():
        while not work_queue.empty():
            if shutdown_event.is_set():
                break

            try:
                token_id = work_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            response = await fetch_nft_metadata(session, api_url, contract, token_id, stats)
            if response:
                parsed = parse_nft_response(response, token_id)
                if parsed:
                    parsed["tags"] = extract_tags(parsed["description"], keyword_pattern)
                    await queue.put(parsed)
                else:
                    stats["skipped"] += 1

            stats["processed"] += 1
            processed = stats["processed"]
            if processed % 100 == 0:
                log.info(f"Progress: {processed}/{total} ({(processed/total)*100:.1f}%) | "
                         f"Errors: {stats['errors']} | Retries: {stats['retries']}")

            work_queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
    if workers:
        await asyncio.gather(*workers)

    await queue.put(None)

async def consumer(
    db: aiosqlite.Connection,
    queue: asyncio.Queue,
    stats: dict,
):
    buffer = []
    while True:
        item = await queue.get()
        if item is None:
            if buffer:
                await write_batch(db, buffer)
            queue.task_done()
            break

        buffer.append(item)
        if len(buffer) >= BATCH_SIZE:
            await write_batch(db, buffer)
            buffer = []
        queue.task_done()

async def run(args: argparse.Namespace):
    global shutdown_event
    shutdown_event = asyncio.Event()

    # Setup signal handling for graceful shutdown
    if sys.platform != 'win32':
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, handle_signal)
            loop.add_signal_handler(signal.SIGTERM, handle_signal)
        except NotImplementedError:
            signal.signal(signal.SIGINT, lambda sig, frame: handle_signal())
    else:
        signal.signal(signal.SIGINT, lambda sig, frame: handle_signal())

    keywords = load_keywords(args.keywords_file)
    keyword_pattern = compile_keyword_pattern(keywords)

    db = await init_db(args.contract)
    fetched_ids = await get_fetched_ids(db, args.start, args.end)

    all_ids = set(range(args.start, args.end + 1))
    remaining_ids = sorted(list(all_ids - fetched_ids))

    log.info(f"Starting extraction: {len(remaining_ids)}/{len(all_ids)} tokens to fetch ({len(fetched_ids)} cached)")

    if not remaining_ids:
        log.info("All requested tokens have already been fetched.")
    else:
        api_url = f"https://{args.network}.g.alchemy.com/nft/v3/{args.api_key}/getNFTMetadata"
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)

        stats = {"fetched": 0, "skipped": 0, "errors": 0, "retries": 0, "processed": 0}

        async with aiohttp.ClientSession(timeout=timeout) as session:
            producer_task = asyncio.create_task(
                producer(session, queue, api_url, args.contract, remaining_ids, keyword_pattern, stats, args.concurrency)
            )
            consumer_task = asyncio.create_task(
                consumer(db, queue, stats)
            )

            await asyncio.gather(producer_task, consumer_task)

        log.info("Extraction complete.")
        log.info(f"Stats: Fetched={stats['fetched']}, Skipped={stats['skipped']}, "
                 f"Errors={stats['errors']}, Retries={stats['retries']}")

    if args.export:
        await export_data(db, args.contract, args.export)

    await db.close()

# Section 9: Export Functions
async def export_data(db: aiosqlite.Connection, contract: str, fmt: str):
    log.info(f"Exporting data to {fmt}...")
    cursor = await db.execute("""
        SELECT t.token_id, t.name, t.description, t.image_url, t.fetched_at,
               GROUP_CONCAT(g.tag, '|') AS tags
        FROM tokens t
        LEFT JOIN tags g ON t.token_id = g.token_id
        GROUP BY t.token_id
        ORDER BY t.token_id;
    """)
    rows = await cursor.fetchall()

    if fmt == "csv":
        filename = f"{contract}_export.csv"
        with open(filename, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["token_id", "name", "description", "image_url", "fetched_at", "tags"])
            for row in rows:
                writer.writerow(row)
        log.info(f"Exported to {filename}")
    elif fmt == "json":
        filename = f"{contract}_export.json"
        data = []
        for row in rows:
            data.append({
                "token_id": row[0],
                "name": row[1],
                "description": row[2],
                "image_url": row[3],
                "tags": row[5].split('|') if row[5] else [],
                "fetched_at": row[4]
            })
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        log.info(f"Exported to {filename}")

# Section 10: Entry Point
def main():
    parser, args = parse_args()
    validate_args(parser, args)
    if args.verbose:
        log.setLevel(logging.DEBUG)

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        log.info("Interrupted by user.")

if __name__ == "__main__":
    main()
