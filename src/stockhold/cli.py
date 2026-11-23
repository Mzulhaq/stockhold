"""Local StockHold commands."""

import argparse
import json
import os
import sys

import uvicorn
from sqlalchemy.exc import SQLAlchemyError

from stockhold.api import create_app
from stockhold.store import Database


def port(value):
    parsed = int(value)
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("Port must be 1-65535")
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve, recover and audit local inventory")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the local API")
    expire = commands.add_parser("expire", help="Release a bounded batch of expired holds")
    expire.add_argument("--limit", type=int, default=100)
    audit = commands.add_parser("audit", help="Check inventory and transition-history invariants")
    for command in (serve, expire, audit):
        command.add_argument(
            "--database-url", default=os.getenv("STOCKHOLD_DATABASE_URL", "sqlite:///stockhold.db")
        )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=port, default=8000)
    args = parser.parse_args(argv)
    database = None
    try:
        database = Database(args.database_url)
        if args.command == "expire":
            print(json.dumps(database.expire(args.limit)))
        elif args.command == "audit":
            result = database.audit()
            print(json.dumps(result))
            return 0 if result["ok"] else 1
        else:
            uvicorn.run(create_app(database), host=args.host, port=args.port, access_log=False)
        return 0
    except (ValueError, SQLAlchemyError, OSError):
        print(
            json.dumps(
                {"error": "Unable to initialize or run StockHold; check local configuration"}
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
