"""Qore server: serves the page and runs AI jobs in the background.

Keys never reach the browser. Set them as environment variables in Render:
ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY and (recommended) APP_PASSWORD.
"""
import hmac
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

import ai
from prompts import COMMANDS, TAGS, system_for

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024

JOBS = {}
LOCK = threading.Lock()
HITS = {}
MAX_TEXT = 60000


def password():
    return ai.env("APP_PASSWORD")


def authed():
    pw = password()
    return (not pw) or hmac.compare_digest(request.headers.get("X-Access", ""), pw)


def limited():
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip()
    now = time.time()
    h = [t for t in HITS.get(ip, []) if now - t < 3600]
    if len(h) >= int(ai.env("HOURLY_LIMIT", "60")):
        HITS[ip] = h
        return True
    h.append(now)
    HITS[ip] = h
    return False


def guard():
    if not authed():
        return jsonify(error="Wrong or missing password.", auth=True), 401
    return None


# ------------------------------------------------------------ files
def read_file(f):
    """Returns (text, ai_file or None). ai_file is sent to the model as is (images)."""
    name = (f.filename or "file").lower()
    data = f.read()
    mime = f.mimetype or ""
    if name.endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(data))
        out = [p.text for p in d.paragraphs if p.text.strip()]
        for t in d.tables:
            for r in t.rows:
                out.append(" | ".join(c.text.strip() for c in r.cells))
        return f"[File {f.filename}]\n" + "\n".join(out), None
    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            txt = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
        except Exception:
            txt = ""
        return f"[File {f.filename}]\n{txt}", None
    if mime.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        m = mime if mime.startswith("image/") else "image/jpeg"
        return f"[Picture {f.filename} attached]", {"kind": "image", "mime": m, "data": data}
    return f"[File {f.filename}]\n" + data.decode("utf-8", "ignore"), None


# ------------------------------------------------------------ jobs
def new_job():
    jid = uuid.uuid4().hex[:12]
    with LOCK:
        for k in [k for k, v in JOBS.items() if time.time() - v["t"] > 7200]:
            JOBS.pop(k, None)
        JOBS[jid] = {"t": time.time(), "state": "run", "steps": [], "result": None, "error": None}
    return jid


def step(jid, msg):
    with LOCK:
        JOBS[jid]["steps"].append(msg)


def work(jid, cmd, text, tags, history, prev_doc, ai_files):
    spec = COMMANDS[cmd]
    try:
        forced = next((t for t in tags if t in ("claude", "gpt", "gemini")), None)
        tier = "deep" if "deep" in tags else ("fast" if "fast" in tags else None)
        sources = []
        prompt = text
        want_search = spec["search"] and "nosearch" not in tags or "search" in tags
        if want_search and cmd in ("build_document", "lesson", "ask") and text.strip():
            notes, sources = ai.research(text[:1500], say=lambda m: step(jid, m))
            if notes:
                prompt = f"{text}\n\n--- RESEARCH NOTES (from the web; use as sources) ---\n{notes}"
        if cmd == "revise":
            prompt = f"CURRENT DOCUMENT:\n{prev_doc}\n\nCHANGE REQUEST:\n{text}"
        messages = [m for m in history if m.get("text")][-6:] if cmd == "ask" else []
        messages.append({"role": "user", "text": prompt[:MAX_TEXT]})
        step(jid, "Starting...")
        r = ai.run_with_fallback(
            spec["route"], system_for(cmd), messages, files=ai_files,
            thinking=spec["thinking"] or tier == "deep", max_tokens=16000 if spec["kind"] == "doc" else 6000,
            forced=forced, tier_override=tier, say=lambda m: step(jid, m))
        out = {"kind": spec["kind"], "cmd": cmd, "text": r["text"], "model": r["model"],
               "provider": ai.LABEL[r["provider"]], "note": r["note"],
               "sources": (sources + r["sources"])[:8]}
        if spec["kind"] == "json":
            out["data"] = ai.extract_json(r["text"])
        with LOCK:
            JOBS[jid].update(state="done", result=out)
    except ai.AIError as e:
        with LOCK:
            JOBS[jid].update(state="error", error=str(e))
    except Exception as e:      # noqa: BLE001
        with LOCK:
            JOBS[jid].update(state="error", error="Something went wrong: " + str(e)[:160])


