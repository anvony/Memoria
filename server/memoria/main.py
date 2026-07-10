"""FastAPI application. Local-only by design: uvicorn binds 127.0.0.1 and
CORS admits only the origins our own frontend can run on (Vite dev/preview
and the Tauri WebView) — a random website open in your browser cannot read
your photo library.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__, config, db
from .api import router

ALLOWED_ORIGINS = [
    "http://localhost:5173",   # vite dev
    "http://localhost:4173",   # vite preview
    "http://tauri.localhost",  # tauri (windows)
    "tauri://localhost",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.get_data_dir() is not None:
        db.init_db()
    yield


app = FastAPI(title="Memoria", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
