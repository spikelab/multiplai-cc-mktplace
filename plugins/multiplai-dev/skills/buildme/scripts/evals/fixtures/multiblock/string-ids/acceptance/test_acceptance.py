from app.customers import customer_of
from app.orders import order_items
from app.report import order_lines
from app.store import Store
from app.users import display_name


def test_display_name():
    assert display_name(Store(), 2) == "Grace"
    assert display_name(Store(), 3) == "unknown user"


def test_order_items():
    assert order_items(Store(), 11) == ["pad"]
    assert order_items(Store(), 12) == []
    assert order_items(Store(), 404) == []


def test_customer_of():
    assert customer_of(Store(), 10) == "Ada"
    assert customer_of(Store(), 11) == "unknown user"
    assert customer_of(Store(), 404) == "unknown user"


def test_order_lines():
    assert order_lines(Store(), [11, 10, 404, 12]) == [
        "#11 unknown user: 1 items",
        "#10 Ada: 2 items",
        "#12 Grace: 0 items",
    ]
