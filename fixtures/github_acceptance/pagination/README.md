# BuildMedic remote acceptance fixture

This branch deliberately contains a small pagination defect. Its purpose is to
produce a real failed GitHub Actions Job for BuildMedic ingestion and independent
repair verification. The initial `pytest` Job is expected to fail two tests.

This is a familiar public development fixture, not a held-out evaluation case.
The complete standalone repository is kept on an `acceptance/github-pagination-*`
branch. The main BuildMedic application and operator configuration are separate.

The workflow pins Python 3.12.16 and all five test dependencies to the versions
in the registered local Docker image. Only `src/demo/pagination.py` may be repaired;
tests, dependencies and this workflow stay unchanged during verification.

The real failed Run URL, Run attempt, Job ID and full commit SHA must be obtained
from GitHub. Register that exact SHA, workflow `.github/workflows/acceptance.yml`,
Job `pytest`, target `tests/test_pagination.py` and failure fingerprint `test_pages`.
Local validation does not itself prove that a later GitHub Run passed.
