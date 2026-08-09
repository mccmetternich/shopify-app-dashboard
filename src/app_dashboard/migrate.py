"""Entrypoint: apply pending migrations. Run as `python -m app_dashboard.migrate`."""

from app_dashboard.db import connect, run_migrations


def main() -> None:
    run_migrations(connect())


if __name__ == "__main__":
    main()
