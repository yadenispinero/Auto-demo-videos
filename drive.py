"""Copy the final files of a video to its folder in Google Drive, where a YouTube upload (an n8n workflow, an
Apps Script...) can pick them up. Used by `video.py drive`; only needs `requests` and `pyjwt` (with `cryptography`).

Settings: config.json (see config.py and config.example.json).
Access: a Google service account key (JSON), never in a repo nor in a spec. Where it is looked for, in order:
`--key`, the variable VIDEO_DRIVE_KEY, drive.key of config.json (default ~/.config/video-tool/drive-service-account.json).
The account must be editor of the videos folder (drive.videos_folder). A service account cannot own files in a
personal My Drive (Google answers storageQuotaExceeded when it creates one): with domain-wide delegation it acts as a
person (`--as`, VIDEO_DRIVE_AS or drive.act_as) and the files are theirs. Replacing a file that already exists
(a new version, same id and link) works without delegation.

Optional plan sheet (plan.* in config.json): a Google Sheet with one row per video. After the upload the row gets
plan.video_type in the «type» column and the spec's youtube_description in the «post» column.
"""

import hashlib
import json
import os
import re
import time
import unicodedata

from config import SETTINGS, local_path

DEFAULT_KEY = local_path(SETTINGS["drive"]["key"])
API = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
CHUNK = 8 * 1024 * 1024
TYPES = {".mp4": "video/mp4", ".srt": "application/x-subrip", ".json": "application/json", ".jpg": "image/jpeg"}
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
PLAN = SETTINGS["plan"]
PLAN_SHEET, PLAN_TAB, VIDEO_TYPE, COLUMNS = PLAN["sheet_id"], PLAN["tab"], PLAN["video_type"], PLAN["columns"]


class DriveError(Exception):
    pass


def folder_id(value):
    """Id from a folder link (…/folders/<id>), a file link (…/d/<id>, ?id=<id>) or the bare id."""
    value = (value or "").strip()
    m = re.search(r"/folders/([\w-]{10,})", value) or re.search(r"/d/([\w-]{10,})", value) \
        or re.search(r"[?&]id=([\w-]{10,})", value)
    if m:
        return m.group(1)
    return value if re.fullmatch(r"[\w-]{10,}", value) else None


# Parent folder of the videos (product folder › video folder): the upload only sees what is inside. None = no check.
VIDEOS_FOLDER = folder_id(SETTINGS["drive"]["videos_folder"])


