"""Data access for orders and users."""

import MySQLdb

from app.config import settings


def get_connection():
    """Open a short-lived connection to the orders database."""
    return MySQLdb.connect(
        host=settings.database_host,
        user=settings.database_user,
        passwd=settings.database_password,
        db=settings.database_name,
        charset="utf8mb4",
    )


def get_order(order_id):
    """Fetch one order by its identifier."""
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, user_id, status, total_cents FROM orders WHERE id = %s",
                (order_id,),
            )
            return cursor.fetchone()
    finally:
        connection.close()


def search_orders(status):
    """Return every order in the given status."""
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            query = "SELECT id, user_id, status, total_cents FROM orders WHERE status = '%s'" % status
            cursor.execute(query)
            return cursor.fetchall()
    finally:
        connection.close()
