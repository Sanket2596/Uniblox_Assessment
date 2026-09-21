import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Product
from app.schemas.product import ProductCreate


def load_seed_products(path: Path | None = None) -> list[ProductCreate]:
    raw = json.loads((path or settings.seed_file).read_text(encoding="utf-8"))
    return [ProductCreate.model_validate(item) for item in raw["products"]]


def seed_products(db: Session, path: Path | None = None) -> int:
    """Insert seed products that are not already present (matched by name). Idempotent."""
    existing = set(db.scalars(select(Product.name)).all())
    inserted = 0
    for item in load_seed_products(path):
        if item.name in existing:
            continue
        db.add(Product(**item.model_dump()))
        inserted += 1
    db.commit()
    return inserted


if __name__ == "__main__":
    from app.db import Base, SessionLocal, engine

    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        count = seed_products(session)
    print(f"Seeded {count} new product(s).")
