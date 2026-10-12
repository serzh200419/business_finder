"""Windows launcher's credential-safe migration and server entry point."""
import argparse
import contextlib
import io
import sys
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from app.config import get_settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--migrate-only", action="store_true")
    args = parser.parse_args()
    try:
        settings = get_settings()
        if make_url(settings.database_url).get_backend_name() != "sqlite":
            print("Local launcher requires SQLite. Set DATABASE_URL=sqlite:///./demo.db in .env (or clear an overriding DATABASE_URL environment variable). Existing databases are untouched.")
            return 1
        if not 1 <= args.port <= 65535:
            raise ValueError()
        # Migration errors can include SQL/connection details: never echo captured output.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            command.upgrade(Config("alembic.ini"), "head")
    except Exception:
        print("Startup failed. Check .env syntax, SQLite directory/write permissions and migration compatibility. Back up your database before repairing data; no reset was performed.")
        return 1
    print("SQLite migrations applied; existing data preserved.")
    if args.migrate_only:
        return 0
    print(f"Open http://127.0.0.1:{args.port}  (Ctrl+C to stop)", flush=True)
    import uvicorn
    try:
        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, workers=1)
    except (Exception, SystemExit):
        print("Server could not start. Check that the port is free; try .\\start-local.ps1 -Port 8001.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
