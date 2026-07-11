"""Pydantic response models — the Python mirror of app/src/lib/types.ts.

This is the contract the frontend was built against (on mocks) since day one.
`alias_generator=to_camel` makes FastAPI emit camelCase JSON while the Python
code stays snake_case — each language keeps its native convention.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Photo(ApiModel):
    id: str
    filename: str
    thumb_url: str
    preview_url: str
    date_taken: str
    width: int
    height: int
    is_favorite: bool
    face_count: int
    place: str | None
    lat: float | None
    lng: float | None
    camera: str | None
    person_ids: list[str]
    # Video additions (photos have kind="photo", duration=None)
    kind: str = "photo"
    duration: float | None = None
    size_bytes: int | None = None   # largest on-disk copy, for the info panel
    has_live: bool = False          # a Live Photo: a companion MOV is paired to it


class Person(ApiModel):
    id: str
    name: str | None
    avatar_url: str
    photo_count: int


class Trip(ApiModel):
    id: str
    place: str                    # location label (auto or user-set)
    start_date: str
    end_date: str
    photo_count: int
    cover_urls: list[str]
    name: str | None = None       # custom title; falls back to place when unset
    cover_hash: str | None = None # user-chosen cover; when set the card shows it full-bleed


class Place(ApiModel):
    id: str
    name: str
    lat: float
    lng: float
    photo_count: int
    cover_url: str


class Album(ApiModel):
    id: str
    name: str
    created_at: str
    photo_ids: list[str]
    cover_url: str | None


class DuplicateFile(ApiModel):
    file_id: str
    path: str
    drive: str
    size_bytes: int
    width: int
    height: int
    modified_at: str
    thumb_url: str
    preview_url: str


class DuplicateGroup(ApiModel):
    id: str
    similarity: int
    date_taken: str
    files: list[DuplicateFile]


# ---- Backend-only shapes (settings / status screens) ------------------------


class SourceFolder(ApiModel):
    id: int
    path: str
    drive_label: str | None
    online: bool
    file_count: int
    failed_count: int = 0   # files in this folder the indexer couldn't process


class IndexStatus(ApiModel):
    state: str            # idle | scanning | indexing | faces | clip | done | error
    total: int
    done: int
    current: str | None   # file currently being processed
    error: str | None
    ml_available: bool    # are the faces/CLIP deps installed?
    failed_count: int = 0  # files that couldn't be indexed (see /index/failures)


class IndexFailure(ApiModel):
    path: str
    filename: str
    folder_id: int | None
    error: str
    failed_at: str


class SetupState(ApiModel):
    configured: bool
    data_dir: str | None
    default_data_dir: str


class StorageInfo(ApiModel):
    data_dir: str            # holds the database (the irreplaceable part)
    db_bytes: int
    cache_dir: str           # thumbnail cache — always safe to delete
    cache_bytes: int
    models_dir: str          # downloaded ML models
    models_bytes: int
    free_bytes: int          # free space left on the data drive


class MlStatus(ApiModel):
    installed: bool          # are the insightface/open_clip packages present?
    models_present: bool     # have the model weights been downloaded?
    models_bytes: int
    models_dir: str
    downloading: bool
    progress: float          # 0..1 while downloading, else 0
    message: str | None


class PhotoFace(ApiModel):
    face_id: int
    person_id: str | None    # None once detached ("not this person")
    name: str | None
    avatar_url: str | None   # the person's key photo, if they're a known cluster
    crop_url: str            # this exact face cropped from this image
    x: float
    y: float
    w: float
    h: float
