"""Prepare a demo video of an Odoo module, from the spec to the folder YouTube picks up. Guide: README.md.

Usage: python3 video.py <command> <spec.json> [options]

Commands:
  new     <spec.json>  create a spec skeleton (with --lang, --subtitles, --product, --plan-row, --base, --db)
  check   <spec.json>  validate the spec against spec.schema.json and the video rules, without recording
  demo    <spec.json>  rebuild the demo database and start Odoo in the spec's language (--lang to change it)
  record  <spec.json>  check, then record the scenes on the demo Odoo of the spec's "demo" block
                       (--only <scene> repeatable, --frames keeps the frames, --base/--db override the spec,
                        --lang/--out for a take in another language in another folder)
  subtitles <spec.json> <final>/<video>.<lang>.srt per subtitle language + <video>.chapters.json, from the notes'
                       timings of the recorded clips (scenes one after the other, as in the final video)
  cards   <spec.json>  opening, closing and thumbnail from the product's identity (products CSV, see config.json)
  build   <spec.json>  <final>/<video>.mp4 (opening + scenes in order + closing, 1920x1080 30 fps H.264, silent AAC,
                       +faststart), <video>.thumb.jpg (1280x720, < 2 MB) and the subtitles above
  review  <spec.json>  (planned) contact sheet of key frames + timing report
  drive   <spec.json>  copy the final files to the video's folder in Drive (spec "drive_folder", inside drive.videos_folder):
                       new version of the files already there (same link), --dry-run shows the plan,
                       --move-old [name] moves the other files of the folder to a subfolder; never deletes
                       and, with a plan sheet in config.json, sets the type of the spec's plan_row and writes
                       "youtube_description" in its post column (--replace-post if it already has another text)
  all     <spec.json>  (planned) check, record, build and review in a row

Output contract (for agents and scripts):
  - progress goes to stderr; the LAST line on stdout is one JSON object:
    {"ok": bool, "command": ..., "spec": ..., "errors": [...], "warnings": [...], ...command data}
  - exit code 0 = ok, 1 = errors (see "errors"), 2 = usage problem or command not implemented yet.
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "spec.schema.json")
PLANNED = {
    "review": "contact sheet of key frames and timing report",
    "all": "runs check, record, build and review in a row once review exists (today: record, then build)",
}


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


def finish(result, code=None):
    result.setdefault("errors", [])
    result.setdefault("warnings", [])
    result["ok"] = not result["errors"] and code in (None, 0)
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(code if code is not None else (0 if result["ok"] else 1))


# ---------------------------------------------------------------- check

def load_schema():
    with open(SCHEMA_PATH) as f:
        return json.load(f)


def where(path):
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else p)
    return out or "(spec)"


def schema_errors(schema, data, sub=None, prefix=""):
    """Schema errors as readable strings; sub = a $defs key to validate data against."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return [], ["jsonschema is not installed (pip install jsonschema): only the video rules were checked"]
    target = {"$defs": schema["$defs"], "$ref": f"#/$defs/{sub}"} if sub else schema
    errors = []
    for e in sorted(Draft202012Validator(target).iter_errors(data), key=lambda e: list(e.absolute_path)):
        place = where(list(e.absolute_path))
        if prefix:
            place = prefix if place == "(spec)" else f"{prefix}.{place}"
        errors.append(f"{place}: {e.message}")
    return errors, []


def text_errors(text, langs, place):
    """A localized text must have the recording language and every subtitle language."""
    if text is None:
        return []
    if isinstance(text, str):
        return [] if len(langs) == 1 else [f"{place}: plain text, but the video has subtitles: write it as "
                                            f"{{{', '.join(repr(l) + ': ...' for l in langs)}}}"]
    missing = [l for l in langs if l not in text]
    return [f"{place}: missing the {', '.join(missing)} text"] if missing else []


