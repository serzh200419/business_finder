"""Run after alembic upgrade head. All demo data and failures are synthetic."""
from app.cli import fixture_demo

if __name__ == "__main__":
    fixture_demo()
