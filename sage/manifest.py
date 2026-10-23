"""What a device may be asked to do: its manifest, shaped like MCP `tools/list`, and checks against it."""

import json
import re

NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
RESERVED = {"reply", "reply_delta", "stop"}
MAX_TOOLS = 64
# Senses a body may have; any it does not declare is stated as missing, so the model never claims it.
SENSES = {"vision": "see", "speech": "hear", "touch": "feel touch"}
MAX_REPLY = 160  # characters: one or two short spoken sentences, enforced by the decoder
MAX_FACT = 120  # characters of one remembered fact
# The kernel's own answer fields, after the device's tools; a device tool of the same name is the device's.
MEMORY = {"remember": {"type": "string", "maxLength": MAX_FACT}, "forget": {"type": "string", "maxLength": MAX_FACT}}
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_manifest(manifest) -> dict:
    """The tools a device declared, as declared (issuing its token was the owner's consent); ValueError if malformed."""
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object with senses and tools")
    senses, tools = manifest.get("senses", []), manifest.get("tools", [])
    if not isinstance(senses, list) or not all(isinstance(sense, str) for sense in senses):
        raise ValueError("manifest.senses must be a list of names")
    if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
        raise ValueError(f"manifest.tools must be a list of at most {MAX_TOOLS} tools")
    names = set()
    for tool in tools:
        name = tool.get("name") if isinstance(tool, dict) else None
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in RESERVED or name in names:
            raise ValueError("Each tool needs a unique name matching [a-z][a-z0-9_]{0,63}, "
                                     "other than reply, reply_delta and stop")
        names.add(name)
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError(f"Tool {name}: inputSchema must be a JSON Schema with type object")
        if not isinstance(tool.get("description", ""), str):
            raise ValueError(f"Tool {name}: description must be text")
        if "max_s" in tool and not (_number(tool["max_s"]) and tool["max_s"] >= 0):
            raise ValueError(f"Tool {name}: max_s must be a number of seconds ≥ 0")
        if not isinstance(tool.get("interruptible", False), bool):
            raise ValueError(f"Tool {name}: interruptible must be true or false")
    return {"senses": tuple(senses),
            "tools": tuple({key: tool[key] for key in ("name", "description", "inputSchema", "max_s", "interruptible")
                            if key in tool} for tool in tools)}


def format_schema(tools) -> dict:
    """The Ollama `format` the model answers in: the reply first, one optional property per tool, then MEMORY."""
    own = {tool["name"]: tool["inputSchema"] for tool in tools}
    return {"type": "object",
            "properties": {"reply": {"type": "string", "maxLength": MAX_REPLY}, **own,
                           **{name: schema for name, schema in MEMORY.items() if name not in own}},
            "required": ["reply"], "additionalProperties": False}


def describe(device: str, manifest: dict) -> str:
    """The body a turn comes from, for the newest message: its senses and its tools as declared."""
    tools = "; ".join(f"{tool['name']}: {tool.get('description') or 'no description'} "
                      f"Input: {json.dumps(tool['inputSchema'])}" for tool in manifest["tools"]) or "none"
    missing = [verb for sense, verb in SENSES.items() if sense not in manifest["senses"]]
    cannot = (f" This body cannot {', '.join(missing[:-1])}{' or ' if len(missing) > 1 else ''}{missing[-1]}; "
              "if asked, say so." if missing else "")
    return (f'[Body "{device}". Senses: {", ".join(manifest["senses"]) or "none"}.{cannot} '
            f"Tools, to add to your answer only when one helps: {tools}.]")


def labels(tools) -> tuple[str, ...]:
    """Declared names that are not words (they hold a digit or an underscore): never to be said aloud."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "enum" and isinstance(value, list):
                    found.extend(v for v in value if isinstance(v, str) and re.search(r"[\d_]", v))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk([tool["inputSchema"] for tool in tools])
    return tuple(dict.fromkeys(found))


def check(schema, value, where="input") -> str:
    """What is wrong with `value` under `schema`, or "" when nothing is.

    ponytail: a JSON Schema subset (type, enum, required, properties, additionalProperties: false,
    items, minItems, maxItems); other keywords are ignored. Add them when a device declares one that matters.
    """
    if not isinstance(schema, dict):
        return ""
    kind = schema.get("type")
    if kind in ("number", "integer"):
        fits = _number(value) and (kind == "number" or float(value).is_integer())
    else:
        fits = not isinstance(kind, str) or kind not in TYPES or isinstance(value, TYPES[kind])
    if not fits:
        return f"{where} must be of type {kind}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{where} must be one of {json.dumps(schema['enum'])}"
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                return f"{where}.{key} is required"
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                if problem := check(properties[key], item, f"{where}.{key}"):
                    return problem
            elif schema.get("additionalProperties") is False:
                return f"{where}.{key} is not allowed"
    if isinstance(value, list):
        if _number(schema.get("maxItems")) and len(value) > schema["maxItems"]:
            return f"{where} must have at most {schema['maxItems']} item{'s' * (schema['maxItems'] != 1)}"
        if _number(schema.get("minItems")) and len(value) < schema["minItems"]:
            return f"{where} must have at least {schema['minItems']} item{'s' * (schema['minItems'] != 1)}"
        for index, item in enumerate(value):
            if problem := check(schema.get("items"), item, f"{where}[{index}]"):
                return problem
    return ""


def authorize(name: str, value, tools) -> str:
    """"" for a call the turn's device declared with valid input; else why it was not done."""
    tool = next((tool for tool in tools if tool["name"] == name), None)
    if tool is None:
        return "unavailable, it has no such tool"
    if problem := check(tool["inputSchema"], value):
        return f"invalid input, {problem}"
    return ""
