"""P3-V04: Add transit_distributions JSONB column and transit_time_stats materialized view."""
from pathlib import Path

MIGRATION_SQL = (Path(__file__).resolve().parent.parent / "add_transit_time_aggregate.sql").read_text()


async def upgrade(conn):
    """Execute the migration SQL statements."""
    for statement in MIGRATION_SQL.split(";"):
        trimmed = statement.strip()
        if trimmed:
            await conn.execute(trimmed)
