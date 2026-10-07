"""Provision a real administrator without passwords in shell history or output."""
import argparse
from getpass import getpass

from sqlalchemy import select

from .auth import hash_password
from .db import init_db, SessionLocal
from .models import User
from .schemas import UserCreateRequest
from .services import audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    password = getpass("New administrator passphrase (15+ characters): ")
    if password != getpass("Confirm passphrase: "):
        raise SystemExit("Passphrases did not match")
    payload = UserCreateRequest(username=args.username, password=password, role="admin")
    init_db()
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.username == payload.username)):
            raise SystemExit("Username exists; use the authenticated account controls")
        account = User(username=payload.username, password_hash=hash_password(password), role="admin")
        db.add(account)
        db.flush()
        audit(db, "administrator_provisioned_locally", account.id)
        db.commit()
    print("Administrator provisioned. No demonstration accounts were enabled.")


if __name__ == "__main__":
    main()
