"""Model access for Qore: Claude, ChatGPT (OpenAI) and Gemini behind one interface.

Every call goes through run_with_fallback(), which tries the model the user asked for,
then the other available providers. Keys come from environment variables only.
"""
import base64
import os
import re
import time

FAKE = os.environ.get("QORE_FAKE") == "1"   # test mode: canned answers, no network


def env(name, default=""):
    for k, v in os.environ.items():
        if k.lower() == name.lower() and v.strip():
            return v.strip()
    return default


KEYS = {
    "claude": lambda: env("ANTHROPIC_API_KEY"),
    "gpt": lambda: env("OPENAI_API_KEY"),
    "gemini": lambda: env("GEMINI_API_KEY") or env("GOOGLE_API_KEY"),
}
LABEL = {"claude": "Claude", "gpt": "ChatGPT", "gemini": "Gemini"}


def _chain(var, default):
    v = env(var)
    return [x.strip() for x in v.split(",") if x.strip()] if v else default


# Each tier is a preference list. The first model the account can use wins.
CHAINS = {
    "claude": {
        "fast": _chain("CLAUDE_FAST_MODEL", ["claude-haiku-5-5"]),
        "std": _chain("CLAUDE_STD_MODEL", ["claude-sonnet-5-5"]),
        "deep": _chain("CLAUDE_DEEP_MODEL", ["claude-opus-5-5", "claude-sonnet-5-5"]),
    },
    "gemini": {
        "fast": _chain("GEMINI_FAST_MODEL", ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]),
        "std": _chain("GEMINI_MODEL", ["gemini-3.8-flash", "gemini-3.6-flash"]),
        "deep": _chain("GEMINI_DEEP_MODEL", ["gemini-3.8-pro", "gemini-3.8-flash", "gemini-3.6-flash"]),
    },
    "gpt": {
        "fast": _chain("OPENAI_FAST_MODEL", ["gpt-5.5-mini", "gpt-5.2-mini", "gpt-5-mini", "gpt-4.1-mini"]),
        "std": _chain("OPENAI_MODEL", ["gpt-5.5", "gpt-5.2", "gpt-5", "gpt-4.1"]),
        "deep": _chain("OPENAI_DEEP_MODEL", ["gpt-5.5", "gpt-5.2", "gpt-5", "gpt-4.1"]),
    },
}


class AIError(Exception):
    pass


class Unusable(AIError):
    """The provider cannot be used at all (bad key, no access)."""


def available():
    if FAKE:
        return {"claude": True, "gpt": True, "gemini": True}
    return {p: bool(KEYS[p]()) for p in KEYS}


def _status(e):
    return getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)


def _is_missing_model(e):
    s = _status(e)
    msg = str(e).lower()
    return s == 404 or "not found" in msg and "model" in msg or "does not exist" in msg and "model" in msg


def _is_retryable(e):
    s = _status(e)
    msg = str(e).lower()
    return s in (429, 500, 502, 503, 504, 529) or "overloaded" in msg or "unavailable" in msg or "timeout" in msg


BILLING = {}


def _is_billing(e):
    m = str(e).lower()
    return ("credit balance" in m or "no credits" in m or "insufficient_quota" in m or "exceeded your current quota" in m
            or "billing" in m and ("limit" in m or "hard" in m) or "resource_exhausted" in m and "quota" in m)


def _billing_text(provider):
    return {"claude": "has no credit left. Add credit at console.anthropic.com > Plans and Billing.",
            "gpt": "has no credit left. Add credit at platform.openai.com > Billing.",
            "gemini": "has no quota left. Check billing at aistudio.google.com."}.get(provider, "has no credit left.")


def _is_auth(e):
    s = _status(e)
    return s in (401, 403)


