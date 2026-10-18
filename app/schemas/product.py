from uuid import UUID
from pydantic import BaseModel, ConfigDict


class ProductResponse(BaseModel):
    """
    Schema representing the public details of a product in the catalog.
    Prices are strictly integers in paise (e.g. 15000 paise = ₹150.00) to eliminate rounding errors.
    """
    id: UUID
    name: str
    price_paise: int
    stock: int

    model_config = ConfigDict(from_attributes=True)
