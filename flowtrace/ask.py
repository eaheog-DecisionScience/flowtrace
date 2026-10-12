"""Natural-language querying over a project (`flowtrace ask`).

Follows the roadmap's own guidance for this layer: build a deterministic
evidence graph FIRST using already-tested code (nav, impact, callgraph),
then hand that evidence to an LLM to phrase as natural language. The LLM
is not trusted to invent facts about the codebase - it only narrates
facts already computed and shown to it verbatim, and the system prompt
instructs it to say so when the evidence is insufficient rather than
guess.
"""

import json
import re

from .callgraph import build_call_graph
from .impact import build_impact
from .indexer import build_index
from .navigation import build_navigation_index

EVIDENCE_SYSTEM_PROMPT = (
    "You are a code-understanding assistant for the FlowTrace tool. You will be given "
    "structured, machine-computed evidence about a Python project (source code, docstrings, "
    "static callers/callees, and impact metrics) as JSON. Answer the user's question using "
    "ONLY the evidence provided - do not invent function names, files, or behavior that "
    "isn't in the evidence. If the evidence doesn't contain enough to answer, say so plainly "
    "and suggest what to search for next. Cite qualified_name and file:line for every claim."
)


def _extract_candidate_symbols(question, nav_index, max_symbols=5):
    """Lightweight keyword extraction: pull identifier-looking tokens out
    of the question and check them against the project's known symbols,
    so evidence gathering stays relevant instead of dumping everything.

    Deliberately does NOT use find_symbol's loose substring-anywhere
    fallback tier here - that's fine for an explicit CLI lookup where the
    person typed a specific name, but far too noisy applied to every
    common English word in a free-text question ("the" matching inside
    "gather_evidence" is a real false positive this hit during testing).
    Only exact and dotted-suffix matches count as evidence here.
    """
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", question)
    found, seen = [], set()
    for tok in tokens:
        if len(tok) < 3:
            continue
        if tok in nav_index:
            matches = [tok]
        else:
            matches = [q for q in nav_index if q.endswith("." + tok)]
        for match in matches:
            if match not in seen:
                seen.add(match)
                found.append(match)
    return found[:max_symbols]


def gather_evidence(project_dir, question, extra_ignore=(), max_symbols=5):
    index_doc = build_index(project_dir, extra_ignore=extra_ignore)
    call_graph = build_call_graph(index_doc, project_dir)
    nav_index = build_navigation_index(index_doc, call_graph)

    symbols = _extract_candidate_symbols(question, nav_index, max_symbols=max_symbols)
    evidence = {"project_root": str(project_dir), "matched_symbols": symbols, "records": []}
    for sym in symbols:
        rec = dict(nav_index[sym])
        if rec.get("kind") != "class":
            rec["impact_summary"] = build_impact(index_doc, call_graph, sym, max_depth=3)["summary"]
        evidence["records"].append(rec)
    return evidence


def build_answer(project_dir, question, llm_client, extra_ignore=(), max_symbols=5, evidence_char_limit=12000):
    evidence = gather_evidence(project_dir, question, extra_ignore=extra_ignore, max_symbols=max_symbols)
    if not evidence["matched_symbols"]:
        return {
            "question": question, "evidence": evidence,
            "answer": ("No project symbols matched any identifiers in the question - "
                       "try including a specific function or class name."),
        }
    evidence_json = json.dumps(evidence, indent=2, default=str)[:evidence_char_limit]
    user_prompt = f"QUESTION: {question}\n\nEVIDENCE (JSON):\n{evidence_json}"
    answer = llm_client.chat(EVIDENCE_SYSTEM_PROMPT, user_prompt)
    return {"question": question, "evidence": evidence, "answer": answer}
