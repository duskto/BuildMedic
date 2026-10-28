"""Upgrade the database; explicitly adopt an exact-match unversioned legacy schema."""

import argparse

from buildmedic.database import upgrade_database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--adopt-existing",
        action="store_true",
        help="adopt a matching legacy schema; SQLite is backed up first",
    )
    args = parser.parse_args()
    try:
        backup = upgrade_database(args.adopt_existing)
    except RuntimeError as exc:
        parser.exit(1, str(exc) + "\n")
    if backup:
        print("Legacy SQLite backup:", backup)
    print("Database is at migration head.")


if __name__ == "__main__":
    main()
