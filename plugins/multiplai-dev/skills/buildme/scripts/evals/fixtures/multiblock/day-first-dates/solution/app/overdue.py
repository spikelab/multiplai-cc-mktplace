from app.due import is_overdue
from app.loans import all_loans


def overdue_ids():
    return sorted(i for i in all_loans() if is_overdue(i))
