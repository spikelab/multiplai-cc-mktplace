from app.due import due_date
from app.loans import all_loans


def reminder(loan_id):
    title = all_loans()[loan_id]["title"]
    return f"Please return {title} by {due_date(loan_id).isoformat()}"
