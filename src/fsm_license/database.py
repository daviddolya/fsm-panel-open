"""PostgreSQL persistence. Entitlement row locks serialize all seat mutations."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Entitlement(Base):
    __tablename__ = "entitlements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(200), unique=True)
    external_customer_id: Mapped[str] = mapped_column(String(200))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    device_limit: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(Integer)
    revision_digest: Mapped[str] = mapped_column(String(64))
    activation_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Activation(Base):
    __tablename__ = "activations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    entitlement_id: Mapped[str] = mapped_column(ForeignKey("entitlements.id"), index=True)
    installation_id: Mapped[str] = mapped_column(String(36))
    device_public_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(String(40))

    __table_args__ = (Index("ix_activation_live", "entitlement_id", "released_at"),)


class ProofNonce(Base):
    __tablename__ = "proof_nonces"

    device_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(100), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Database:
    def __init__(self, url: str | None = None, *, engine: Engine | None = None) -> None:
        if engine is None:
            if url is None:
                raise ValueError("database URL or engine is required")
            engine = create_engine(url, pool_pre_ping=True)
        self.engine = engine
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def dispose(self) -> None:
        self.engine.dispose()
