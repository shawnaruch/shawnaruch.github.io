#!/usr/bin/env python3
"""Furnace camera: grab a frame, have Claude read the furnace, publish the result.

Runs on the Mac mini (the camera is only reachable on the home network), on a
launchd timer installed by install-mac.sh. Each run:

  1. grabs one frame from the Eufy camera's RTSP stream with ffmpeg
  2. asks Claude what the furnace shows (status display, error code, hazards)
  3. force-pushes frame.jpg + camera.json to the `camera-data` branch, which the
     dashboard at home.esemay.com reads
  4. sends an ntfy push when an error code appears or clears

Secrets live in ~/.config/furnace-cam/env (never in the repo):
  CAMERA_URL         rtsp://user:pass@192.168.4.109:554/live0
  ANTHROPIC_API_KEY  Claude API key
  GITHUB_TOKEN       fine-grained token: this repo only, Contents read/write
  NTFY_TOPIC         optional, same topic as the dashboard's alerts
Optional: CAMERA_MODEL (default claude-opus-5-5), GITHUB_REPO.

Usage: furnace_cam.py            one run
       furnace_cam.py --dry-run  analyze but don't publish or push alerts
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone

import anthropic

CONFIG_DIR = os.path.expanduser(os.environ.get("FURNACE_CAM_DIR", "~/.config/furnace-cam"))
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")
LAST_FRAME = os.path.join(CONFIG_DIR, "last-frame.jpg")
BRANCH = "camera-data"

SCHEMA = {
    "type": "object",
    "properties": {
        "shows_furnace": {
            "type": "boolean",
            "description": "True only if the image shows the furnace or its control board/display.",
        },
        "status": {
            "type": "string",
            "enum": ["normal", "error", "unclear", "not_visible"],
            "description": "normal: display/LED shows a normal state; error: an error or fault code is shown; "
                           "unclear: furnace visible but the display can't be read; not_visible: no furnace in view.",
        },
        "display_reading": {
            "type": "string",
            "description": "Exactly what the status display or LED shows (e.g. '57', 'solid green', 'off'). Empty if none visible.",
        },
        "error_code": {
            "type": "string",
            "description": "The error/fault code if one is shown, digits only (e.g. '57'). Empty if none.",
        },
        "summary": {
            "type": "string",
            "description": "One or two plain sentences on what the furnace shows right now.",
        },
        "concerns": {
            "type": "string",
            "description": "Anything unsafe or unusual in view: water, scorch marks, smoke, removed panels, "
                           "disconnected or bypassed wires. Empty if nothing.",
        },
    },
    "required": ["shows_furnace", "status", "display_reading", "error_code", "summary", "concerns"],
    "additionalProperties": False,
}

PROMPT = """This is a frame from a camera pointed at a home gas furnace.
Context: the furnace has been throwing error 57, which this owner reads as a pressure-switch fault.
A technician is booked. This check runs every 15 minutes so the owner knows if the furnace is faulting again.

Read the image and report:
- whether the furnace (or its control board / status display) is in view
- exactly what the status display or LED shows, character for character if it is a digit display
- any error code shown
- anything unsafe or unusual (water, scorch marks, smoke, panels off, wires disconnected or jumpered)

Report only what you can see. A single frame cannot show a blink pattern: if the code is a blinking LED,
describe it as lit/unlit and color, mark status "unclear" unless the code is plainly readable, and do not
guess a blink count. If the image is dark, blurry, or the display is out of frame, say so."""


def load_env():
    path = os.path.join(CONFIG_DIR, "env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def redact(text):
    for key in ("CAMERA_URL", "GITHUB_TOKEN", "ANTHROPIC_API_KEY"):
        val = os.environ.get(key)
        if val:
            text = text.replace(val, f"<{key}>")
    return text


def grab_frame(url, out_path):
    """One JPEG from the stream, at most 1280px wide. Raises on failure."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if url.startswith("rtsp"):
        cmd += ["-rtsp_transport", "tcp"]
    cmd += ["-i", url, "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-q:v", "4", "-y", out_path]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(redact(f"ffmpeg failed: {e.stderr.strip()[-300:]}")) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg timed out (camera asleep or unreachable?)") from None
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("ffmpeg produced no image")


