from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.core.database import close_pool, open_pool
from app.routers.products import router as products_router


# Manages startup and shutdown lifecycle events for the application.
# Opens the database connection pool on startup and gracefully closes it upon application shutdown.
@asynccontextmanager
async def lifespan(app: FastAPI):
    open_pool()
    yield
    close_pool()


app = FastAPI(
    title="CartAPI",
    description="E-commerce orders and payments backend with Razorpay",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount products router under /api
app.include_router(products_router, prefix="/api")


# Returns a simple JSON response indicating that the API service is up and running.
# Clients and uptime monitors use this endpoint to verify server health without needing authentication.
@app.get("/health")
def health_check():
    return {"status": "ok"}
