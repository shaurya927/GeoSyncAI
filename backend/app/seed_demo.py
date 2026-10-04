"""Explicit local/demo bootstrap command.

Run with ``python -m app.seed_demo`` from the backend directory. Production
deployments should leave AUTO_BOOTSTRAP disabled and create real accounts
through an administrative provisioning process.
"""
from .db import get_db, init_db
from .main import bootstrap


def main() -> None:
    init_db()
    db = next(get_db())
    try:
        bootstrap(db)
    finally:
        db.close()
    print("Seeded the explicit GeoSyncAI demonstration accounts and project.")


if __name__ == "__main__":
    main()
