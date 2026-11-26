"""Local settings of the video tool.

They live in config.json next to the scripts, or in the file named by the variable VIDEO_CONFIG — so one copy of
the tool can serve several workspaces, each with its own config. That file is yours and stays out of the repo:
copy config.example.json and fill it in. Without it the tool still records and builds videos; only `drive`
(Drive folder) and the plan sheet need it. A relative path in the file is resolved against the folder of the
config file; when nothing exists there, against the folder of the scripts (so the bundled fonts and examples
keep working). Environment variables win over the file:
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
        # Call to action that `drive` adds to the YouTube description, per recording language ({store} = the
        # product's store link, {entity} and {web} = the entity that makes it). A description line with the store
        # link is replaced by it. Empty = no call to action.
        # No addresses in the description (links go in the video's comments): {product} = product name,
        # {store_name} = store name (store_names, by the host of the store link), {entity} = entity name.
        # {store} and {web} (the addresses) are still available for other uses.
        "cta": {
            "en": "Search for «{product}» in the {store_name}.\nFor help or other services, contact {entity} through its website.",
            "es": "Busca «{product}» en {store_name}.\nSi necesitas ayuda u otros servicios, contacta con {entity} desde su web.",
            "de": "Suchen Sie «{product}» im {store_name}.\nFür Hilfe oder weitere Leistungen erreichen Sie {entity} über die Website.",
        },
        "store_names": {"apps.odoo.com": "Odoo Apps Store"},
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


CONFIG_DIR = os.path.dirname(os.path.abspath(path())) if os.path.isfile(path()) else HERE


def local_path(value):
    """A path of the config: ~ expanded. A relative path is resolved against the folder of the config file;
    when nothing exists there, against the folder of the scripts (bundled fonts and examples)."""
    value = os.path.expanduser(value or "")
    if os.path.isabs(value):
        return value
    in_config = os.path.join(CONFIG_DIR, value)
    return in_config if os.path.exists(in_config) else os.path.join(HERE, value)


SETTINGS = load()
