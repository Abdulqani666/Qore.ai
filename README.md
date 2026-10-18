# Qore.ai

Docx Formatter: formats Word documents in the browser without changing the text.

Hosted on Render as a static site (publish directory `.`, no build).

## AI commands (Python server)
Run `app.py` (Render web service). Type `/` for tasks and `#` to pick a model.
Tasks: /build_document /formatter /exam /answer_key /lesson /worksheet /translate /summarize /proofread /revise /ask /images /models
Tags: #claude #gpt #gemini #deep #fast #search #nosearch #noai
Set ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY and APP_PASSWORD in Render. Test without keys: `QORE_FAKE=1 python app.py`.
