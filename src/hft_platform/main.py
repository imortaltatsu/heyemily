from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from hft_platform.config import get_settings
from hft_platform.database import engine, init_db
from hft_platform.routers import auth, bots, internal


@asynccontextmanager
async def lifespan(_: FastAPI):
    os.makedirs("data", exist_ok=True)
    await init_db()
    yield
    await engine.dispose()


app = FastAPI(title="Hyperbot HFT Platform", lifespan=lifespan)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
app.include_router(bots.router, prefix="/api")
app.include_router(internal.router, prefix="/api")


@app.get("/api/health")
async def health() -> dict[str, bool]:
    return {"ok": True}