# ---------------------------------------------------------------- Claude
def _claude(model, system, messages, files, thinking, max_tokens, timeout=240):
    import anthropic
    client = anthropic.Anthropic(api_key=KEYS["claude"](), timeout=timeout, max_retries=0)
    msgs = []
    for i, m in enumerate(messages):
        content = [{"type": "text", "text": m["text"] or "(empty)"}]
        if i == len(messages) - 1 and m["role"] == "user":
            for f in files:
                data = base64.b64encode(f["data"]).decode()
                if f["kind"] == "image":
                    content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": f["mime"], "data": data}})
                elif f["kind"] == "pdf":
                    content.insert(0, {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}})
        msgs.append({"role": m["role"], "content": content})
    kw = dict(model=model, max_tokens=max_tokens, system=system, messages=msgs)
    if thinking:
        budget = min(8000, max_tokens // 2)
        kw["max_tokens"] = max_tokens + budget
        kw["thinking"] = {"type": "enabled", "budget_tokens": budget}
    try:
        with client.messages.stream(**kw) as s:
            msg = s.get_final_message()
    except anthropic.BadRequestError as e:
        if thinking and "thinking" in str(e).lower():       # this model does not take that setting
            kw.pop("thinking"); kw["max_tokens"] = max_tokens
            with client.messages.stream(**kw) as s:
                msg = s.get_final_message()
        else:
            raise
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text"), []


# ---------------------------------------------------------------- Gemini
def _gemini(model, system, messages, files, thinking, max_tokens, grounded=False, timeout=240):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=KEYS["gemini"](), http_options=types.HttpOptions(timeout=int(timeout * 1000)))
    contents = []
    for i, m in enumerate(messages):
        parts = [types.Part.from_text(text=m["text"] or "(empty)")]
        if i == len(messages) - 1 and m["role"] == "user":
            for f in files:
                parts.insert(0, types.Part.from_bytes(data=f["data"], mime_type=f["mime"]))
        contents.append(types.Content(role="user" if m["role"] == "user" else "model", parts=parts))
    cfg = dict(system_instruction=system, max_output_tokens=max_tokens)
    if grounded:
        cfg["tools"] = [types.Tool(google_search=types.GoogleSearch())]
    resp = client.models.generate_content(model=model, contents=contents, config=types.GenerateContentConfig(**cfg))
    text = resp.text or ""
    sources = []
    try:
        gm = resp.candidates[0].grounding_metadata
        for ch in (gm.grounding_chunks or []):
            if ch.web and ch.web.uri:
                sources.append({"title": ch.web.title or ch.web.uri, "url": ch.web.uri})
    except Exception:
        pass
    return text, sources


# ---------------------------------------------------------------- OpenAI
def _gpt(model, system, messages, files, thinking, max_tokens, timeout=240):
    from openai import OpenAI
    client = OpenAI(api_key=KEYS["gpt"](), timeout=timeout, max_retries=0)
    msgs = [{"role": "system", "content": system}]
    for i, m in enumerate(messages):
        if i == len(messages) - 1 and m["role"] == "user" and any(f["kind"] == "image" for f in files):
            content = [{"type": "text", "text": m["text"] or "(empty)"}]
            for f in files:
                if f["kind"] == "image":
                    content.append({"type": "image_url", "image_url": {"url": f"data:{f['mime']};base64," + base64.b64encode(f["data"]).decode()}})
            msgs.append({"role": "user", "content": content})
        else:
            msgs.append({"role": m["role"], "content": m["text"] or "(empty)"})
    try:
        r = client.chat.completions.create(model=model, messages=msgs, max_completion_tokens=max_tokens)
    except Exception as e:
        if "max_completion_tokens" in str(e):
            r = client.chat.completions.create(model=model, messages=msgs, max_tokens=max_tokens)
        else:
            raise
    return r.choices[0].message.content or "", []


# ---------------------------------------------------------------- model discovery
_cache = {}


