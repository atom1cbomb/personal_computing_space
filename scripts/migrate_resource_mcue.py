"""Apply Observer schema migrations and explicit resource MCUE assignments.

Run with the upgraded release's LOOM_* database/content-store settings while
Observer and Driver are stopped. The mapping contains only resource URIs and
public attributes; never put credentials in it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from loom_v2.db.migrations import apply_migrations
from loom_v2.db.session import make_engine
from loom_v2.observer.mcue_migration import backfill_resource_mcue
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings


async def migrate(path: Path) -> None:
    settings = Settings()
    engine = make_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
            await apply_migrations(connection, "observer")
        repo = ObserverRepository(engine, settings=settings)
        await repo.ensure_mcue_definitions(settings.workspace_id)
        changed = await backfill_resource_mcue(repo, settings.workspace_id, json.loads(path.read_text()))
        print(json.dumps({"workspace": settings.workspace_id, "resources_migrated": changed}))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", required=True, type=Path)
    asyncio.run(migrate(parser.parse_args().mapping))
