"""FastAPI application. Local-only by design: uvicorn binds 127.0.0.1 and
CORS admits only the origins our own frontend can run on (Vite dev/preview
and the Tauri WebView) — a random website open in your browser cannot read
your photo library.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, config, db
from .api import router

ALLOWED_ORIGINS = [
    "http://localhost:5173",   # vite dev
    "http://localhost:4173",   # vite preview
    "http://tauri.localhost",  # tauri (windows)
    "tauri://localhost",
]

# The server binds 127.0.0.1, but CORS alone can't stop DNS rebinding: a
# malicious site that rebinds its hostname to 127.0.0.1:8123 makes its requests
# same-origin, so the browser skips CORS/preflight and could then read the whole
# library. Pinning the accepted Host header to the loopback names closes that:
# a rebinding request still carries the attacker's hostname in Host and is
# rejected with 400. TrustedHostMiddleware strips the port before matching, so
# only the bare hostnames are listed here.
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]


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
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.include_router(router)