def check_spec(spec_path, lang_override=None):
    """Returns (spec or None, errors, warnings, info). lang_override: record in another language than the spec's."""
    errors, warnings = [], []
    if not os.path.isfile(spec_path):
        return None, [f"spec not found: {spec_path}"], [], {}
    try:
        with open(spec_path) as f:
            spec = json.load(f)
    except json.JSONDecodeError as e:
        return None, [f"not valid JSON: {e}"], [], {}
    schema = load_schema()
    actions = {k[len("step_"):]: k for k in schema["$defs"] if k.startswith("step_")}

    # Whole spec, with steps checked one by one below (clearer messages than a oneOf).
    loose = json.loads(json.dumps(schema))
    loose["$defs"]["step"] = {"type": "object"}
    errs, warns = schema_errors(loose, spec)
    errors += errs
    warnings += warns
    if not isinstance(spec, dict) or not isinstance(spec.get("scenes"), list):
        return None, errors or ["the spec needs a 'scenes' list"], warnings, {}

    for i, scene in enumerate(spec["scenes"]):
        if not isinstance(scene, dict):
            continue
        for part in ("setup", "steps", "post"):
            for j, step in enumerate(scene.get(part) or []):
                place = f"scenes[{i}].{part}[{j}]"
                if not isinstance(step, dict):
                    continue
                found = [k for k in step if k in actions]
                if len(found) != 1:
                    errors.append(f"{place}: {'no action' if not found else 'several actions (' + ', '.join(found) + ')'} "
                                  f"- one of: {', '.join(sorted(actions))}")
                    continue
                errs, _ = schema_errors(schema, step, sub=actions[found[0]], prefix=place)
                errors += errs

    lang = spec.get("lang", "es")
    subtitles = spec.get("subtitles", [])
    langs = [lang] + [l for l in subtitles if l != lang]
    if lang_override and lang_override != lang:
        if lang_override not in langs:
            langs.append(lang_override)
        lang = lang_override
    if spec.get("lang", "es") in subtitles:
        errors.append(f"subtitles: {spec.get('lang', 'es')} is the recording language; it needs no subtitles")

    names = [s.get("name") for s in spec["scenes"] if isinstance(s, dict)]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        errors.append(f"scenes: repeated names {', '.join(dup)} (each name is a clip file)")
    stem = os.path.splitext(os.path.basename(spec_path))[0]
    odd = [n for n in names if n and not n.startswith(stem + "-")]
    if odd:
        warnings.append(f"scenes: names should start with '{stem}-' (the spec file name): {', '.join(odd)}")

    for i, scene in enumerate(spec["scenes"]):
        if not isinstance(scene, dict):
            continue
        for part in ("setup", "steps", "post"):
            for j, step in enumerate(scene.get(part) or []):
                if not isinstance(step, dict):
                    continue
                place = f"scenes[{i}].{part}[{j}]"
                if "note" in step:
                    errors += text_errors(step["note"], langs, place + ".note")
                    if part != "steps":
                        warnings.append(f"{place}: a note in '{part}' is not recorded")
                for key in ("click", "hover", "highlight", "type", "at", "arrow", "spotlight", "badge", "zoom"):
                    value = step.get(key)
                    if isinstance(value, dict) and lang not in value:
                        errors.append(f"{place}.{key}: no text in the recording language '{lang}' "
                                      f"(has {', '.join(value)}): write it as it appears on screen in that language")
                for mark in ("arrow", "spotlight", "badge", "zoom"):
                    if step.get(mark) is True and not step.get("at_css"):
                        errors.append(f"{place}: '{mark}': true needs 'at_css' (or give the element's text instead of true)")

    durations = [scene_seconds(scene, schema, lang) if isinstance(scene, dict) else 0 for scene in spec["scenes"]]
    chapters = []
    for i, scene in enumerate(spec["scenes"]):
        if isinstance(scene, dict) and "chapter" in scene:
            chapters.append(i)
            errors += text_errors(scene["chapter"], langs, f"scenes[{i}].chapter")
    if chapters:
        if chapters[0] != 0:
            errors.append("chapters: the first scene must have a chapter (YouTube's first chapter starts at 0:00)")
        if len(chapters) < 3:
            warnings.append(f"chapters: {len(chapters)} defined; YouTube shows chapters only with 3 or more")
        closing_s = (closing_seconds(spec))
        for k, i in enumerate(chapters):
            stop = chapters[k + 1] if k + 1 < len(chapters) else len(spec["scenes"])
            secs = sum(durations[i:stop]) + (closing_s if stop == len(spec["scenes"]) else 0)
            if secs < 10:
                warnings.append(f"scenes[{i}].chapter: about {secs:.0f} s; YouTube wants chapters of 10 s or more "
                                "(longer 'after' or merge it with the next scene)")

    base_dir = os.path.dirname(os.path.abspath(spec_path))
    closing = spec.get("closing")
    if isinstance(closing, dict) and closing.get("image") and not os.path.isfile(os.path.join(base_dir, closing["image"])):
        errors.append(f"closing.image: {closing['image']} not found next to the spec")
    if generated_screens(spec):
        sys.path.insert(0, HERE)
        import cards

        if not spec.get("product"):
            errors.append("intro/closing: generated from the product's identity, so the spec needs 'product'")
        else:
            try:
                product = cards.find_product(spec["product"])
                cards.logo_file(product["Logo"])
                cards.logo_file(product["entity"]["Video logo"])
            except cards.CardError as e:
                errors.append(f"product: {e}")
    elif spec.get("product"):
        warnings.append("no 'intro' and a hand-made or missing closing: the video does not show the product's identity "
                        "(add \"intro\": {} and \"closing\": {} to generate them from the products CSV)")
    if not spec.get("demo"):
        warnings.append("demo: missing; 'record' then needs --base and --db")
    elif spec["demo"].get("rebuild") and not os.path.isfile(os.path.join(base_dir, spec["demo"]["rebuild"])):
        errors.append(f"demo.rebuild: {spec['demo']['rebuild']} not found next to the spec")
    if "lang" not in spec:
        warnings.append("lang: missing, 'es' assumed (the first language of the video)")
    todo = []

    def find_todo(value, path):
        if isinstance(value, str) and "TODO" in value:
            todo.append(where(path))
        elif isinstance(value, dict):
            for k, v in value.items():
                find_todo(v, path + [k])
        elif isinstance(value, list):
            for k, v in enumerate(value):
                find_todo(v, path + [k])

    find_todo(spec, [])
    if todo:
        errors.append(f"unfilled TODO in: {', '.join(todo)}")
    parts = os.path.abspath(spec_path).split(os.sep)
    if "worktrees" in parts or "repos" in parts:
        errors.append("the spec is inside a module repository: specs and demo data live in presentacion/, never in the repo")

    info = {"lang": lang, "subtitles": subtitles, "scenes": names, "chapters": len(chapters),
            "estimated_seconds": round(intro_seconds(spec) + sum(durations) + closing_seconds(spec), 1),
            "out": os.path.join(base_dir, spec.get("out", "clips"))}
    return spec, errors, warnings, info