# ------------------------------------------------------------ routes
@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(HERE, "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/sw.js")
def sw():
    r = send_from_directory(HERE, "sw.js", mimetype="text/javascript")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/icon-<int:n>.png")
def icon(n):
    return send_from_directory(HERE, f"icon-{n}.png") if n in (192, 512) else ("", 404)


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/status")
def status():
    return jsonify(locked=bool(password()), authed=authed(),
                   commands={k: {"label": v["label"], "desc": v["desc"], "kind": v["kind"]} for k, v in COMMANDS.items()},
                   tags=TAGS, models=ai.describe() if authed() else {}, fake=ai.FAKE, pdf=soffice_ok())


@app.post("/api/run")
def run():
    g = guard()
    if g:
        return g
    cmd = request.form.get("cmd", "ask")
    if cmd not in COMMANDS or COMMANDS[cmd]["kind"] in ("images", "models"):
        return jsonify(error="Unknown command."), 400
    if limited():
        return jsonify(error="Too many requests. Wait a little and try again."), 429
    text = request.form.get("text", "")
    tags = [t for t in request.form.get("tags", "").split(",") if t in TAGS]
    try:
        history = json.loads(request.form.get("history", "[]"))
        history = [{"role": "assistant" if h.get("role") == "assistant" else "user", "text": str(h.get("text", ""))[:8000]} for h in history]
    except Exception:
        history = []
    prev_doc = request.form.get("prev_doc", "")[:MAX_TEXT]
    texts, ai_files = [], []
    for f in request.files.getlist("files")[:8]:
        t, af = read_file(f)
        texts.append(t)
        if af:
            ai_files.append(af)
    if texts:
        text = (text + "\n\n" + "\n\n".join(texts)).strip()
    if not text.strip() and not ai_files:
        return jsonify(error="Write something or attach a file first."), 400
    jid = new_job()
    threading.Thread(target=work, args=(jid, cmd, text, tags, history, prev_doc, ai_files), daemon=True).start()
    return jsonify(job=jid)


@app.get("/api/job/<jid>")
def job(jid):
    g = guard()
    if g:
        return g
    with LOCK:
        j = JOBS.get(jid)
        if not j:
            return jsonify(state="lost", error="This job is gone (the server restarted). Please send it again."), 404
        return jsonify(state=j["state"], steps=j["steps"][-6:], result=j["result"], error=j["error"])


@app.get("/api/images")
def images():
    g = guard()
    if g:
        return g
    q = request.args.get("q", "").strip()[:100]
    if not q:
        return jsonify(error="Type what to search for."), 400
    if ai.FAKE:
        return jsonify(items=[{"title": "Test picture", "thumb": "", "url": "https://example.org", "by": "Test", "license": "CC0"}])
    items = []
    hdr = {"User-Agent": "QoreSchoolApp/1.0 (teacher tool)"}
    try:
        r = requests.get("https://api.openverse.org/v1/images/", params={"q": q, "page_size": 12, "license_type": "commercial,modification"},
                         headers=hdr, timeout=20)
        for x in r.json().get("results", []):
            items.append({"title": x.get("title") or q, "thumb": x.get("thumbnail") or x.get("url"), "url": x.get("foreign_landing_url") or x.get("url"),
                          "full": x.get("url"), "by": x.get("creator") or "Unknown", "license": (x.get("license") or "").upper() + " " + (x.get("license_version") or "")})
    except Exception:
        pass
    if len(items) < 6:
        try:
            r = requests.get("https://commons.wikimedia.org/w/api.php", headers=hdr, timeout=20, params={
                "action": "query", "generator": "search", "gsrsearch": q + " filetype:bitmap", "gsrnamespace": 6, "gsrlimit": 12,
                "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": 400, "format": "json"})
            for p in (r.json().get("query", {}).get("pages", {}) or {}).values():
                ii = (p.get("imageinfo") or [{}])[0]
                md = ii.get("extmetadata", {})
                items.append({"title": p.get("title", "").replace("File:", ""), "thumb": ii.get("thumburl") or ii.get("url"),
                              "url": ii.get("descriptionurl"), "full": ii.get("url"),
                              "by": (md.get("Artist", {}).get("value", "Wikimedia") or "Wikimedia")[:60],
                              "license": md.get("LicenseShortName", {}).get("value", "")})
        except Exception:
            pass
    if not items:
        return jsonify(error="No pictures found. Try another word."), 404
    return jsonify(items=items[:16])


