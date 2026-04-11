"""P4-V03: add lpr_results table."""

from pathlib import Path

MIGRATION_SQL = (Path(__file__).resolve().parent.parent / "add_lpr_results.sql").read_text()


async def upgrade(conn):
    """Execute the migration SQL statements."""
    for statement in MIGRATION_SQL.split(";"):
        trimmed = statement.strip()
        if trimmed:
            await conn.execute(trimmed)