def closing_seconds(spec):
    closing = spec.get("closing")
    return float(closing.get("seconds", 4)) if isinstance(closing, dict) else 0.0


def intro_seconds(spec):
    intro = spec.get("intro")
    return float(intro.get("seconds", 3)) if isinstance(intro, dict) else 0.0


def generated_screens(spec):
    """True if the spec asks for screens made from the product's identity (opening, or closing without image)."""
    closing = spec.get("closing")
    return isinstance(spec.get("intro"), dict) or (isinstance(closing, dict) and not closing.get("image"))


def screens(spec_path, spec, info):
    """Renders opening, closing and thumbnail from the products CSV (see cards.py)."""
    sys.path.insert(0, HERE)
    import cards

    final = os.path.join(os.path.dirname(os.path.abspath(spec_path)), spec.get("final", "final"))
    return cards.build(spec_path, spec, info["out"], final, log=log)


def scene_seconds(scene, schema, lang):
    """Rough length of a recorded scene, from the steps' waits (schema defaults when not given)."""
    total = float(scene.get("lead", 0.5)) + float(scene.get("tail", 1.0))
    for step in scene.get("steps") or []:
        if not isinstance(step, dict):
            continue
        action = next((k for k in step if f"step_{k}" in schema["$defs"]), None)
        if not action or action == "goto":
            continue
        props = schema["$defs"][f"step_{action}"]["properties"]
        default = lambda key: float(props.get(key, {}).get("default", 0))
        if action == "wait":
            total += float(step["wait"]) if isinstance(step["wait"], (int, float)) else 0
            continue
        if action == "note" and "after" not in step:
            text = step["note"].get(lang, "") if isinstance(step["note"], dict) else str(step["note"])
            total += max(2.5, len(text) / 15)
            continue
        total += float(step.get("after", default("after")))
        if "move" in props:
            total += float(step.get("move", default("move")))
        if action == "type" and isinstance(step["type"], str):
            total += len(step["type"]) * float(step.get("delay", default("delay")))
    return total


# ---------------------------------------------------------------- subtitles and chapters

SRT_LINE = 42      # characters per subtitle line (usual limit for readability)
MIN_CUE = 1.0      # seconds a subtitle stays at least


def clock(seconds, srt=False):
    """0:00 for chapters (h:mm:ss past an hour), 00:00:00,000 for .srt."""
    if srt:
        ms = int(round(seconds * 1000))
        return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def wrap(text, width=SRT_LINE):
    """One subtitle in at most two balanced lines: the break nearest the middle that keeps both lines short."""
    if len(text) <= width:
        return [text]
    words = text.split()
    best = None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        score = max(len(a), len(b)) + (0 if a[-1] in ",:;." else 4)
        if best is None or score < best[0]:
            best = (score, [a, b])
    return best[1]


