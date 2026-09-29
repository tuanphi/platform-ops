"""rename session_remember_days to session_remember_hours

Doi don vi setting "Ghi nho dang nhap" (Settings > General) tu NGAY sang GIO -
rename cot app_setting.session_remember_days -> session_remember_hours va nhan
gia tri cu (don vi ngay) len x24 de GIU NGUYEN hanh vi hieu luc hien tai (vd 7
ngay -> 168 gio). Xem app/config.py Settings.session_remember_hours/min_hours/
max_hours va app/auth.py (_session_expired, RememberMeMiddleware) cho logic
doc/dung gia tri nay.

Revision ID: 7a2b4c9e1d3f
Revises: 50898c7b1f59
Create Date: 2026-08-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a2b4c9e1d3f'
down_revision: Union[str, Sequence[str], None] = '50898c7b1f59'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Rename cot truoc (van giu gia tri cu - don vi ngay) roi moi UPDATE nhan x24 -
    # tranh dung 2 ten cot cung luc tren cung 1 batch (SQLite batch mode rebuild bang).
    with op.batch_alter_table('app_setting') as batch_op:
        batch_op.alter_column(
            'session_remember_days',
            new_column_name='session_remember_hours',
            existing_type=sa.Integer(),
            server_default='168',
        )
    op.execute("UPDATE app_setting SET session_remember_hours = session_remember_hours * 24")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("UPDATE app_setting SET session_remember_hours = session_remember_hours / 24")
    with op.batch_alter_table('app_setting') as batch_op:
        batch_op.alter_column(
            'session_remember_hours',
            new_column_name='session_remember_days',
            existing_type=sa.Integer(),
            server_default='7',
        )
