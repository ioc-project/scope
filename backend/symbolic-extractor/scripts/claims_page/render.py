"""Fill the page template (``template.html``: the HTML, CSS and JavaScript of the page) with the payload."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TEMPLATE_PATH = Path(__file__).resolve().parent / "template.html"


def render_html(payload: dict[str, Any]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    session = str(payload.get("session_id") or "").replace("<", "&lt;")
    return template.replace("__DATA__", data).replace("__SESSION__", session)
