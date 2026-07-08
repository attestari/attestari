"""Apply the Notari schema to a Postgres database — no repo clone needed.

The schema ships inside the package (`notari/db/schema.sql`), so a pip-only
install can stand up the durable tier against *any* Postgres (managed, local,
or the bundled docker-compose one):

    python -m notari.initdb postgresql://user:pass@host:5432/dbname
    # or, with NOTARI_DATABASE_URL already set:
    python -m notari.initdb

Idempotent: the schema uses CREATE ... IF NOT EXISTS plus lightweight ALTER
migrations, so re-running against an existing database is safe. Requires the
`postgres` extra (psycopg) and a server with the pgvector extension available
(e.g. the pgvector/pgvector image).
"""

from __future__ import annotations

import importlib.resources
import os
import re
import sys


def schema_sql() -> str:
    """The packaged Postgres schema (single source of truth: notari/db/schema.sql)."""
    return importlib.resources.files("notari").joinpath("db/schema.sql").read_text()


def _redact(dsn: str) -> str:
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn)


def init_db(dsn: str) -> None:
    """Apply the schema to `dsn`. Raises on connection/DDL errors."""
    import psycopg  # the `postgres` extra; imported lazily

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(schema_sql())


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    dsn = args[0] if args else os.environ.get("NOTARI_DATABASE_URL")
    if not dsn:
        print(
            "usage: python -m notari.initdb [DSN]\n"
            "       (or set NOTARI_DATABASE_URL)",
            file=sys.stderr,
        )
        return 2
    init_db(dsn)
    print(f"notari schema applied to {_redact(dsn)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    raise SystemExit(main())
