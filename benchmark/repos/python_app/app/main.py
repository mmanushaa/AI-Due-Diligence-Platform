"""FastAPI application for the orders fixture."""

from fastapi import FastAPI

from app.routes import orders, search

app = FastAPI(title="Orders Fixture", version="0.1.0")
app.include_router(search.router)
app.include_router(orders.router)
