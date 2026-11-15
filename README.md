# Odoo demo videos from a JSON spec

Record short demo videos of an Odoo module from a JSON *spec*: each scene opens a page of a local demo database and
runs clicks, typing and on-screen notes in headless Chrome. The tool then builds the final video with an opening
and a closing screen, a thumbnail, subtitles and YouTube chapters, and can copy the result to a Google Drive folder
for a YouTube upload (an n8n workflow, an Apps Script...) to pick up.

```
spec.json ──record──▶ clips/*.mp4 ──build──▶ final/v1.mp4 + v1.thumb.jpg + v1.<lang>.srt + v1.chapters.json ──drive──▶ Drive folder
                                                                                                          + title and description in the plan sheet
```

## Requirements

- Python 3.9+ with `pip install jsonschema Pillow requests "pyjwt[crypto]"` (`requests` and `pyjwt` only for `drive`).
- Google Chrome (`google-chrome`, or set `CHROME` to its path).
- ffmpeg 7. Either set `FFMPEG` to an ffmpeg on your machine (`export FFMPEG=$(which ffmpeg)`), or build the image
  used by `bin/ffmpeg`: `docker build -t local/ffmpeg -f bin/ffmpeg.Dockerfile bin`. That wrapper only sees the
  folder `VIDEO_WORKSPACE` (default: three levels above `bin/`): keep specs and clips inside it.
