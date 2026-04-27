"""Standalone runner for fetching Restream clips."""
import asyncio
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from backend.database import async_session
from backend.services.restream_clips import fetch_restream_clips_for_project


async def main():
    if len(sys.argv) < 2:
        print("Usage: fetch_restream_runner.py <project_id>")
        sys.exit(1)
    project_id = sys.argv[1]
    async with async_session() as db:
        await fetch_restream_clips_for_project(project_id, db)


if __name__ == "__main__":
    asyncio.run(main())
