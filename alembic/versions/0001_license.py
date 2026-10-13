"""Initial entitlements, bound device activations, and persistent proof replay protection."""

import sqlalchemy as sa

from alembic import op

revision = "0001_license"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entitlements",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("external_id", sa.String(200), nullable=False, unique=True),
        sa.Column("external_customer_id", sa.String(200), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device_limit", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("revision_digest", sa.String(64), nullable=False),
        sa.Column("activation_key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "activations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "entitlement_id", sa.String(36), sa.ForeignKey("entitlements.id"), nullable=False
        ),
        sa.Column("installation_id", sa.String(36), nullable=False),
        sa.Column("device_public_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("release_reason", sa.String(40)),
    )
    op.create_index("ix_activations_entitlement_id", "activations", ["entitlement_id"])
    op.create_index("ix_activation_live", "activations", ["entitlement_id", "released_at"])
    op.create_table(
        "proof_nonces",
        sa.Column("device_fingerprint", sa.String(64), primary_key=True),
        sa.Column("nonce", sa.String(100), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_proof_nonces_expires_at", "proof_nonces", ["expires_at"])


def downgrade() -> None:
    # Operators must back up before downgrade: removing these tables erases entitlements.
    op.drop_table("proof_nonces")
    op.drop_table("activations")
    op.drop_table("entitlements")
