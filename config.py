"""Local settings of the video tool.

They live in config.json next to the scripts (or the file in the variable VIDEO_CONFIG). That file is yours and
stays out of the repo: copy config.example.json and fill it in. Without it the tool still records and builds
videos; only `drive` (Drive folder) and the plan sheet need it. Environment variables win over the file:
VIDEO_DRIVE_KEY (drive.key), VIDEO_DRIVE_AS (drive.act_as), VIDEO_DRIVE_FOLDER (drive.videos_folder).
"""

import copy
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "drive": {
        # Service account key (JSON). Never in the repo nor in a spec.
        "key": "~/.config/video-tool/drive-service-account.json",
        # Person the service account acts as (domain-wide delegation), so it can create files in a My Drive.
        "act_as": "",
        # Parent folder of the videos (product folder › video folder): link or id. Empty = no check.
        "videos_folder": "",
    },
    # Optional: a Google Sheet with one row per video (the spec's plan_row). Empty sheet_id = not used.
    "plan": {
        "sheet_id": "",
        "tab": "",
        # Value written in the «type» column of the row after the upload (empty = not written).
        "video_type": "",
        # Header names of the columns the tool reads or writes. The row is found by its «media» column (the video
        # folder); «languages», «channel» and «doc» are only checked before the upload (empty = not checked).
        "columns": {"type": "", "media": "", "post": "", "languages": "", "channel": "", "doc": ""},
        # Accepted values of the «channel» column (YouTube channels). Empty = not checked.
        "channels": [],
        # Language names used in the «languages» column -> ISO 639-1 (codes like "en" are always accepted).
        "language_names": {"english": "en", "spanish": "es", "german": "de", "french": "fr", "italian": "it",
                           "portuguese": "pt", "dutch": "nl", "polish": "pl", "catalan": "ca"},
    },
    # Identity of products and entities for the opening, closing and thumbnail (see examples/).
    "identity": {
        "products_csv": "productos.csv",
        "entities_csv": "entidades.csv",
        "logos_dir": "marcas/logos",
        "font": "fonts/OpenSans[wdth,wght].ttf",
    },
}

ENV = {("drive", "key"): "VIDEO_DRIVE_KEY", ("drive", "act_as"): "VIDEO_DRIVE_AS",
       ("drive", "videos_folder"): "VIDEO_DRIVE_FOLDER"}


def _merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        elif not k.startswith("_"):
            base[k] = v
    return base


def path():
    return os.environ.get("VIDEO_CONFIG") or os.path.join(HERE, "config.json")


def load():
    cfg = copy.deepcopy(DEFAULTS)
    p = path()
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            _merge(cfg, json.load(f))
    for (section, key), var in ENV.items():
        if os.environ.get(var):
            cfg[section][key] = os.environ[var]
    return cfg


def local_path(value):
    """A path of the config: ~ expanded, relative to the folder of the scripts."""
    value = os.path.expanduser(value or "")
    return value if os.path.isabs(value) else os.path.join(HERE, value)


SETTINGS = load()
