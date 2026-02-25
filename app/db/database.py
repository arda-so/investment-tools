from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine

from app.core.config import CORE_DB_PATH


engine = create_engine(
    f"sqlite:///{CORE_DB_PATH}",
    connect_args={"check_same_thread": False},
)


def get_session() -> Session:
    return Session(engine)


def create_tables() -> None:
    SQLModel.metadata.create_all(engine)

