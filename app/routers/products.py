from fastapi import APIRouter
from app.core.database import transaction
from app.repositories.product_repo import list_products
from app.schemas.product import ProductResponse

router = APIRouter(prefix="/products", tags=["products"])


# Handles the GET /api/products HTTP request and returns the list of available products.
# Coordinates the database connection via transaction context and repository query without writing SQL in the router.
@router.get("", response_model=list[ProductResponse])
def get_products():
    with transaction() as conn:
        return list_products(conn)