# ------------------------------------------------------------ Word -> PDF (LibreOffice)
CONV = threading.Lock()
PDFS = {}


def soffice_ok():
    return bool(shutil.which("soffice") or shutil.which("libreoffice"))


def convert_job(jid, data):
    try:
        step(jid, "Making the PDF...")
        d = tempfile.mkdtemp(prefix="qpdf")
        src = os.path.join(d, "doc.docx")
        with open(src, "wb") as f:
            f.write(data)
        exe = shutil.which("soffice") or shutil.which("libreoffice")
        with CONV:
            subprocess.run([exe, "--headless", "--norestore", "-env:UserInstallation=file:///tmp/lo_profile",
                            "--convert-to", "pdf", "--outdir", d, src], timeout=150, capture_output=True)
        pdf = os.path.join(d, "doc.pdf")
        if not os.path.exists(pdf):
            raise RuntimeError("The PDF could not be made from this file.")
        pages = 1
        try:
            out = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True, timeout=30).stdout
            pages = int(next(l for l in out.splitlines() if l.startswith("Pages:")).split()[1])
        except Exception:
            pass
        pid = uuid.uuid4().hex[:12]
        with LOCK:
            for k in [k for k, v in PDFS.items() if time.time() - v["t"] > 7200]:
                shutil.rmtree(PDFS.pop(k)["dir"], ignore_errors=True)
            PDFS[pid] = {"t": time.time(), "dir": d, "pdf": pdf, "pages": pages}
            JOBS[jid].update(state="done", result={"id": pid, "pages": pages})
    except Exception as e:      # noqa: BLE001
        with LOCK:
            JOBS[jid].update(state="error", error=str(e)[:160])


@app.post("/api/pdf")
def pdf_start():
    g = guard()
    if g:
        return g
    if not soffice_ok():
        return jsonify(error="PDF maker is not installed on this server."), 501
    if limited():
        return jsonify(error="Too many requests. Wait a little and try again."), 429
    f = request.files.get("file")
    if not f:
        return jsonify(error="No file."), 400
    jid = new_job()
    threading.Thread(target=convert_job, args=(jid, f.read()), daemon=True).start()
    return jsonify(job=jid)


@app.get("/api/pdf/<pid>")
def pdf_get(pid):
    g = guard()
    if g:
        return g
    p = PDFS.get(pid)
    if not p:
        return jsonify(error="This PDF is gone. Make it again."), 404
    with open(p["pdf"], "rb") as f:
        return Response(f.read(), mimetype="application/pdf")


@app.get("/api/pdf/<pid>/page/<int:n>")
def pdf_page(pid, n):
    g = guard()
    if g:
        return g
    p = PDFS.get(pid)
    if not p or n < 1 or n > p["pages"]:
        return jsonify(error="No such page."), 404
    base = os.path.join(p["dir"], f"pg{n}")
    if not os.path.exists(base + ".png"):
        subprocess.run(["pdftoppm", "-r", "110", "-png", "-singlefile", "-f", str(n), "-l", str(n), p["pdf"], base],
                       capture_output=True, timeout=60)
    if not os.path.exists(base + ".png"):
        return jsonify(error="Could not draw the page."), 500
    with open(base + ".png", "rb") as f:
        return Response(f.read(), mimetype="image/png")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
