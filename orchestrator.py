import argparse
import asyncio
import aiohttp
import aiosqlite
import logging
import signal
import sys
import os

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s | Orchestrator | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("orchestrator")

shutdown_event = None

def handle_signal():
    log.warning("Shutdown signal received — initiating graceful shutdown...")
    if shutdown_event:
        shutdown_event.set()

async def init_db(db_path: str = "orchestrator.db") -> aiosqlite.Connection:
    db = await aiosqlite.connect(db_path)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS contracts (
            address TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            discovered_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """)
    await db.commit()
    return db

async def discover_contracts(
    session: aiohttp.ClientSession,
    db: aiosqlite.Connection,
    deployer: str,
    etherscan_key: str,
    poll_interval: int,
    start_block: int = 0
):
    url = "https://api.etherscan.io/api"
    current_start_block = start_block

    while not shutdown_event.is_set():
        params = {
            "module": "account",
            "action": "txlist",
            "address": deployer,
            "startblock": current_start_block,
            "endblock": 99999999,
            "page": 1,
            "offset": 100,
            "sort": "asc",
            "apikey": etherscan_key
        }

        try:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("status") == "1":
                        transactions = data.get("result", [])
                        new_contracts_found = 0
                        for tx in transactions:
                            block_number = int(tx.get("blockNumber", current_start_block))
                            current_start_block = max(current_start_block, block_number + 1)

                            to_addr = tx.get("to", "")
                            contract_addr = tx.get("contractAddress", "")

                            if (not to_addr or to_addr == "") and contract_addr:
                                # Ensure standard format
                                contract_addr = "0x" + contract_addr.lstrip("0x")

                                cursor = await db.execute(
                                    "SELECT status FROM contracts WHERE address = ?", (contract_addr,)
                                )
                                row = await cursor.fetchone()
                                if not row:
                                    await db.execute(
                                        "INSERT INTO contracts (address, status) VALUES (?, ?)",
                                        (contract_addr, "pending")
                                    )
                                    await db.commit()
                                    new_contracts_found += 1
                                    log.info(f"Discovered new contract: {contract_addr}")

                        if new_contracts_found > 0:
                            log.info(f"Discovery sweep completed. {new_contracts_found} new contracts found.")
                    elif data.get("message") == "No transactions found":
                        pass
                    else:
                        log.warning(f"Etherscan API warning: {data.get('message')} - {data.get('result')}")
                else:
                    log.error(f"Etherscan API error: HTTP {response.status}")
        except Exception as e:
            log.error(f"Discovery error: {e}")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass

async def process_contracts(
    db: aiosqlite.Connection,
    alchemy_key: str,
    poll_interval: int,
    concurrency: int,
    start_id: int,
    end_id: int
):
    while not shutdown_event.is_set():
        try:
            cursor = await db.execute("SELECT address FROM contracts WHERE status = 'pending' LIMIT 1")
            row = await cursor.fetchone()

            if row:
                contract_addr = row[0]
                log.info(f"Starting extraction pipeline for contract: {contract_addr}")

                await db.execute(
                    "UPDATE contracts SET status = 'processing', updated_at = datetime('now') WHERE address = ?",
                    (contract_addr,)
                )
                await db.commit()

                cmd = [
                    sys.executable, "extractor.py",
                    "--contract", contract_addr,
                    "--start", str(start_id),
                    "--end", str(end_id),
                    "--api-key", alchemy_key,
                    "--concurrency", str(concurrency)
                ]

                log.info(f"Running command: {' '.join(cmd)}")

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                stdout, stderr = await process.communicate()

                if process.returncode == 0:
                    log.info(f"Extraction pipeline completed successfully for {contract_addr}")
                    await db.execute(
                        "UPDATE contracts SET status = 'completed', updated_at = datetime('now') WHERE address = ?",
                        (contract_addr,)
                    )
                else:
                    log.error(f"Extraction pipeline failed for {contract_addr}. Return code: {process.returncode}")
                    if stderr:
                        log.error(f"Stderr: {stderr.decode().strip()}")
                    await db.execute(
                        "UPDATE contracts SET status = 'failed', updated_at = datetime('now') WHERE address = ?",
                        (contract_addr,)
                    )
                await db.commit()
            else:
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=poll_interval)
                except asyncio.TimeoutError:
                    pass
        except Exception as e:
            log.error(f"Processing error: {e}")
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass

async def run(args: argparse.Namespace):
    global shutdown_event
    shutdown_event = asyncio.Event()

    if sys.platform != 'win32':
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, handle_signal)
            loop.add_signal_handler(signal.SIGTERM, handle_signal)
        except NotImplementedError:
            signal.signal(signal.SIGINT, lambda sig, frame: handle_signal())
    else:
        signal.signal(signal.SIGINT, lambda sig, frame: handle_signal())

    db = await init_db()

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        discovery_task = asyncio.create_task(
            discover_contracts(
                session, db, args.deployer, args.etherscan_key, args.poll_interval, args.start_block
            )
        )

        processing_task = asyncio.create_task(
            process_contracts(db, args.alchemy_key, args.poll_interval, args.concurrency, args.start_id, args.end_id)
        )

        log.info(f"Orchestrator daemon started. Monitoring deployer {args.deployer}. Press Ctrl+C to exit.")

        await shutdown_event.wait()

        log.info("Waiting for background tasks to finish...")
        await asyncio.gather(discovery_task, processing_task, return_exceptions=True)

    await db.close()
    log.info("Orchestrator shutdown complete.")

def parse_args() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(description="Automated Orchestrator for NFT Metadata Extractor.")
    parser.add_argument("--deployer", type=str, required=True, help="Deployer wallet address to monitor")
    parser.add_argument("--etherscan-key", type=str, required=True, help="Etherscan API key for discovery")
    parser.add_argument("--alchemy-key", type=str, required=True, help="Alchemy API key for extraction")
    parser.add_argument("--poll-interval", type=int, default=60, help="Polling interval in seconds (default: 60)")
    parser.add_argument("--start-block", type=int, default=0, help="Block number to start discovery from")
    parser.add_argument("--concurrency", type=int, default=15, help="Extraction concurrency level (default: 15)")
    parser.add_argument("--start-id", type=int, default=1, help="First token ID to fetch (default: 1)")
    parser.add_argument("--end-id", type=int, default=6000, help="Last token ID to fetch (default: 6000)")
    return parser, parser.parse_args()

def main():
    parser, args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        log.info("Interrupted by user.")

if __name__ == "__main__":
    main()
