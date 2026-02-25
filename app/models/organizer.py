from __future__ import annotations

from typing import Optional

from sqlmodel import Field, SQLModel


class Todo(SQLModel, table=True):
    __tablename__ = "todos"

    id: Optional[int] = Field(default=None, primary_key=True)
    task: str
    status: str = "open"
    created_at: str
    priority: str = "P2"
    due_date: str = ""
    ticker: str = ""
    category: str = "general"


class DailyNote(SQLModel, table=True):
    __tablename__ = "daily_notes"

    day: str = Field(primary_key=True)
    content: str = ""
    updated_at: str


class DailyNoteTag(SQLModel, table=True):
    __tablename__ = "daily_note_tags"

    id: Optional[int] = Field(default=None, primary_key=True)
    day: str
    ticker: str
    created_at: str


class InvestorNote(SQLModel, table=True):
    __tablename__ = "investor_notes"

    id: Optional[int] = Field(default=None, primary_key=True)
    scope: str
    ticker: Optional[str] = None
    sentiment: str = "neutral"
    note: str
    tags: Optional[str] = None
    created_at: Optional[str] = None


class WorkspaceJournal(SQLModel, table=True):
    __tablename__ = "workspace_journal"

    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: str
    action: str
    emotion: str
    note: str
    created_at: str

