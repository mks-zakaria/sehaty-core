# sehaty-core

Business-logic library for the Sehaty platform. Pure Python — **no FastAPI,
no HTTP**. Imports [`sehaty-db`](../sehaty-db) (the schema-of-record) and
exposes the controller/service layer a future `sehaty-api` transport can call.

## Architecture

```
Controller (class-as-namespace, @staticmethod)
  - validates inputs / business rules
  - raises the SehatyError taxonomy
  - composes service calls
        │
        ▼
Service (SQLAlchemy IO)
  - the only layer that knows about sessions + queries
  - queries sehaty.db ORM models
```

Each layer only knows about the one below it. This keeps the transport
replaceable, the controllers unit-testable, and the services swappable.

## Layout
```
src/sehaty/core/
  errors.py            # SehatyError taxonomy (http_status + code)
  db/session.py        # lazy Engine + get_session() from DATABASE_URL
  controllers/         # business logic, one module per domain
    doctors.py         # DoctorController.search(...)
  services/            # SQLAlchemy IO, one module per domain
    doctors.py         # search_doctors(...)
  _version.py          # __version__ — semantic-release rewrites this
```

## Error taxonomy

| Error | http_status | code |
|---|---|---|
| `SehatyError` | 500 | `sehaty_error` |
| `SehatyNotFoundError` | 404 | `not_found` |
| `SehatyValidationError` | 400 | `validation_error` |
| `SehatyForbiddenError` | 403 | `forbidden` |
| `SehatyConflictError` | 409 | `conflict` |

## Develop
```bash
uv sync --all-extras       # resolves sehaty-db from ../sehaty-db (editable)
uv run pytest -q           # smoke tests, no live DB
uv run ruff check .
uv run ruff format --check .
```

`sehaty-db` is a **local path dependency** (`[tool.uv.sources]`), so it must
be checked out at `../sehaty-db`. CI checks out `mks-zakaria/sehaty-db`
alongside this repo before `uv sync` (see `.github/workflows/primary.yml`).

## Conventions
Conventional Commits (enforced via pre-commit); versioning + CHANGELOG via
`python-semantic-release` (`release.yml`). One PR = one issue.
