"""MedicationController catalogue-search tests (in-memory SQLite)."""

import pytest
from sehaty.db import Medication
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.medications import MedicationController
from sehaty.core.db import session as session_mod


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SehatyBase.metadata.create_all(engine, tables=[Medication.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    yield factory
    session_mod.set_session_factory(None)


def test_search_by_inn_and_brand(db):
    with db() as s:
        s.add_all(
            [
                Medication(
                    inn_name="Amoxicillin", brand_name="Clamoxyl", form="tablet", strength="500mg"
                ),
                Medication(
                    inn_name="Paracetamol", brand_name="Doliprane", form="tablet", strength="1g"
                ),
            ]
        )
        s.commit()

    by_inn = MedicationController.search("amox")
    assert len(by_inn) == 1
    assert by_inn[0].name == "Amoxicillin" and by_inn[0].strength == "500mg"

    by_brand = MedicationController.search("doli")
    assert len(by_brand) == 1 and by_brand[0].brand == "Doliprane"

    assert MedicationController.search("   ") == []
