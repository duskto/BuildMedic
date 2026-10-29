"""Local database operator: prepare blinded requests or import signed, complete reviews."""

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

from scorer_service.diagnosis_review import external_path, read_private, write_private

from . import db, diagnosis_scoring
from .database import require_schema
from .errors import AppError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["prepare", "import"])
    parser.add_argument("--experiment", required=True)
    parser.add_argument(
        "--file", type=Path, required=True, help="external request output or signed receipt input"
    )
    args = parser.parse_args()
    external_path(args.file)
    require_schema()
    with db.engine.connect() as conn:
        owner = conn.execute(
            select(db.experiments.c.owner_id).where(
                db.experiments.c.id == args.experiment,
            )
        ).scalar_one()
    if args.operation == "prepare":
        write_private(args.file, diagnosis_scoring.prepare(args.experiment, owner))
        print("Blinded request prepared; no model/runner calls or accuracy claims.")
    else:
        result = diagnosis_scoring.ingest(args.experiment, owner, read_private(args.file))
        print(f"Diagnosis review {result['id']}: {result['status']}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, AppError):
        print(
            "Diagnosis operation rejected; check schema, frozen report, configuration and signed receipt.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
