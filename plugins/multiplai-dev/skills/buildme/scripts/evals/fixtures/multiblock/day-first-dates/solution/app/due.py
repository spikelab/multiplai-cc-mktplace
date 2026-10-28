from datetime import datetime

from app.clock import today
from app.loans import all_loans


def due_date(loan_id):
    return datetime.strptime(all_loans()[loan_id]["due"], "%d/%m/%Y").date()


def is_overdue(loan_id):
    return today() > due_date(loan_id)


def days_left(loan_id):
    return (due_date(loan_id) - today()).days
