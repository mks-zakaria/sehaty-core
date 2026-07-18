"""Shared base for core domain projections.

Controllers return detached, serialisable projections of the schema — never ORM
objects. Historically these were frozen dataclasses; they are now pydantic models
so the transport layer (``sehaty-api``) can consume them directly as FastAPI
``response_model`` without a parallel ``*Out`` mirror.

``DomainModel`` keeps the semantics the frozen dataclasses had:

* ``frozen=True`` — projections are immutable, hashable value objects.
* ``from_attributes=True`` — a projection can still be built from an ORM row (or
  any attribute-bearing object) via :meth:`model_validate` when a controller
  finds that convenient.

Domain types stay defined *inline in their controller module* (the controller
owns its projection); this base only removes the per-class ``model_config``
boilerplate.
"""

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Immutable, serialisable base for controller return projections."""

    model_config = ConfigDict(frozen=True, from_attributes=True)