def _listed(provider):
    """Set of model ids this key can use, or None if it cannot be listed."""
    hit = _cache.get(provider)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    ids = None
    try:
        if provider == "claude":
            import anthropic
            ids = {m.id for m in anthropic.Anthropic(api_key=KEYS["claude"](), max_retries=0, timeout=20).models.list(limit=100).data}
        elif provider == "gpt":
            from openai import OpenAI
            ids = {m.id for m in OpenAI(api_key=KEYS["gpt"](), max_retries=0, timeout=20).models.list().data}
        elif provider == "gemini":
            from google import genai
            ids = {m.name.split("/")[-1] for m in genai.Client(api_key=KEYS["gemini"]()).models.list()}
    except Exception as e:
        if _is_auth(e):
            ids = set()
    _cache[provider] = (time.time(), ids)
    return ids


def pick_models(provider, tier):
    chain = CHAINS[provider][tier]
    ids = _listed(provider)
    if ids:
        ok = [m for m in chain if m in ids]
        if ok:
            return ok
        found = _family_pick(provider, tier, ids)
        if found:
            return found + chain
    return chain


def _vkey(name):
    return [int(x) for x in re.findall(r"\d+", re.sub(r"\d{8}|latest", "", name))] or [0]


def _family_pick(provider, tier, ids):
    """The names in CHAINS were not found on this account, so choose the newest model of the right kind."""
    bad = ("image", "tts", "embed", "live", "audio", "vision", "realtime", "transcribe", "search", "moderation",
           "instruct", "codex", "robotics", "computer", "customtools", "dall", "whisper", "sora")
    pool = [m for m in ids if not any(b in m for b in bad)]
    if provider == "claude":
        word = {"fast": "haiku", "std": "sonnet", "deep": "opus"}[tier]
        pool = [m for m in pool if word in m] or [m for m in pool if "sonnet" in m]
    elif provider == "gemini":
        pool = [m for m in pool if m.startswith("gemini")]
        if tier == "deep":
            pool = [m for m in pool if "pro" in m] or [m for m in pool if "flash" in m]
        elif tier == "fast":
            pool = [m for m in pool if "lite" in m] or [m for m in pool if "flash" in m]
        else:
            pool = [m for m in pool if "flash" in m and "lite" not in m] or [m for m in pool if "flash" in m]
    else:
        pool = [m for m in pool if m.startswith("gpt-")]
        small = [m for m in pool if "mini" in m or "nano" in m]
        big = [m for m in pool if m not in small]
        pool = (small if tier == "fast" else big) or pool
    pool.sort(key=lambda m: (_vkey(m), -len(m)), reverse=True)
    return pool[:3]


def describe():
    out = {}
    for p, on in available().items():
        out[p] = {"label": LABEL[p], "on": on,
                  "models": {t: (pick_models(p, t)[0] if on and not FAKE else CHAINS[p][t][0]) for t in ("fast", "std", "deep")}}
    return out


# ---------------------------------------------------------------- one call, with retries and fallbacks
def call(provider, tier, system, messages, files=(), thinking=False, max_tokens=8000, grounded=False, say=None):
    """Returns (text, model_used, sources). Raises Unusable or AIError."""
    if FAKE:
        from fake import fake_reply
        time.sleep(0.3)
        return fake_reply(system, messages), f"{provider}-{tier}-test", ([{"title": "Example source", "url": "https://example.org"}] if grounded else [])
    last = None
    for model in pick_models(provider, tier):
        for attempt in (1, 2):
            try:
                to = 45 if tier == "fast" else (300 if max_tokens > 8000 or thinking else 120)
                if provider == "claude":
                    t, s = _claude(model, system, messages, files, thinking, max_tokens, to)
                elif provider == "gemini":
                    t, s = _gemini(model, system, messages, files, thinking, max_tokens, grounded, to)
                else:
                    t, s = _gpt(model, system, messages, files, thinking, max_tokens, to)
                if not t.strip():
                    raise AIError("The model returned an empty answer.")
                return t, model, s
            except Exception as e:                       # noqa: BLE001
                last = e
                if _is_billing(e):
                    BILLING[provider] = time.time() + 3600
                    raise Unusable(f"{LABEL[provider]} {_billing_text(provider)}")
                if _is_auth(e):
                    raise Unusable(f"{LABEL[provider]} rejected the key. Check it in Render settings.")
                if _is_missing_model(e):
                    break                                # try the next model in the chain
                if _is_retryable(e) and attempt == 1:
                    time.sleep(3)
                    continue
                if _is_retryable(e):
                    break
                raise AIError(f"{LABEL[provider]} could not answer: {str(e)[:200]}")
    raise AIError(f"{LABEL[provider]} could not run: {str(last)[:160] if last else 'no usable model'}")


