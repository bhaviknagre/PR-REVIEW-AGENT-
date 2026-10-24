from alembic import context
from sqlalchemy import create_engine

from pr_review.core.settings import get_settings
from pr_review.storage.models import Base

target_metadata = Base.metadata


def run() -> None:
    # A caller (tests, programmatic upgrade) may hand in a ready connection.
    connection = context.config.attributes.get("connection")
    if connection is None:
        url = get_settings().database_url.get_secret_value()
        connection = create_engine(url).connect()
    with connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()


run()
