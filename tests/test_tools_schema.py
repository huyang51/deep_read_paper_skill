"""TOOLS schema vs Pydantic models — a drift tripwire.

server.py's TOOLS list is hand-maintained ("must be manually kept in sync"
says the comment above it) and models.py is what actually validates params.
When they drift, one of two things happens silently:

  * a field exists only in the schema — the agent sees it, sends it, and
    pydantic's default swallows it (model_config ignores extra keys by
    default) or the tool errors;
  * a field exists only in the model — the agent is never told it exists.

This test diffs every tool's inputSchema properties against its model's
fields, both ways, plus the required set and the JSON type of each property.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server import models  # noqa: E402
from mcp_server.relations import RELATION_TYPES  # noqa: E402
from mcp_server.server import TOOLS  # noqa: E402

# tool name -> the pydantic model that validates its params
# (paper_index_stats takes no params and has no model by design)
TOOL_MODELS = {
    "paper_search": models.SearchInput,
    "paper_get": models.GetPaperInput,
    "paper_find_related": models.FindRelatedInput,
    "paper_search_by_method": models.SearchByMethodInput,
    "paper_index": models.PaperIndexInput,
    "paper_remove": models.PaperRemoveInput,
    "cite_verify": models.CiteVerifyInput,
    "paper_citations": models.PaperCitationsInput,
}

# pydantic annotation -> the JSON-schema "type" the hand-written schema claims
_JSON_TYPES = {int: "integer", str: "string", bool: "boolean", list: "array"}


def _json_type(annotation):
    if annotation in _JSON_TYPES:
        return _JSON_TYPES[annotation]
    origin = getattr(annotation, "__origin__", None)
    if origin in (list,):          # list[str], list[int], list[dict]
        return "array"
    return None                    # Optional[int] etc. — skip the type check


def _annotation(annotation):
    """Strip Optional[X] -> X so the type comparison sees the real type."""
    origin = getattr(annotation, "__origin__", None)
    if origin is type(None):
        return annotation
    if [a for a in getattr(annotation, "__args__", []) if a is type(None)]:
        args = [a for a in annotation.__args__ if a is not type(None)]
        return args[0] if len(args) == 1 else annotation
    return annotation


class SchemaDriftTest(unittest.TestCase):
    def test_every_tool_with_a_model_is_mapped(self):
        """A tool added to TOOLS without a mapping here is untested drift —
        extend TOOL_MODELS when you add it."""
        listed = {t["name"] for t in TOOLS}
        self.assertEqual(listed - {"paper_index_stats"}, set(TOOL_MODELS))

    def test_schemas_agree_with_models(self):
        by_name = {t["name"]: t for t in TOOLS}
        for tool_name, model in TOOL_MODELS.items():
            with self.subTest(tool=tool_name):
                schema = by_name[tool_name]["inputSchema"]
                props = schema.get("properties", {})
                fields = model.model_fields

                schema_only = set(props) - set(fields)
                model_only = set(fields) - set(props)
                self.assertEqual(schema_only, set(),
                                 f"{tool_name}: in schema, missing from model")
                self.assertEqual(model_only, set(),
                                 f"{tool_name}: in model, missing from schema")

                required = set(schema.get("required", []))
                model_required = {n for n, f in fields.items()
                                  if f.is_required()}
                self.assertEqual(required, model_required,
                                 f"{tool_name}: required sets disagree")

                for name, prop in props.items():
                    expected = _json_type(_annotation(fields[name].annotation))
                    if expected is not None:
                        self.assertEqual(prop.get("type"), expected,
                                         f"{tool_name}.{name}: type drift")

    def test_enum_properties_match_the_model_vocabulary(self):
        """The enum lists the agent chooses from must equal what the model
        accepts — a stale enum either blocks valid values or invites invalid
        ones that fail only at the server, one round-trip later."""
        by_name = {t["name"]: t for t in TOOLS}
        checks = [
            ("paper_search", "response_format",
             sorted(e.value for e in models.ResponseFormat)),
            ("paper_index", "read_mode", ["quick", "standard", "deep"]),
            # "" is the default and means "unset" — a JSON-schema default
            # outside its own enum makes the schema contradict itself
            ("paper_index", "novelty_level",
             ["", "incremental", "substantial", "breakthrough"]),
            # against the vocabulary table, not a hand-copied list: this one
            # was missing here entirely when the audit found it
            ("paper_find_related", "relation_type", sorted(RELATION_TYPES)),
        ]
        for tool, prop, values in checks:
            with self.subTest(tool=tool, prop=prop):
                enum = by_name[tool]["inputSchema"]["properties"][prop].get("enum")
                self.assertIsNotNone(enum, f"{tool}.{prop}: schema has no enum")
                self.assertEqual(sorted(enum), sorted(values))

    def test_schema_carries_every_model_constraint(self):
        """Anything the model enforces (ge/le/pattern) must also be visible
        to the agent in the schema — otherwise the agent pays a rejected
        round-trip for a value we could have told it up front. Compared
        against pydantic's own JSON-schema export, so adding a constraint to
        a model lights this up without re-enumerating fields by hand."""
        by_name = {t["name"]: t for t in TOOLS}
        for tool_name, model in TOOL_MODELS.items():
            gen = model.model_json_schema().get("properties", {})
            props = by_name[tool_name]["inputSchema"].get("properties", {})
            for field, gspec in gen.items():
                if field not in props:
                    continue
                with self.subTest(tool=tool_name, field=field):
                    for key in ("minimum", "maximum", "pattern"):
                        # Optional fields nest constraints in anyOf branches
                        branches = [gspec] + [g for g in gspec.get("anyOf", [])
                                              if isinstance(g, dict)]
                        for cand in branches:
                            if key in cand:
                                self.assertEqual(props[field].get(key), cand[key],
                                                 f"{tool_name}.{field}: {key} drift")
                                break


if __name__ == "__main__":
    unittest.main()
