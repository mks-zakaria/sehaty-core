"""Medication catalogue search (shared read).

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). Backs the
doctor's prescribe autocomplete: search the ``medications`` catalogue by INN or
brand name and return small detached projections. Read-only; no writes.
"""

from sehaty.db import Medication
from sqlalchemy import select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session


class MedicationRow(DomainModel):
    """A catalogue medication for the prescribe/dispense pickers."""

    id: int
    name: str
    brand: str | None
    form: str
    strength: str | None


class MedicationController:
    @staticmethod
    def search(query: str, limit: int = 20) -> list[MedicationRow]:
        """Search the catalogue by INN / brand name (empty query → no rows)."""
        q = (query or "").strip()
        if not q:
            return []
        like = f"%{q}%"
        with get_session() as session:
            rows = session.execute(
                select(
                    Medication.id,
                    Medication.inn_name,
                    Medication.brand_name,
                    Medication.form,
                    Medication.strength,
                )
                .where(Medication.inn_name.ilike(like) | Medication.brand_name.ilike(like))
                .order_by(Medication.inn_name)
                .limit(limit)
            ).all()
        return [
            MedicationRow(
                id=r.id, name=r.inn_name, brand=r.brand_name, form=r.form, strength=r.strength
            )
            for r in rows
        ]
