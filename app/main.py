from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.database import close_pool, open_pool
from app.routers.demo_auth import router as demo_auth_router
from app.routers.orders import router as orders_router
from app.routers.products import router as products_router
from app.routers.webhooks import router as webhooks_router


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

# Mount routers under /api
app.include_router(products_router, prefix="/api")
app.include_router(orders_router, prefix="/api")
app.include_router(webhooks_router, prefix="/api")
app.include_router(demo_auth_router, prefix="/api")

# Mount static directory; html=True lets /static serve index.html automatically.
app.mount("/static", StaticFiles(directory="app/static", html=True), name="static")


# Redirects the bare root URL to the storefront so visitors land on the store.
@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse("/static/index.html")


# Returns a simple JSON response indicating that the API service is up and running.
# Clients and uptime monitors use this endpoint to verify server health without needing authentication.
@app.get("/health")
def health_check():
    return {"status": "ok"}
