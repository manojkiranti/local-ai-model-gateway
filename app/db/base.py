"""Declarative base for all ORM models.

Models import `Base` from here; Alembic imports it (plus the model modules) so
its metadata knows every table for autogeneration.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