def split_cue(text, width=SRT_LINE):
    """A note longer than two lines -> consecutive pieces of at most two lines, cut after punctuation if possible."""
    words, pieces = text.split(), []
    n = -(-len(text) // (2 * width))
    while len(pieces) < n - 1:
        rest = " ".join(words)
        target = len(rest) / (n - len(pieces))
        best = None
        for i in range(1, len(words)):
            head = " ".join(words[:i])
            if len(head) > 2 * width:
                break
            score = abs(len(head) - target) - (12 if head[-1] in ",:;." else 0)
            if best is None or score < best[0]:
                best = (score, i)
        i = best[1] if best else len(words) // 2
        pieces.append(" ".join(words[:i]))
        words = words[i:]
    pieces.append(" ".join(words))
    return [p for p in pieces if p]


def timeline(spec_path, spec, info):
    """Scenes one after the other, as in the final video: [{"scene", "start", "seconds", "notes", "chapter"}].
    Reads <out>/<scene>.notes.json written by 'record'; errors if a scene is missing or out of date."""
    errors, scenes, start = [], [], intro_seconds(spec)
    for scene in spec["scenes"]:
        path = os.path.join(info["out"], scene["name"] + ".notes.json")
        if not os.path.isfile(path):
            errors.append(f"{scene['name']}: not recorded yet (no {os.path.basename(path)}): run 'record' first")
            continue
        with open(path) as f:
            rec = json.load(f)
        if rec.get("lang") != info["lang"]:
            errors.append(f"{scene['name']}: recorded in '{rec.get('lang')}', the spec says '{info['lang']}': record it again")
        # Only the recording language is in the picture: the other languages (subtitles) come from the spec as it is now.
        current = [st["note"] for st in scene.get("steps", []) if "note" in st]
        on_screen = lambda value: value.get(info["lang"]) if isinstance(value, dict) else value
        if [on_screen(n["text"]) for n in rec["notes"]] != [on_screen(v) for v in current]:
            errors.append(f"{scene['name']}: its notes in '{info['lang']}' changed after recording it: "
                          f"record it again (--only {scene['name']})")
            continue
        notes = [{**n, "text": text} for n, text in zip(rec["notes"], current)]
        # The clip's real length (what the final video adds up), else the recorder's.
        clip = os.path.join(info["out"], scene["name"] + ".mp4")
        seconds = (media_seconds(clip) if os.path.isfile(clip) else None) or rec["seconds"]
        scenes.append({"scene": scene["name"], "start": start, "seconds": round(seconds, 3), "notes": notes,
                       "chapter": scene.get("chapter"), "file": clip})
        start += seconds
    return scenes, round(start, 2), errors


def build_subtitles(spec_path, spec, info):
    """Writes <final>/<video>.<lang>.srt per subtitle language and <final>/<video>.chapters.json."""
    scenes, total, errors = timeline(spec_path, spec, info)
    warnings, files = [], []
    if errors:
        return {"errors": errors, "warnings": warnings}
    video = os.path.splitext(os.path.basename(spec_path))[0]
    final = os.path.join(os.path.dirname(os.path.abspath(spec_path)), spec.get("final", "final"))
    os.makedirs(final, exist_ok=True)
    text_of = lambda value, lang: value[lang] if isinstance(value, dict) else value
    for lang in info["subtitles"]:
        cues = []
        for sc in scenes:
            for n in sc["notes"]:
                begin, end = sc["start"] + n["start"], sc["start"] + n["end"]
                if end - begin < MIN_CUE:
                    end = begin + MIN_CUE
                pieces = split_cue(text_of(n["text"], lang))
                total_chars, span = sum(len(x) for x in pieces), end - begin
                for piece in pieces:
                    share = span * len(piece) / total_chars
                    cues.append((begin, begin + share, piece))
                    begin += share
        # A cue never runs into the next one.
        cues = [(b, min(e, cues[i + 1][0] - 0.04) if i + 1 < len(cues) else e, t) for i, (b, e, t) in enumerate(cues)]
        for b, e, t in cues:
            lines = wrap(t)
            if any(len(line) > SRT_LINE + 6 for line in lines):
                warnings.append(f"subtitle in {lang} has a long line (> {SRT_LINE + 6} characters): {t[:50]}…")
            if len(t) / max(e - b, 0.1) > 21:
                warnings.append(f"subtitle in {lang} goes fast ({len(t) / (e - b):.0f} characters/s, 17-21 is comfortable): "
                                f"{t[:50]}… (longer 'after' on that note)")
        path = os.path.join(final, f"{video}.{lang}.srt")
        with open(path, "w", encoding="utf-8") as f:
            for i, (b, e, t) in enumerate(cues, 1):
                f.write(f"{i}\n{clock(b, True)} --> {clock(e, True)}\n" + "\n".join(wrap(t)) + "\n\n")
        files.append(path)
    closing = closing_seconds(spec)
    marks = [(sc["start"], sc["chapter"]) for sc in scenes if sc["chapter"]]
    if marks and scenes and marks[0][0] == scenes[0]["start"]:
        marks[0] = (0.0, marks[0][1])  # the first chapter includes the opening
    chapters = {}
    if marks:
        if marks[0][0] > 0:
            warnings.append("chapters: the first scene has no chapter; YouTube needs the first one at 0:00")
        if len(marks) < 3:
            warnings.append(f"chapters: {len(marks)}; YouTube shows chapters only with 3 or more")
        ends = [m[0] for m in marks[1:]] + [total + closing]
        for (begin, title), end in zip(marks, ends):
            if end - begin < 10:
                warnings.append(f"chapter '{text_of(title, info['lang'])}' lasts {end - begin:.1f} s; YouTube needs 10 s or more")
        for lang in [info["lang"]] + info["subtitles"]:
            chapters[lang] = "\n".join(f"{clock(b)} {text_of(t, lang)}" for b, t in marks)
        path = os.path.join(final, f"{video}.chapters.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(chapters, f, ensure_ascii=False, indent=1)
            f.write("\n")
        files.append(path)
    else:
        warnings.append("no chapters in the spec: the description goes without chapters")
    return {"errors": [], "warnings": warnings, "files": files, "seconds": total, "closing": closing,
            "scenes": [{"scene": sc["scene"], "start": round(sc["start"], 2), "seconds": sc["seconds"]} for sc in scenes],
            "chapters": chapters}


def ffmpeg():
    sys.path.insert(0, HERE)
    import record as recorder

    recorder.FFMPEG = recorder.FFMPEG or recorder.find_ffmpeg()
    return recorder


def media_seconds(path):
    return ffmpeg().media_seconds(path)


# ---------------------------------------------------------------- build

W, H, FPS = 1920, 1080, 30
THUMB_MAX_BYTES = 2 * 1024 * 1024  # YouTube limit for thumbnails


def build_video(spec_path, spec, info, scenes, made=None):
    """final/<video>.mp4 = the clips one after the other + the closing image, with a silent audio track;
    final/<video>.thumb.jpg = the closing image at 1280x720 (or a frame of the last scene)."""
    rec = ffmpeg()
    if not rec.FFMPEG:
        return {"errors": ["ffmpeg not found (see README.md > Requirements)"], "warnings": []}
    errors, warnings = [], []
    base = os.path.dirname(os.path.abspath(spec_path))
    video = os.path.splitext(os.path.basename(spec_path))[0]
    final = os.path.join(base, spec.get("final", "final"))
    os.makedirs(final, exist_ok=True)
    missing = [sc["scene"] for sc in scenes if not os.path.isfile(sc["file"])]
    if missing:
        return {"errors": [f"clip not found for {', '.join(missing)}: run 'record' first"], "warnings": []}
    made = made or {}
    closing = spec.get("closing")
    closing_img = (os.path.join(base, closing["image"]) if closing.get("image") else made.get("closing")) if closing else None
    closing_s = closing_seconds(spec)
    intro_img, intro_s = made.get("opening") if spec.get("intro") is not None else None, intro_seconds(spec)
    if not closing:
        warnings.append("no 'closing' in the spec: the video ends on the last scene")

    inputs, chains = [], []
    fit = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=white"
    still = lambda k: f"[{k}:v]fps={FPS},{fit},scale=out_range=tv,setsar=1,format=yuv420p[v{k}]"
    n = 0
    if intro_img:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{intro_s:.3f}", "-i", intro_img]
        chains.append(still(n))
        n += 1
    for sc in scenes:
        inputs += ["-i", sc["file"]]
        chains.append(f"[{n}:v]fps={FPS},{fit},setsar=1,format=yuv420p[v{n}]")
        n += 1
    if closing_img:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{closing_s:.3f}", "-i", closing_img]
        chains.append(still(n))
        n += 1
    total = (intro_s if intro_img else 0) + sum(sc["seconds"] for sc in scenes) + (closing_s if closing_img else 0)
    inputs += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    graph = ";".join(chains) + ";" + "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    out = os.path.join(final, f"{video}.mp4")
    run = subprocess.run([rec.FFMPEG, "-y", "-loglevel", "error", *inputs, "-filter_complex", graph,
                          "-map", "[v]", "-map", f"{n}:a", "-r", str(FPS), "-c:v", "libx264", "-preset", "slow",
                          "-crf", "18", "-pix_fmt", "yuv420p", "-color_range", "tv", "-c:a", "aac", "-b:a", "128k",
                          "-shortest", "-movflags", "+faststart", out], capture_output=True, text=True)
    if run.returncode:
        return {"errors": [f"ffmpeg failed joining the video: {run.stderr.strip()[-500:]}"], "warnings": warnings}
    got = media_seconds(out)
    if got is None or abs(got - total) > 0.3:
        errors.append(f"{out}: lasts {got} s, expected {total:.2f} s (opening + scenes + closing)")

    thumb = os.path.join(final, f"{video}.thumb.jpg")
    if made.get("thumb_png"):
        source = ["-i", made["thumb_png"]]
    elif closing_img:
        source = ["-i", closing_img]
    else:
        last = scenes[-1]
        source = ["-ss", f"{max(0.0, last['seconds'] - 1.5):.2f}", "-i", last["file"]]
    for quality in (2, 4, 6, 9):
        run = subprocess.run([rec.FFMPEG, "-y", "-loglevel", "error", *source, "-frames:v", "1", "-vf",
                              "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=white",
                              "-q:v", str(quality), thumb], capture_output=True, text=True)
        if run.returncode:
            errors.append(f"ffmpeg failed making the thumbnail: {run.stderr.strip()[-300:]}")
            break
        if os.path.getsize(thumb) < THUMB_MAX_BYTES:
            break
    else:
        errors.append(f"{thumb}: over 2 MB even at low quality; simplify the closing image")
    return {"errors": errors, "warnings": warnings, "video": out, "thumbnail": thumb,
            "video_seconds": round(got or 0, 2), "video_mb": round(os.path.getsize(out) / 1e6, 1)}


# ---------------------------------------------------------------- commands

def cmd_check(args):
    spec, errors, warnings, info = check_spec(args.spec)
    finish({"command": "check", "spec": args.spec, "errors": errors, "warnings": warnings, **info})


def cmd_record(args):
    spec, errors, warnings, info = check_spec(args.spec, args.lang)
    result = {"command": "record", "spec": args.spec, "errors": errors, "warnings": warnings}
    if errors:
        result["errors"].insert(0, "the spec has errors: nothing was recorded")
        finish(result)
    demo = spec.get("demo") or {}
    base, db = args.base or demo.get("base"), args.db or demo.get("db")
    if not base or not db:
        result["errors"].append("no demo Odoo: add a 'demo' block {base, db} to the spec or pass --base and --db")
        finish(result)
    sys.path.insert(0, HERE)
    import record as recorder

    try:
        clips = recorder.record(args.spec, base, db, demo.get("login", "admin"), demo.get("password", "admin"),
                                args.only, args.frames, args.port, log=log, lang=args.lang, out=args.out)
    except recorder.RecordError as e:
        hint = f" (rebuild the demo with {demo['rebuild']})" if demo.get("rebuild") and "not answering" in str(e) else ""
        result["errors"].append(str(e) + hint)
        finish(result)
    out = os.path.join(os.path.dirname(os.path.abspath(args.spec)), args.out) if args.out else info["out"]
    result.update(lang=args.lang or info["lang"], clips=clips, seconds=round(sum(c["seconds"] for c in clips), 2), out=out)
    finish(result)


def cmd_demo(args):
    import subprocess
    import time
    import urllib.request

    spec, errors, warnings, info = check_spec(args.spec, args.lang)
    result = {"command": "demo", "spec": args.spec, "errors": errors, "warnings": warnings}
    demo = (spec or {}).get("demo") or {}
    if not errors and not demo.get("rebuild"):
        errors.append("demo.rebuild: missing; add the script that rebuilds the database (e.g. grabar_v3.sh)")
    if errors:
        finish(result)
    lang = args.lang or info["lang"]
    script = os.path.join(os.path.dirname(os.path.abspath(args.spec)), demo["rebuild"])
    log(f"rebuilding the demo in '{lang}' with {demo['rebuild']} (a few minutes)…")
    started = time.time()
    run = subprocess.run(["bash", script], env={**os.environ, "REC_LANG": lang, "REBUILD_ONLY": "1"},
                         stdout=sys.stderr, stderr=sys.stderr)
    if run.returncode:
        result["errors"].append(f"{demo['rebuild']} failed (exit {run.returncode}); see its output above")
        finish(result)
    try:
        urllib.request.urlopen(demo["base"] + "/web/login", timeout=10)
    except Exception as e:
        result["errors"].append(f"Odoo is not answering at {demo['base']} after the rebuild: {e}")
        finish(result)
    result.update(lang=lang, base=demo["base"], db=demo["db"], seconds=round(time.time() - started))
    finish(result)


def cmd_new(args):
    if os.path.exists(args.spec):
        finish({"command": "new", "spec": args.spec, "errors": [f"{args.spec} already exists: edit it instead"]})
    stem = os.path.splitext(os.path.basename(args.spec))[0]
    subtitles = [s.strip() for s in (args.subtitles or "").split(",") if s.strip()]
    langs = [args.lang] + subtitles
    spec = {"$schema": os.path.relpath(SCHEMA_PATH, os.path.dirname(os.path.abspath(args.spec))),
            "title": args.title or "TODO: what the video shows",
            "product": args.product or "TODO_module",
            "lang": args.lang, "subtitles": subtitles}
    if args.plan_row:
        spec["plan_row"] = args.plan_row
    if args.drive_folder:
        spec["drive_folder"] = args.drive_folder
    spec["demo"] = {"base": args.base or "http://127.0.0.1:8096", "db": args.db or "TODO_db"}
    spec["out"] = "clips"
    spec["intro"] = {"seconds": 3}
    spec["closing"] = {"seconds": 4}
    spec["scenes"] = [{"name": f"{stem}-01-start", "path": "/odoo",
                       "chapter": {l: "TODO" for l in langs} if subtitles else "TODO",
                       "steps": [{"wait": 1}]}]
    os.makedirs(os.path.dirname(os.path.abspath(args.spec)), exist_ok=True)
    with open(args.spec, "w") as f:
        json.dump(spec, f, ensure_ascii=False, indent=1)
        f.write("\n")
    finish({"command": "new", "spec": args.spec, "warnings": ["fill the TODO values, then run: video.py check " + args.spec]})


def cmd_subtitles(args):
    spec, errors, warnings, info = check_spec(args.spec)
    result = {"command": "subtitles", "spec": args.spec, "errors": errors, "warnings": warnings}
    if errors:
        finish(result)
    built = build_subtitles(args.spec, spec, info)
    result["errors"] += built.pop("errors")
    result["warnings"] += built.pop("warnings")
    result.update(built)
    finish(result)


def cmd_build(args):
    spec, errors, warnings, info = check_spec(args.spec)
    result = {"command": "build", "spec": args.spec, "errors": errors, "warnings": warnings}
    if errors:
        finish(result)
    scenes, total, errs = timeline(args.spec, spec, info)
    if errs:
        result["errors"] += errs
        finish(result)
    made = {}
    if generated_screens(spec):
        sys.path.insert(0, HERE)
        import cards

        try:
            made = screens(args.spec, spec, info)
        except cards.CardError as e:
            result["errors"].append(f"screens: {e}")
            finish(result)
        result.update(product=made["product"], entity=made["entity"], opening=made["opening"], closing=made["closing"])
    log(f"joining opening + {len(scenes)} scenes + closing ({total:.1f} s before the closing)…")
    built = build_video(args.spec, spec, info, scenes, made)
    result["errors"] += built.pop("errors")
    result["warnings"] += built.pop("warnings")
    result.update(built)
    if result["errors"]:
        finish(result)
    subs = build_subtitles(args.spec, spec, info)
    result["errors"] += subs.pop("errors")
    result["warnings"] += subs.pop("warnings")
    result.update(subtitles=[f for f in subs.get("files", []) if f.endswith(".srt")],
                  chapters=subs.get("chapters"), scenes=subs.get("scenes"))
    finish(result)


def cmd_cards(args):
    spec, errors, warnings, info = check_spec(args.spec)
    result = {"command": "cards", "spec": args.spec, "errors": errors, "warnings": warnings}
    if errors:
        finish(result)
    sys.path.insert(0, HERE)
    import cards

    try:
        made = screens(args.spec, spec, info)
    except cards.CardError as e:
        result["errors"].append(str(e))
        finish(result)
    result.update(made)
    finish(result)


def final_files(spec_path, spec, info):
    """The files of final/ that the YouTube upload reads, in upload order; errors for the missing mp4."""
    base = os.path.dirname(os.path.abspath(spec_path))
    video = os.path.splitext(os.path.basename(spec_path))[0]
    final = os.path.join(base, spec.get("final", "final"))
    names = [f"{video}.mp4", f"{video}.thumb.jpg"] + [f"{video}.{lang}.srt" for lang in spec.get("subtitles", [])]
    names.append(f"{video}.chapters.json")
    errors, warnings, files = [], [], []
    for name in names:
        path = os.path.join(final, name)
        if os.path.isfile(path):
            files.append(path)
        elif name.endswith(".mp4"):
            errors.append(f"{path} not found: run 'build' first")
        elif name.endswith(".srt"):
            errors.append(f"{name} not in {final}: the spec has that subtitle language (run 'subtitles' or 'build')")
        else:
            warnings.append(f"{name} not in {final}: the video goes up without it (run 'build')")
    clips = [os.path.join(info["out"], sc["name"] + ".mp4") for sc in spec["scenes"]]
    newest = max((os.path.getmtime(c) for c in clips if os.path.isfile(c)), default=0)
    old = [os.path.basename(f) for f in files if os.path.getmtime(f) < newest]
    if old:
        warnings.append(f"{', '.join(old)} older than the clips: run 'build' again if a scene was re-recorded")
    return files, errors, warnings


def youtube_post(spec):
    """Text for the post column: «H1: <title>» and the description (the format the upload reads), plus the older
    texts this tool may have written there (description alone), which can be replaced without --replace-post."""
    title, desc = (spec.get("youtube_title") or "").strip(), (spec.get("youtube_description") or "").strip()
    if not (title and desc):
        return "", []
    return f"H1: {title}\n{desc}", [desc]


def cmd_drive(args):
    spec, errors, warnings, info = check_spec(args.spec)
    result = {"command": "drive", "spec": args.spec, "errors": errors, "warnings": warnings}
    if errors:
        finish(result)
    sys.path.insert(0, HERE)
    import drive

    folder = drive.folder_id(args.folder or spec.get("drive_folder"))
    if not folder:
        row = f" of row {spec['plan_row']}" if spec.get("plan_row") else ""
        result["errors"].append("no Drive folder: put in the spec 'drive_folder' = the folder of the video inside "
                                "drive.videos_folder" + (row and f" (the media column{row} of the plan sheet)") +
                                ", or pass --folder")
        finish(result)
    files, errs, warns = final_files(args.spec, spec, info)
    result["errors"] += errs
    result["warnings"] += warns
    if errs:
        finish(result)
    try:
        client = drive.Drive(args.key, getattr(args, "as"))
        log(f"reading the folder in Drive as {client.email}" + (f" (acting as {client.subject})" if client.subject else "") + "…")
        todo = drive.plan(client, folder, files)
    except drive.DriveError as e:
        result["errors"].append(str(e))
        finish(result)
    result.update(folder=todo["folder_link"] or folder, path=todo["path"])
    loose = [o["name"] for o in todo["others"] if not o["folder"]]
    extra_mp4 = [n for n in loose if n.lower().endswith(".mp4")]
    if extra_mp4 and not args.move_old:
        result["errors"].append(f"the folder has other mp4 ({', '.join(extra_mp4)}): the YouTube upload needs only the "
                                "final one. Rerun with --move-old (moves the other files to a subfolder, deletes nothing)")
    elif loose and not args.move_old:
        result["warnings"].append(f"other files in the folder, left as they are: {', '.join(loose)}")
    result["plan"] = [{k: it[k] for k in ("name", "action", "mb")} for it in todo["upload"]]
    if args.move_old and loose:
        result["plan_move"] = {"to": args.move_old, "files": loose}
    row_todo = post_todo = None
    if drive.PLAN_SHEET:
        try:
            found = drive.find_row(client, folder, spec.get("plan_row"))
            result["row"] = found["row"]
            result["warnings"] += found["warnings"]
            errs, warns = drive.check_row(found, info["lang"], spec.get("subtitles", []))
            result["errors"] += errs
            result["warnings"] += warns
            row_todo = drive.plan_type(found)
            result["plan_sheet"] = {k: row_todo[k] for k in ("row", "column", "from", "to", "action")}
            text, ours = youtube_post(spec)
            if not text:
                result["errors"].append("the spec needs 'youtube_title' and 'youtube_description': 'drive' writes them in "
                                        "the post column of the row, where the YouTube upload takes them from")
            else:
                post_todo = drive.plan_post(found, text, args.replace_post, ours)
                result["plan_post"] = {k: post_todo[k] for k in ("row", "column", "action")}
                if post_todo["action"] == "kept":
                    result["errors"].append(f"«{post_todo['column']}» of row {post_todo['row']} already has another text: "
                                            "rerun with --replace-post to write the spec's title and description")
        except drive.DriveError as e:
            result["errors"].append(f"plan sheet: {e}")
    else:
        result["warnings"].append("no plan sheet in config.json: only the files are copied")
    if result["errors"] or args.dry_run:
        finish(result)
    try:
        log(f"copying {len(files)} files to {todo['path']}…")
        moved = drive.sync(client, todo, args.move_old, log)
    except drive.DriveError as e:
        result["errors"].append(str(e))
        result["uploaded"] = [{k: it.get(k) for k in ("name", "action", "id", "link")} for it in todo["upload"] if it.get("done")]
        finish(result)
    result["uploaded"] = [{k: it.get(k) for k in ("name", "action", "id", "link")} for it in todo["upload"]]
    if moved:
        result["moved"] = {"to": args.move_old, "files": moved}
    if row_todo:
        try:
            drive.set_type(client, row_todo)
            result["sheet"] = {k: row_todo[k] for k in ("row", "column", "from", "to", "action")}
            if row_todo["action"] == "set":
                log(f"row {row_todo['row']} of the plan sheet: «{row_todo['column']}» = {row_todo['to']}")
        except drive.DriveError as e:
            result["errors"].append(f"files uploaded, but «{row_todo['column']}» of row {row_todo['row']} could not "
                                    f"be changed to «{row_todo['to']}»: {e}. Change it by hand.")
    if post_todo:
        try:
            drive.set_type(client, post_todo)
            result["sheet_post"] = {k: post_todo[k] for k in ("row", "column", "action")}
            if post_todo["action"] == "set":
                log(f"row {post_todo['row']} of the plan sheet: «{post_todo['column']}» = youtube_title + youtube_description")
        except drive.DriveError as e:
            result["errors"].append(f"files uploaded, but «{post_todo['column']}» of row {post_todo['row']} could not be written: {e}. "
                                    "Paste the spec's title (as «H1: …») and description there by hand.")
    finish(result)


def cmd_planned(args):
    finish({"command": args.command, "spec": args.spec,
            "errors": [f"'{args.command}' is not implemented yet ({PLANNED[args.command]}); see README.md > Status"]}, 2)


def main():
    parser = argparse.ArgumentParser(prog="video.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check", help="validate a spec without recording")
    p.add_argument("spec")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("record", help="check, then record the scenes")
    p.add_argument("spec")
    p.add_argument("--only", action="append", help="scene name to record (repeatable)")
    p.add_argument("--frames", action="store_true", help="keep the frames folder (debug)")
    p.add_argument("--base", help="override demo.base")
    p.add_argument("--db", help="override demo.db")
    p.add_argument("--port", type=int, default=9334, help="Chrome debugging port")
    p.add_argument("--lang", help="record in this language instead of the spec's (the demo must be rebuilt in it: demo --lang)")
    p.add_argument("--out", help="output folder instead of the spec's 'out' (e.g. clips-en for a test take)")
    p.set_defaults(func=cmd_record)
    p = sub.add_parser("cards", help="opening, closing and thumbnail from the product's identity (products CSV), to review them")
    p.add_argument("spec")
    p.set_defaults(func=cmd_cards)
    p = sub.add_parser("build", aliases=["montar"], help="final mp4 (scenes + closing, silent audio), thumbnail, .srt and chapters.json")
    p.add_argument("spec")
    p.set_defaults(func=cmd_build)
    p = sub.add_parser("subtitles", help="write the .srt of each subtitle language and chapters.json from the recording")
    p.add_argument("spec")
    p.set_defaults(func=cmd_subtitles)
    p = sub.add_parser("demo", help="rebuild the demo database and start Odoo (runs demo.rebuild with REC_LANG)")
    p.add_argument("spec")
    p.add_argument("--lang", help="language of the demo instead of the spec's")
    p.set_defaults(func=cmd_demo)
    p = sub.add_parser("new", help="create a spec skeleton")
    p.add_argument("spec")
    p.add_argument("--lang", default="es", help="recording language")
    p.add_argument("--subtitles", help="subtitle languages, comma separated, e.g. en,de")
    p.add_argument("--product", help="module, e.g. my_module")
    p.add_argument("--title")
    p.add_argument("--plan-row", type=int)
    p.add_argument("--drive-folder", help="folder of the video in Drive (inside drive.videos_folder)")
    p.add_argument("--base")
    p.add_argument("--db")
    p.set_defaults(func=cmd_new)
    p = sub.add_parser("drive", help="copy the final files to the video's folder in Drive (spec drive_folder)")
    p.add_argument("spec")
    p.add_argument("--folder", help="folder link or id instead of the spec's drive_folder")
    p.add_argument("--key", help="service account key (JSON); default VIDEO_DRIVE_KEY or drive.key of config.json")
    p.add_argument("--as", help="act as this person (domain-wide delegation; default VIDEO_DRIVE_AS or drive.act_as)")
    p.add_argument("--move-old", nargs="?", const="previous", metavar="SUBFOLDER",
                   help="move the other files of the folder to this subfolder (default 'previous'); never deletes")
    p.add_argument("--replace-post", action="store_true",
                   help="write youtube_description in the post column even if the row already has another text there")
    p.add_argument("--dry-run", action="store_true", help="show what would be uploaded or moved, change nothing")
    p.set_defaults(func=cmd_drive)
    for name in PLANNED:
        p = sub.add_parser(name, help="(planned) " + PLANNED[name])
        p.add_argument("spec")
        p.set_defaults(func=cmd_planned)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
