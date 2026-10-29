"""Compare the database's migration state with the migrations shipped in this build."""

from __future__ import annotations

from importlib.resources import files

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection


def expected_heads() -> frozenset[str]:
    """Head revisions of the migrations packaged with the code."""
    directory = ScriptDirectory(str(files("aiops") / "migrations"))
    return frozenset(directory.get_heads())


def current_heads(connection: Connection) -> frozenset[str]:
    """Revisions the database is at; empty before the first migration."""
    return frozenset(MigrationContext.configure(connection).get_current_heads())
