from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

MoneyField = Field(gt=0, max_digits=12, decimal_places=2)


class ProductCreate(BaseModel):
    """Shape used by the seed loader; there is no public create endpoint."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    price: Decimal = MoneyField
    inventory: int = Field(ge=0)


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    price: Decimal
    inventory: int