- A local Odoo with demo data for the module you show (the spec's `demo` block).

## Setup

```bash
cp config.example.json config.json   # your settings; config.json is not committed
```

Everything in `config.json` is optional. Without `drive` and `plan` you can still record and build videos.

| Key | What it is |
|---|---|
| `drive.key` | Google service account key (JSON). Never in the repo nor in a spec. Also `--key` or `VIDEO_DRIVE_KEY`. |
| `drive.act_as` | Person the service account acts as (domain-wide delegation, scope `https://www.googleapis.com/auth/drive`). Needed to create files in a personal My Drive. Also `--as` or `VIDEO_DRIVE_AS`. |
| `drive.videos_folder` | Parent folder of the videos (product folder › video folder). `drive` refuses folders outside it. Also `VIDEO_DRIVE_FOLDER`. |
| `plan.*` | Optional Google Sheet with one row per video. `drive` finds the row by its `columns.media` cell (the video folder; rows move when the sheet is sorted, so the spec's `plan_row` is only a hint), checks it (`columns.languages` = the spec's language and subtitles, `columns.channel` one of `channels`, `columns.doc` empty), then sets `video_type` in `columns.type` and writes the YouTube title and description in `columns.post`. `language_names` maps the names used in the sheet to ISO codes. |
| `identity.*` | Products CSV, entities CSV, folder of logo copies and the font of the opening, closing and thumbnail; `cta`: the call to action `drive` adds to the YouTube description, per language, with `{store}` (product store link), `{entity}` and `{web}` (entity website); it replaces the description line with the store link, and lines with an e-mail address are left out. |

Other environment variables: `VIDEO_CONFIG` (another config file), `VIDEO_CACHE` (cache folder), `CHROME`, `FFMPEG`.

## Try the example

```bash
VIDEO_CONFIG=config.example.json python3 video.py check examples/videos/v1.json
VIDEO_CONFIG=config.example.json python3 video.py cards examples/videos/v1.json   # opening, closing, thumbnail
```

`examples/` has an example product branded with the author's identity (`products.csv`, `entities.csv`, `logos/`) and
a spec that tours Odoo Contacts in English with Spanish subtitles. To record it, start an Odoo with demo data on `http://127.0.0.1:8069` (database `demo_contacts`).

## Commands

Every command prints its progress on stderr and, as the **last line on stdout, one JSON object**
`{"ok", "command", "spec", "errors", "warnings", ...}`. Exit code 0 = ok, 1 = errors, 2 = usage problem.

| Command | What it does |
|---|---|
| `video.py new <spec>` | Spec skeleton (`--lang`, `--subtitles`, `--product`, `--title`, `--plan-row`). Fill the `TODO`s. |
| `video.py check <spec>` | Validates the spec against `spec.schema.json`, texts in every language, product identity, demo script. |
| `video.py demo <spec>` | Rebuilds the demo database with the spec's `demo.rebuild` script (`REC_LANG`, `REBUILD_ONLY=1`). |
| `video.py record <spec>` | Records one clip per scene (`--only <scene>`, `--lang`, `--out`, `--frames`). |
| `video.py subtitles <spec>` | `.srt` per subtitle language and `chapters.json`, from the notes' timings. |
| `video.py cards <spec>` | Opening, closing and thumbnail, to review them. |
| `video.py build <spec>` | Final video (opening + scenes + closing, 1920x1080, 30 fps, H.264), thumbnail and subtitles. |
| `video.py drive <spec>` | The hand-off to the upload: checks the row of the plan sheet, copies the final files to `drive_folder` (new version of existing files, same link; md5 checked), sets the row's type and writes `H1: <youtube_title>` + `youtube_description` + the call to action in its post column. `--dry-run` shows the plan and changes nothing, `--move-old [subfolder]` moves other files away (never deletes), `--replace-post` overwrites a post column with a text it did not write. |

## The spec

`spec.schema.json` describes every field. In short:

- `title`, `product` (its `Module` or `Product` in the products CSV), `feature` (the specific function the video
  demonstrates, shown big on the opening screen; required with a generated opening) and `thumbnail_title`
  (default: `feature`).
- `composition`: the layout of the opening and the thumbnail, from the goal of the video:
  `A` question (default; `feature` is the doubt or problem), `B` concept (`feature` is the idea, product logo on the
  left, optional `subline`), `C` guide (`feature` is the task, a `label` such as "Step-by-step guide" above and an
  optional `version` line).
- `lang` (recording language) and `subtitles` (other languages). Texts are `{"en": ..., "es": ...}` with every
  language, or a plain string when there are no subtitles.
- `demo`: `base` (local Odoo URL), `db`, optional `rebuild` script.
- `intro`, `closing`: generated from the product's identity, or replaced by something made elsewhere — `closing.image`
  for a hand-made image, or `clip` (`intro.clip`, `closing.clip`) for a video exported from any editor; a clip keeps its
  own length. `thumbnail`: a hand-made 1280x720 image (otherwise it follows the generated opening, or a frame of the
  opening clip).
- `scenes`: `name` (`v1-01-word`, also the clip name), `path` (Odoo URL), optional `chapter`, `setup`/`post`
  steps that are not recorded, `lead`/`tail` seconds, and `steps`: `wait`, `goto`, `click`, `hover`, `highlight`
  (each also as `*_css`), `type`, `key`, `scroll`, `scroll_to`, `note`, `arrow`, `spotlight`, `badge`, `zoom`
  (and their `un*`), `clear`, `js`, `call`, usually with `after` seconds.
- `youtube_title` (100 characters at most) and `youtube_description` (plain text, one paragraph per line, optional
  last line `Tags: a, b, c`), `drive_folder` and `plan_row` (a hint) for the Drive step.

YouTube chapters: the first one at 0:00, at least 3, each 10 s or longer.

## Two ways to make the opening and the closing

Both end the same way — `build` joins opening + recorded scenes + closing — and differ only in where the opening and
the closing come from:

1. **Generated** (default): `intro: {}` / `closing: {}` render them from the product's identity (see below).
2. **Made in a video editor** (for example, a template in Google Vids): export each as an mp4 and point to it:

   ```json
   "intro":   {"clip": "clips/v3-00-opening.mp4"},
   "closing": {"clip": "clips/v3-99-closing.mp4"}
   ```

   Each one keeps its own length, so the subtitle and chapter times already include them. With both as clips the spec
   does not need `product` or `feature`. You can also mix: a clip opening with a generated closing, or the reverse.
   Add `"thumbnail": "clips/v3-thumb.png"` if you made the thumbnail in the editor too; without it the thumbnail is
   a frame of the opening clip.

The scenes are recorded the same way in both (`record`).

## Product identity

The opening shows, in one of the three compositions, the product name, the feature the video demonstrates and the entity that makes it; the closing, the product logo, its store link and
the entity's web; the thumbnail uses the opening's composition with the thumbnail title. They come from two CSV (they can be tabs of a
Google Sheet, *File › Download › CSV*); see `examples/` for the columns (Spanish headers of the original sheet are
accepted too, see `cards.py`). A logo can be a local path, a public URL or
a Google Drive link; for a Drive file shared with people (not by link), save a copy as `<logos_dir>/<Drive id>.png`.
The default font is Open Sans (`fonts/`).

## One tool, many workspaces

The scripts can live in one folder and serve several projects. Keep your real data (CSVs, logos, specs,
generated clips) in each project's own workspace and point the tool at it:

```bash
VIDEO_CONFIG=/path/to/workspace/config.json python3 /path/to/Auto-demo-videos/video.py cards <spec>
```

Relative paths inside that config resolve against the config file's folder, so `logos_dir: "logos"` means
`<workspace>/logos`; anything not found there (the font, the examples) falls back to the scripts' folder.
Outputs are written next to each spec, so results stay in the workspace. Only generic demos and examples
belong in this repository.

## Companion tools

- `shoot.py` — store screenshots of an Odoo module from a local demo, with headless Chrome (CDP).
- `build_index.py` — the Odoo Apps store page (`static/description/index.html`) of a module, from a content file.

Both read a spec/content file that lives in your workspace, never in the module's repository.

## Status

Done: `new`, `check`, `demo`, `record`, `subtitles`, `cards`, `build`, `drive`.
Planned: `review` (contact sheet of key frames and timing report) and `all` (every step in a row).

## Why this exists

I built this tool for my own work as an **IT consultant and project manager**: companies ask me for
short, consistent demo videos of their Odoo modules, in several languages and with their own branding. Recording them
by hand took hours per video and every re-take meant starting again. With a spec, a video is reproducible: change
a step or a text, record again, and the opening, closing, thumbnail, subtitles and chapters follow.

It is one piece of a larger publishing process I designed and automated: the plan of what to publish lives in a
Google Sheet, this tool produces the video and drops it in Drive, and an n8n workflow uploads it to the right
YouTube channel with translated titles and descriptions, then writes the result back to the plan.


## License

[MIT](LICENSE) © 2026 Yadenis Piñero. The Open Sans font in `fonts/` is under the SIL Open Font License
([fonts/OFL.txt](fonts/OFL.txt)). Odoo is a trademark of Odoo S.A.; this project is not affiliated with it.