DOWN = {}          # provider -> time until it is skipped (after a failure)


def mark_down(provider, seconds=300):
    DOWN[provider] = time.time() + seconds


def order_for(route, forced=None):
    """route: list of (provider, tier). forced: provider name from a #tag, or None.
    Returns attempts in order; a forced provider goes first, the rest are fallbacks."""
    on = available()
    attempts = [(p, t) for p, t in route if on.get(p)]
    healthy = [x for x in attempts if DOWN.get(x[0], 0) < time.time()]
    if healthy:
        attempts = healthy + [x for x in attempts if x not in healthy] if not forced else attempts
    if forced:
        tier = next((t for p, t in route if p == forced), route[0][1])
        attempts = [(forced, tier)] + [a for a in attempts if a[0] != forced]
    return attempts


def run_with_fallback(route, system, messages, files=(), thinking=False, max_tokens=8000,
                      grounded_ok=False, forced=None, tier_override=None, say=None):
    """Try each provider in order. Returns dict(text, model, provider, sources, note)."""
    on = available()
    if forced and not on.get(forced):
        raise AIError(f"#{forced} is not set up yet. Add its API key in Render, then try again.")
    attempts = order_for(route, forced)
    if not attempts:
        raise AIError("No AI key is set. Add ANTHROPIC_API_KEY, OPENAI_API_KEY or GEMINI_API_KEY in Render.")
    notes, last = [], None
    for provider, tier in attempts:
        tier = tier_override or tier
        if say:
            say(f"Thinking with {LABEL[provider]}...")
        try:
            text, model, sources = call(provider, tier, system, messages, files, thinking, max_tokens, say=say)
            return {"text": text, "model": model, "provider": provider, "sources": sources, "note": " ".join(notes)}
        except AIError as e:
            last = e
            mark_down(provider, 3600 if BILLING.get(provider, 0) > time.time() else 300)
            why = str(e).replace(LABEL[provider], "").strip(" :.")[:110]
            notes.append(f"{LABEL[provider]} failed ({why}).")
    raise AIError(str(last) if last else "No model could answer.")


def research(query, say=None):
    """Web research with Gemini + Google Search grounding. Returns (notes, sources) or ('', [])."""
    if not available().get("gemini"):
        return "", []
    if say:
        say("Searching the web with Gemini...")
    sys = ("You are a careful researcher. Search the web and write short factual notes in bullet points. "
           "Keep numbers, dates and names exact. Do not write the final document. Do not invent anything.")
    try:
        text, _model, sources = call("gemini", "std", sys, [{"role": "user", "text": query}], grounded=True, max_tokens=4000)
        seen, uniq = set(), []
        for s in sources:
            if s["url"] not in seen:
                seen.add(s["url"]); uniq.append(s)
        return text, uniq[:8]
    except AIError:
        return "", []


def extract_json(text):
    """Pull the first JSON object or array out of a model answer."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    pairs = [("{", "}"), ("[", "]")]
    pairs.sort(key=lambda p: t.find(p[0]) if t.find(p[0]) != -1 else 10**9)   # whichever starts first
    for opener, closer in pairs:
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j > i:
            try:
                import json
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    return None