def key_path(key=None):
    return key or os.environ.get("VIDEO_DRIVE_KEY") or DEFAULT_KEY


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Drive:
    def __init__(self, key=None, subject=None):
        import requests

        self.requests = requests
        self.path = key_path(key)
        self.subject = subject if subject is not None else SETTINGS["drive"]["act_as"] or None
        if not os.path.isfile(self.path):
            raise DriveError(f"no service account key at {self.path} (put it there, or pass --key / VIDEO_DRIVE_KEY; "
                             "see README.md > Drive)")
        try:
            with open(self.path) as f:
                info = json.load(f)
            self.email, self.private_key = info["client_email"], info["private_key"]
            self.token_uri = info.get("token_uri", "https://oauth2.googleapis.com/token")
        except (ValueError, KeyError) as e:
            raise DriveError(f"{self.path} is not a service account key (JSON with client_email and private_key): {e}")
        self.token, self.expires = None, 0

    def _auth(self):
        if self.token and time.time() < self.expires - 60:
            return self.token
        import jwt

        now = int(time.time())
        claims = {"iss": self.email, "scope": "https://www.googleapis.com/auth/drive", "aud": self.token_uri,
                  "iat": now, "exp": now + 3600}
        if self.subject:
            claims["sub"] = self.subject
        r = self.requests.post(self.token_uri, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": jwt.encode(claims, self.private_key, algorithm="RS256")}, timeout=60)
        if r.status_code != 200:
            hint = (f" (acting as {self.subject}: the domain-wide delegation of {self.email} needs the scope "
                    "https://www.googleapis.com/auth/drive in admin.google.com)") if self.subject else ""
            raise DriveError(f"Google refused the key of {self.email}: {r.text[:300]}{hint}")
        data = r.json()
        self.token, self.expires = data["access_token"], now + int(data.get("expires_in", 3600))
        return self.token

    def call(self, method, url, ok=(200,), **kw):
        headers = kw.pop("headers", {})
        headers["Authorization"] = "Bearer " + self._auth()
        r = self.requests.request(method, url, headers=headers, timeout=kw.pop("timeout", 120), **kw)
        if r.status_code not in ok:
            raise DriveError(self.explain(r))
        return r

    def explain(self, r):
        try:
            err = r.json().get("error", {})
            reason = (err.get("errors") or [{}])[0].get("reason", "")
            message = err.get("message", r.text[:300])
        except ValueError:
            reason, message = "", r.text[:300]
        if reason == "storageQuotaExceeded" or "storage quota" in message.lower():
            return (f"Drive: {message} — a service account cannot create files in a personal My Drive. Use "
                    "domain-wide delegation (--as <person> / drive.act_as in config.json) or a shared drive "
                    "(README.md > Drive).")
        if r.status_code == 404:
            return (f"Drive: not found or not shared with {self.email} ({message}). Share the videos folder "
                    "with that account as editor.")
        return f"Drive {r.status_code} {reason}: {message}"

    # ---------------------------------------------------------------- reading

    def get(self, file_id, fields="id,name,mimeType,parents,trashed,md5Checksum,size,webViewLink"):
        return self.call("GET", f"{API}/{file_id}", params={"fields": fields, "supportsAllDrives": "true"}).json()

    def children(self, parent):
        files, token = [], None
        while True:
            params = {"q": f"'{parent}' in parents and trashed = false", "pageSize": 1000,
                      "fields": "nextPageToken,files(id,name,mimeType,md5Checksum,size,webViewLink)",
                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            data = self.call("GET", API, params=params).json()
            files += data.get("files", [])
            token = data.get("nextPageToken")
            if not token:
                return files

    def path_to(self, file_id, stop=VIDEOS_FOLDER, depth=8):
        """Names from `stop` down to the file, or None if the file is not inside `stop` (no `stop`: up to the top)."""
        names, current = [], file_id
        for _ in range(depth):
            meta = self.get(current, "id,name,parents")
            names.insert(0, meta["name"])
            if current == stop:
                return names
            parents = meta.get("parents") or []
            if not parents:
                return names if not stop else None
            current = parents[0]
        return None

    # ---------------------------------------------------------------- writing

    def upload(self, path, parent=None, existing=None):
        """Resumable upload: a new file in `parent`, or a new version of `existing` (same id and link)."""
        size = os.path.getsize(path)
        mime = TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        params = {"uploadType": "resumable", "supportsAllDrives": "true",
                  "fields": "id,name,md5Checksum,size,webViewLink"}
        headers = {"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size),
                   "Content-Type": "application/json; charset=UTF-8"}
        if existing:
            start = self.call("PATCH", f"{UPLOAD}/{existing}", params=params, headers=headers, data="{}")
        else:
            body = json.dumps({"name": os.path.basename(path), "parents": [parent]})
            start = self.call("POST", UPLOAD, params=params, headers=headers, data=body.encode())
        session = start.headers["Location"]
        with open(path, "rb") as f:
            offset = 0
            while True:
                chunk = f.read(CHUNK)
                end = offset + len(chunk) - 1
                rng = f"bytes {offset}-{end}/{size}" if chunk else f"bytes */{size}"
                r = self.call("PUT", session, ok=(200, 201, 308), data=chunk,
                              headers={"Content-Length": str(len(chunk)), "Content-Range": rng}, timeout=600)
                if r.status_code in (200, 201):
                    return r.json()
                offset = end + 1

    def make_folder(self, name, parent):
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}
        return self.call("POST", API, params={"supportsAllDrives": "true", "fields": "id,name,webViewLink"},
                         json=body).json()

    def move(self, file_id, src, dst):
        return self.call("PATCH", f"{API}/{file_id}", json={},
                         params={"addParents": dst, "removeParents": src, "supportsAllDrives": "true",
                                 "fields": "id,name"}).json()


def column(n):
    """A1 letter of the 0-based column n."""
    name = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        name = chr(65 + r) + name
    return name


def _get(drive, rng):
    return drive.call("GET", f"{SHEETS}/{PLAN_SHEET}/values/{rng}").json().get("values") or []


def norm(text):
    """Lower case, no accents, single spaces: to compare names typed by people."""
    text = unicodedata.normalize("NFD", str(text or "")).encode("ascii", "ignore").decode()
    return " ".join(text.lower().split())


def language_code(name):
    """ISO 639-1 code of a language written in the sheet (a code, or a name of plan.language_names)."""
    names = {norm(k): v for k, v in PLAN.get("language_names", {}).items()}
    key = norm(name)
    return names.get(key) or (key if re.fullmatch(r"[a-z]{2}", key) else None)


def find_row(drive, folder, hint=None):
    """The row of the plan sheet whose media column is `folder`: {row, values, head, warnings}.
    Rows move when the sheet is sorted, so the spec's plan_row is only a hint."""
    media = COLUMNS["media"]
    if not (PLAN_SHEET and PLAN_TAB and media):
        raise DriveError("no plan sheet in config.json (plan.sheet_id, plan.tab and plan.columns.media)")
    head = [h.strip() for h in (_get(drive, f"{PLAN_TAB}!1:1") or [[]])[0]]
    if media not in head:
        raise DriveError(f"{PLAN_TAB} has no column «{media}»")
    col = column(head.index(media))
    cells = _get(drive, f"{PLAN_TAB}!{col}:{col}")
    rows = [i + 1 for i, c in enumerate(cells) if i and c and folder_id(c[0]) == folder]
    if not rows:
        raise DriveError(f"no row of {PLAN_TAB} has this folder in «{media}»: put its link there first")
    if len(rows) > 1:
        raise DriveError(f"rows {', '.join(map(str, rows))} of {PLAN_TAB} have this folder in «{media}»: leave it in one")
    row = rows[0]
    warnings = [f"the video is in row {row} of {PLAN_TAB}, not {hint} (rows move when the sheet is sorted; "
                f"plan_row of the spec is only a hint)"] if hint and hint != row else []
    vals = (_get(drive, f"{PLAN_TAB}!{row}:{row}") or [[]])[0]
    values = {h: (vals[i] if i < len(vals) else "") for i, h in enumerate(head) if h}
    return {"row": row, "values": values, "head": head, "warnings": warnings}


def check_row(found, lang, subtitles):
    """Is the row ready for the YouTube upload? (errors, warnings) about languages, channel and document link."""
    values, errors, warnings = found["values"], [], []
    langs, channel, doc = COLUMNS.get("languages"), COLUMNS.get("channel"), COLUMNS.get("doc")
    if langs and langs in values:
        written = [x.strip() for x in values[langs].split(",") if x.strip()]
        codes = [language_code(x) for x in written]
        unknown = [w for w, c in zip(written, codes) if not c]
        if unknown:
            errors.append(f"«{langs}» has languages without an ISO code: {', '.join(unknown)} "
                          "(add them to plan.language_names in config.json)")
        elif codes != [lang] + list(subtitles):
            errors.append(f"«{langs}» of row {found['row']} is «{values[langs]}» ({', '.join(codes) or 'empty'}), but the spec "
                          f"records in {lang} with subtitles {', '.join(subtitles) or '(none)'}: make them match "
                          "(the first language is the video's, the others are the subtitles)")
    channels = [norm(c) for c in PLAN.get("channels", [])]
    if channel and channels and norm(values.get(channel)) not in channels:
        errors.append(f"«{channel}» of row {found['row']} is «{values.get(channel) or '(empty)'}», not a YouTube channel "
                      f"of plan.channels ({', '.join(PLAN['channels'])})")
    link = (values.get(doc) or "").strip() if doc else ""
    if link and "docs.google.com/document/" not in link:
        warnings.append(f"«{doc}» of row {found['row']} is not a Google Doc ({link[:60]}): leave it empty; "
                        "the upload takes title and description from the post column")
    elif link:
        warnings.append(f"«{doc}» of row {found['row']} has a Doc: the upload ignores its title and description "
                        "and uses the post column")
    return errors, warnings


def _cell(found, name):
    return f"{PLAN_TAB}!{column(found['head'].index(name))}{found['row']}"


def plan_type(found):
    """What `set_type` would do with the row's type: {row, cell, column, from, to, action}."""
    tipo = COLUMNS["type"]
    if not (tipo and VIDEO_TYPE):
        raise DriveError("config.json has no plan.video_type or plan.columns.type")
    if tipo not in found["values"]:
        raise DriveError(f"{PLAN_TAB} has no column «{tipo}»")
    old = found["values"][tipo]
    return {"row": found["row"], "cell": _cell(found, tipo), "column": tipo, "from": old, "to": VIDEO_TYPE,
            "action": "unchanged" if old == VIDEO_TYPE else "set"}


def set_type(drive, todo):
    if todo["action"] == "set":
        drive.call("PUT", f"{SHEETS}/{PLAN_SHEET}/values/{todo['cell']}", params={"valueInputOption": "RAW"},
                   json={"values": [[todo["to"]]]})
        todo["done"] = True


def plan_post(found, text, replace=False, ours=()):
    """What `set_type` would do with the post column (title and description for the upload):
    {row, cell, column, from, to, action}. Another text is 'kept' unless replace, or it is one of `ours`
    (an older text written by this tool)."""
    post = COLUMNS["post"]
    if not post:
        raise DriveError("config.json has no plan.columns.post")
    if post not in found["values"]:
        raise DriveError(f"{PLAN_TAB} has no column «{post}»")
    old = found["values"][post].strip()
    action = ("unchanged" if old == text.strip()
              else "set" if not old or replace or old in [o.strip() for o in ours] else "kept")
    return {"row": found["row"], "cell": _cell(found, post), "column": post, "from": old, "to": text, "action": action}


def plan(drive, folder, files):
    """What `sync` would do, without changing anything: {folder, path, upload: [...], others: [...]}."""
    meta = drive.get(folder)
    if meta.get("mimeType") != FOLDER_MIME:
        raise DriveError(f"{folder} ({meta.get('name')}) is not a folder: the spec's drive_folder must be the "
                         "folder of the video (the «media» column of its row in the plan sheet)")
    if meta.get("trashed"):
        raise DriveError(f"the folder «{meta['name']}» is in the trash")
    where = drive.path_to(folder)
    if not where:
        raise DriveError(f"the folder «{meta['name']}» is not inside the videos folder (drive.videos_folder): "
                         f"the upload would not see it (https://drive.google.com/drive/folders/{VIDEOS_FOLDER} › product › video)")
    present = drive.children(folder)
    by_name = {}
    for f in present:
        by_name.setdefault(f["name"], []).append(f)
    upload, names = [], {os.path.basename(p) for p in files}
    for path in files:
        name = os.path.basename(path)
        same = by_name.get(name, [])
        if len(same) > 1:
            raise DriveError(f"the folder has {len(same)} files named {name}: leave only one")
        item = {"name": name, "path": path, "mb": round(os.path.getsize(path) / 1e6, 2)}
        if same and same[0].get("md5Checksum") == md5(path):
            item.update(action="unchanged", id=same[0]["id"], link=same[0].get("webViewLink"))
        elif same:
            item.update(action="replace", id=same[0]["id"])
        else:
            item["action"] = "create"
        upload.append(item)
    others = [{"name": f["name"], "id": f["id"], "folder": f["mimeType"] == FOLDER_MIME}
              for f in present if f["name"] not in names]
    return {"folder": folder, "folder_name": meta["name"], "folder_link": meta.get("webViewLink"),
            "path": " › ".join(where), "upload": upload, "others": others}


def sync(drive, todo, move_to=None, log=print):
    """Uploads `todo["upload"]`, then moves the other files (not folders) to the subfolder `move_to`."""
    for item in todo["upload"]:
        if item["action"] == "unchanged":
            log(f"  {item['name']}: already there, same content")
            continue
        log(f"  {item['name']}: {'new version' if item['action'] == 'replace' else 'uploading'} ({item['mb']} MB)…")
        got = drive.upload(item["path"], parent=todo["folder"], existing=item.get("id"))
        if got.get("md5Checksum") and got["md5Checksum"] != md5(item["path"]):
            raise DriveError(f"{item['name']}: Drive has different content than {item['path']} (md5)")
        item.update(id=got["id"], link=got.get("webViewLink"), done=True)
    moved = []
    if move_to:
        loose = [o for o in todo["others"] if not o["folder"]]
        if loose:
            sub = next((o for o in todo["others"] if o["folder"] and o["name"] == move_to), None)
            sub_id = sub["id"] if sub else drive.make_folder(move_to, todo["folder"])["id"]
            for o in loose:
                log(f"  {o['name']} → {move_to}/")
                drive.move(o["id"], todo["folder"], sub_id)
                moved.append(o["name"])
    return moved
