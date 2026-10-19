# Change-aware CI

Templates: [`change-aware-ci.example.yml`](../ci-templates/change-aware-ci.example.yml) and
[`db-suites.example.yml`](../ci-templates/db-suites.example.yml). They are examples for a
service with `requirements*.txt` and a `Dockerfile`; this repo's own CI (`ci.yml`) does not use Docker.

## The problem

Every pull request rebuilt a large application image, spending most of the job on the build to run
a short test suite. Most pushes touched only application code.

## The pattern

1. A `changes` job decides, using `dorny/paths-filter`, whether `requirements*.txt` or `Dockerfile`
   changed. It also forces the full path on the nightly schedule and for the label `ci:no-cache`.
2. **Dependencies unchanged:** install them with `uv` into a directory outside the checkout
   (`/opt/ci-deps`), cached on the hash of the requirements files, and run pytest in a slim Python
   container with `--network none`.
3. **Dependencies changed, nightly, or labelled:** build the real image and run the tests inside it,
   also with `--network none`.

The `--network none` sandbox is what the image really provided, so it is kept on both paths. The
nightly run catches drift between the cached environment and the Dockerfile within a day.

Two fast-path fixes, both worth knowing:

- **Byte-compile at install** (`uv pip install --compile-bytecode`). With read-only dependencies,
  every parallel test worker recompiled the tree on import (568 s versus 83 s).
- **Install outside the checkout.** Inside it, pytest collected 273 tests from the dependencies'
  own test suites.

Every test step writes a JUnit report and then runs `scripts/check_junit.py`, which fails on a
missing, empty or all-skipped report, so a job cannot pass by not running anything.

## DB-backed suites

`db-suites.example.yml` runs suites that need Postgres. The suites are found by `grep`, so none can
be left out. `--strict-skips` fails the run on any skipped test, and the JUnit gate runs with
`--max-skips 0`. A separate job runs `scripts/check_migrations.py` first. It reads the migration
files with `ast` and needs no dependencies, so duplicate ids, missing parents, cycles and multiple
heads fail the pull request, and not the container at boot.

## Result

The job got several times faster with no test outcome changed, compared test by test with a JUnit diff.

## Experiment that was reverted

A slim, pre-built cached test image kept the same test coverage but was rejected on measurement:
warm builds were no faster than the old build, because moving the layer cache cost about as much as
rebuilding the layers, and it added a second image to maintain. It was reverted in favour of the
cached dependency directory.
