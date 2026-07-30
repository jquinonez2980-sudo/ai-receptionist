"""Add tenants.stripe_customer_id / stripe_subscription_id.

Phase 3 ticket 3.6 (Stripe customer/subscription linkage — smallest useful
slice). No Stripe API calls anywhere in this app yet; these columns just
store identifiers staff paste in after creating them via the Stripe
Dashboard / scripts/stripe_setup.py. billing_mode ("managed" vs "stripe")
is derived from whether stripe_subscription_id is set — see
platform_api/usage.py's compute_tenant_usage().

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("stripe_customer_id", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("stripe_subscription_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "stripe_subscription_id")
    op.drop_column("tenants", "stripe_customer_id")
