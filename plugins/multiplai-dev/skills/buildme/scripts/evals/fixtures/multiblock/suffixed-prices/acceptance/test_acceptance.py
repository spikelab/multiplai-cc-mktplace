from app.basket import basket_total_cents
from app.lines import line_total_cents
from app.price_list import price_list
from app.pricing import unit_price_cents


def test_unit_price():
    assert unit_price_cents("pad") == 200
    assert unit_price_cents("ink") == 310


def test_line_total():
    assert line_total_cents("pad", 3) == 600


def test_basket_total():
    assert basket_total_cents({"ink": 1, "pad": 2, "pen": 4}) == 1210
    assert basket_total_cents({}) == 0


def test_price_list():
    assert price_list() == ["ink: 3.10", "pad: 2.00", "pen: 1.25"]