def analyze(jpeg_path, client=None):
    client = client or anthropic.Anthropic()
    with open(jpeg_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    model = os.environ.get("CAMERA_MODEL", "claude-opus-5-5")
    response = client.beta.messages.create(
        model=model,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # if the request is declined, the API retries on a fallback model
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("analysis declined by the model")
    text = next(b.text for b in response.content if b.type == "text")
    result = json.loads(text)
    result["model"] = response.model
    return result


def publish(files, token, repo):
    """Force-push `files` ({name: path}) as a single commit on the camera-data branch."""
    work = tempfile.mkdtemp(prefix="furnace-cam-")
    try:
        for name, src in files.items():
            shutil.copy(src, os.path.join(work, name))
        git = lambda *a: subprocess.run(["git", *a], cwd=work, check=True, capture_output=True, text=True, timeout=60)
        git("init", "-q", "-b", BRANCH)
        git("-c", "user.name=furnace-cam", "-c", "user.email=furnace-cam@users.noreply.github.com",
            "add", *files.keys())
        git("-c", "user.name=furnace-cam", "-c", "user.email=furnace-cam@users.noreply.github.com",
            "commit", "-q", "-m", f"Furnace camera {now_iso()}")
        remote = repo if repo.startswith(("/", "file:")) else f"https://x-access-token:{token}@github.com/{repo}.git"
        git("push", "-q", "-f", remote, f"HEAD:{BRANCH}")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(redact(f"git {e.args} failed: {(e.stderr or '').strip()[-300:]}")) from None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def push_alert(title, message, priority="high"):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print(f"(no NTFY_TOPIC) would push: {title}: {message}")
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    req = urllib.request.Request(f"{server}/{topic}", data=message.encode(), method="POST", headers={
        "Title": title, "Priority": priority, "Tags": "camera",
        "Click": "https://home.esemay.com", "User-Agent": "furnace-cam/1.0"})
    urllib.request.urlopen(req, timeout=30).read()


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_state(state):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def alerts(result, state):
    """Push once when an error code appears, once when it clears."""
    code = result.get("error_code") or ""
    last = state.get("alerted_code") or ""
    if result.get("status") == "error" and code and code != last:
        push_alert(f"Furnace showing error {code}",
                   f"{result.get('summary', '')} Display: {result.get('display_reading', '')}".strip())
        state["alerted_code"] = code
    elif last and result.get("status") == "normal":
        push_alert("Furnace display back to normal", result.get("summary", ""), priority="default")
        state["alerted_code"] = ""
    if result.get("concerns") and result["concerns"] != state.get("alerted_concern"):
        push_alert("Furnace camera: something to check", result["concerns"])
        state["alerted_concern"] = result["concerns"]
    elif not result.get("concerns"):
        state["alerted_concern"] = ""


def main():
    dry = "--dry-run" in sys.argv
    load_env()
    url = os.environ.get("CAMERA_URL")
    if not url:
        sys.exit(f"CAMERA_URL not set (put it in {CONFIG_DIR}/env)")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    state = load_state()
    tmp = tempfile.mkdtemp(prefix="furnace-frame-")
    frame = os.path.join(tmp, "frame.jpg")
    record = {"captured_at": now_iso()}
    try:
        try:
            grab_frame(url, frame)
            result = analyze(frame)
            record.update(result)
            record["image"] = "frame.jpg" if result["shows_furnace"] else None
            if result["shows_furnace"]:
                shutil.copy(frame, LAST_FRAME)
            if not dry:
                alerts(result, state)
        except Exception as e:  # noqa: BLE001  publish the failure so the dashboard shows it
            record.update({"status": "camera_error", "error": redact(str(e))[:300],
                           "summary": "The camera job could not read the furnace this time."})
            # Keep showing the last good frame, marked stale.
            if os.path.exists(LAST_FRAME):
                shutil.copy(LAST_FRAME, frame)
                record["image"] = "frame.jpg"
                record["image_stale"] = True
            print(f"camera run failed: {record['error']}", file=sys.stderr)
        state["last_run"] = record["captured_at"]
        save_state(state)

        meta = os.path.join(tmp, "camera.json")
        with open(meta, "w") as f:
            json.dump(record, f, indent=1)
        print(json.dumps(record, indent=1))
        if dry:
            return
        files = {"camera.json": meta}
        if record.get("image"):
            files["frame.jpg"] = frame
        publish(files, os.environ.get("GITHUB_TOKEN", ""),
                os.environ.get("GITHUB_REPO", "shawnaruch/shawnaruch.github.io"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
