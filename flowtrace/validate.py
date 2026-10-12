#!/usr/bin/env python3
"""Validate a flowtrace JSON file (trace or index) against its schema.

Usage:
    python validate.py path/to/trace.json
    python validate.py path/to/index.json
    python validate.py path/to/file.json --kind trace|index   # force, skip auto-detect
"""
import argparse
import json
import sys
from pathlib import Path

try:
    import jsonschema
except ImportError:
    print("error: pip install jsonschema", file=sys.stderr)
    sys.exit(2)

SCHEMA_DIR = Path(__file__).parent
SCHEMAS = {
    "trace": SCHEMA_DIR / "flowtrace-trace.schema.json",
    "index": SCHEMA_DIR / "flowtrace-index.schema.json",
}


def detect_kind(doc):
    if "events" in doc and "event_count" in doc:
        return "trace"
    if "files" in doc and "folder_tree" in doc:
        return "index"
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--kind", choices=["trace", "index"], default=None)
    args = parser.parse_args()

    doc_path = Path(args.path)
    doc = json.loads(doc_path.read_text())

    kind = args.kind or detect_kind(doc)
    if kind is None:
        print(f"error: couldn't auto-detect whether {doc_path} is a trace or an index; pass --kind", file=sys.stderr)
        sys.exit(2)

    schema = json.loads(SCHEMAS[kind].read_text())
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))

    if not errors:
        print(f"VALID ({kind}): {doc_path} conforms to schema_version {doc.get('schema_version')}")
        if kind == "trace":
            print(f"  {doc.get('event_count')} events checked")
        else:
            print(f"  {doc.get('file_count')} files, {doc.get('class_count')} classes, {doc.get('function_count')} functions checked")
        sys.exit(0)

    print(f"INVALID ({kind}): {doc_path} has {len(errors)} error(s):")
    for e in errors:
        loc = "/".join(str(p) for p in e.path) or "<root>"
        print(f"  at {loc}: {e.message}")
    sys.exit(1)


if __name__ == "__main__":
    main()

