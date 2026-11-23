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
    parser = argparse.ArgumentParser(description="Serve the local StockHold API")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the local API")
    serve.add_argument(
        "--database-url", default=os.getenv("STOCKHOLD_DATABASE_URL", "sqlite:///stockhold.db")
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=port, default=8000)
    args = parser.parse_args(argv)
    database = None
    try:
        database = Database(args.database_url)
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
