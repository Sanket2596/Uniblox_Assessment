from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import Base, SessionLocal, engine
from app.errors import register_error_handlers
from app.routers import cart, products
from app.seed import seed_products


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed_products(db)
    yield


app = FastAPI(title="Checkout & Rewards Service", version="0.1.0", lifespan=lifespan)
register_error_handlers(app)
app.include_router(products.router)
app.include_router(cart.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
