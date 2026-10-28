"""The product catalogue, read from catalog.csv."""

import csv
from pathlib import Path

_CSV = Path(__file__).with_name("catalog.csv")


def skus() -> list[str]:
    with _CSV.open() as fh:
        return [row["sku"] for row in csv.DictReader(fh)]


def price_of(sku: str) -> str:
    with _CSV.open() as fh:
        for row in csv.DictReader(fh):
            if row["sku"] == sku:
                return row["price"]
    raise KeyError(sku)
