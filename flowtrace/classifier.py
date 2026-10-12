"""Auto-classifies modules into architectural categories based on import
namespace, per the Data Processing Engine spec (e.g. [Network], [Database],
[UI], [Logic]).
"""

NAMESPACE_RULES = [
    (
        ("requests", "urllib", "urllib3", "http", "httpx", "aiohttp",
         "socket", "ftplib", "smtplib", "websockets", "grpc"),
        "Network",
    ),
    (
        ("sqlite3", "psycopg2", "pymysql", "mysql", "sqlalchemy",
         "pymongo", "redis", "django.db", "peewee", "asyncpg"),
        "Database",
    ),
    (
        ("flask", "django", "fastapi", "tkinter", "PyQt5", "PyQt6",
         "PySide2", "PySide6", "streamlit", "jinja2", "kivy", "wx"),
        "UI",
    ),
    (
        ("os", "sys", "json", "re", "itertools", "functools", "collections",
         "math", "typing", "logging", "pathlib", "datetime", "time",
         "abc", "enum", "copy", "io", "inspect", "ast", "argparse", "runpy",
         "threading", "subprocess", "shutil", "tempfile", "glob", "importlib",
         "dataclasses", "contextlib", "warnings", "traceback", "csv", "random",
         "string", "textwrap", "uuid", "hashlib", "base64"),
        "Stdlib",
    ),
]


def classify_module(module_name: str) -> str:
    """Return an architectural category string for a given module name."""
    if not module_name:
        return "Logic"
    top = module_name.split(".")[0]
    for prefixes, category in NAMESPACE_RULES:
        for prefix in prefixes:
            prefix_top = prefix.split(".")[0]
            if module_name == prefix or module_name.startswith(prefix + "."):
                return category
            if top == prefix_top and prefix == prefix_top:
                return category
    return "Logic"
