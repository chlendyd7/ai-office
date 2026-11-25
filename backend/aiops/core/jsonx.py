"""Pull a JSON object out of an LLM reply: bare JSON, ```json fences, or JSON surrounded by prose."""
from __future__ import annotations

import json
import re


def extract_json(raw: str):
    txt = raw.strip()
    try:
        return json.loads(txt)
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)```", txt, re.S)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except ValueError:
            pass
    start = txt.find("{")
    while start != -1:  # first balanced {...} that parses
        depth, in_str, esc = 0, False, False
        for i in range(start, len(txt)):
            ch = txt[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(txt[start : i + 1])
                    except ValueError:
                        break
        start = txt.find("{", start + 1)
    raise ValueError(f"no JSON object found in response: {txt[:200]!r}")
