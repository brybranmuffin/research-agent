"""Tools for all agents, the deterministic verification gate, and the structured LLM call.

Component: Tool calls + verification (Tier 1); Context management (bounded, paged reads).

Search tools (search agent)
- fts_search(query, k)              BM25 full-text search over corpus passages (no reference lists)
- list_docs(source_kind)            document metadata
Read tools (extract agent)
- read_pdf(doc_id, start, end)      bounded page window (<= 3 pages, capped characters per page)
- read_html(doc_id, start, end)     bounded section window
Verification (deterministic, no LLM)
- verify_quote / record_claim       exact -> fuzzy (>= 90) -> neighbouring page -> rejected;
                                    record_claim is the ONLY way a claim reaches state.db
- compute_verdict / final_verdict   supported | contested | thin floor; the LLM may only downgrade
Structured LLM call
- call_structured                   rate-limit token -> forced tool call -> Pydantic validation ->
                                    one repair attempt -> error classified transient | deterministic

LangChain: tools are @tool functions with an injected `ctx` (hidden from the model's schema).
Pipelines call tool.invoke(...), which validates arguments and fires the TraceHandler callbacks.
Search/read tools return langchain_core Document objects. Structured calls use
model.bind_tools([Schema], tool_choice=Schema) and validate the returned tool call.
"""
from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Optional

from langchain_core.documents import Document
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import InjectedToolArg, tool
from pydantic import BaseModel, ValidationError
from rapidfuzz import fuzz

import config
from agent import store
from agent.store import DeterministicError, TransientError, normalize

STOPWORDS = {"the", "and", "for", "with", "that", "this", "from", "was", "were", "are", "its", "into", "than",
             "how", "what", "which", "does", "did", "has", "have", "had", "not", "but", "about", "their", "they"}
VERDICT_STRENGTH = {"thin": 0, "contested": 1, "supported": 2}


# --------------------------------------------------------------------------- shared helpers

def run_config(ctx: store.Ctx, step: str) -> dict:
    """RunnableConfig for every invoke: the trace callback plus attribution metadata."""
    return {"callbacks": [ctx.tracer], "metadata": {"task_id": ctx.task_id, "step": step}, "run_name": step}


def chaos_hit(ctx: Optional[store.Ctx], site: str) -> bool:
    """Seeded fault injection. Deterministic per (seed, task, attempt, site, call number)."""
    if ctx is None or not ctx.cfg.chaos or ctx.task_id is None:
        return False
    rate = {"tool": ctx.cfg.chaos_tool_error_rate, "malformed_llm": ctx.cfg.chaos_malformed_rate}[site]
    ctx.chaos_counter += 1
    rng = random.Random(f"{ctx.cfg.chaos_seed}:{ctx.task_id}:{ctx.attempt}:{site}:{ctx.chaos_counter}")
    if rng.random() >= rate:
        return False
    store.log_event(ctx.conn, ctx.actor, "chaos", task_id=ctx.task_id, fault=site)
    return True


def get_doc(conn, doc_id: str):
    return conn.execute("SELECT * FROM corpus.documents WHERE doc_id = ?", (doc_id,)).fetchone()


def describe_tools(tools: list) -> str:
    """Render tool names, arguments and descriptions for the prompt's tool-definition section."""
    if not tools:
        return "(none: this step reasons only over the memory provided)"
    lines = []
    for t in tools:
        props = t.tool_call_schema.model_json_schema().get("properties", {})
        args = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in props.items())
        lines.append(f"- {t.name}({args}): {t.description.strip()}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- search tools

def _fts_query(query: str) -> str:
    tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9]+", query)]
    tokens = [t for t in dict.fromkeys(tokens) if len(t) >= 3 and t not in STOPWORDS][:12]
    return " OR ".join(f'"{t}"' for t in tokens)


@tool
def fts_search(query: str, k: int = 10, ctx: Annotated[Any, InjectedToolArg] = None) -> list[Document]:
    """Full-text search (BM25 with stemming) over corpus passages, excluding reference lists.
    Use short keyword queries made of distinctive technical terms. Returns passages with
    doc_id, unit (page or section number) and a snippet."""
    if chaos_hit(ctx, "tool"):
        raise TransientError("injected fault: fts_search unavailable")
    q = _fts_query(query)
    if not q:
        return []
    rows = ctx.conn.execute(
        """SELECT c.chunk_id, c.doc_id, c.unit, d.title, d.year, d.source_kind,
                  snippet(chunks_fts, 0, '', '', ' ... ', 32) AS snip, bm25(chunks_fts) AS score
           FROM corpus.chunks_fts JOIN corpus.chunks c ON c.chunk_id = chunks_fts.rowid
           JOIN corpus.documents d ON d.doc_id = c.doc_id
           WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?""", (q, max(1, min(k, 25)))).fetchall()
    return [Document(page_content=r["snip"], metadata={
        "doc_id": r["doc_id"], "unit": r["unit"], "chunk_id": r["chunk_id"], "title": r["title"],
        "year": r["year"], "source_kind": r["source_kind"], "score": round(-r["score"], 3)}) for r in rows]


@tool
def list_docs(source_kind: str = "", ctx: Annotated[Any, InjectedToolArg] = None) -> list[Document]:
    """List corpus documents (doc_id, type, year, venue, title). Optional filter:
    source_kind = 'primary' (research papers) or 'secondary' (web pages)."""
    sql = "SELECT doc_id, type, year, venue, title, source_kind, n_units FROM corpus.documents"
    params: tuple = ()
    if source_kind:
        sql += " WHERE source_kind = ?"
        params = (source_kind,)
    rows = ctx.conn.execute(sql + " ORDER BY doc_id", params).fetchall()
    return [Document(page_content=r["title"] or "", metadata=dict(r)) for r in rows]


# --------------------------------------------------------------------------- read tools

def _read_units(ctx: store.Ctx, doc_id: str, start: int, end: int, expected: str) -> list[Document]:
    if chaos_hit(ctx, "tool"):
        raise TransientError(f"injected fault: read_{expected} unavailable")
    doc = get_doc(ctx.conn, doc_id)
    if doc is None:
        return [Document(page_content="", metadata={"error": f"unknown doc_id {doc_id!r}"})]
    if doc["type"] != expected:
        return [Document(page_content="", metadata={"error": f"{doc_id} is {doc['type']}; use read_{doc['type']}"})]
    start = max(1, start)
    end = min(doc["n_units"], end, start + ctx.cfg.extract_window_units - 1)
    labels = json.loads(doc["unit_labels_json"]) if doc["unit_labels_json"] else None
    out = []
    for unit in range(start, end + 1):
        rows = ctx.conn.execute(
            "SELECT text, is_ref FROM corpus.chunks WHERE doc_id = ? AND unit = ? ORDER BY ord",
            (doc_id, unit)).fetchall()
        body = " ".join(r["text"] for r in rows if not r["is_ref"])
        cap = ctx.cfg.extract_max_chars_per_unit
        out.append(Document(page_content=body[:cap], metadata={
            "doc_id": doc_id, "unit": unit, "label": labels[unit - 1] if labels else f"page {unit}",
            "truncated": len(body) > cap, "reference_list_omitted": any(r["is_ref"] for r in rows)}))
    return out


@tool
def read_pdf(doc_id: str, start: int, end: int, ctx: Annotated[Any, InjectedToolArg] = None) -> list[Document]:
    """Read pages start..end (inclusive, at most 3) of a PDF document. Reference lists are omitted."""
    return _read_units(ctx, doc_id, start, end, "pdf")


@tool
def read_html(doc_id: str, start: int, end: int, ctx: Annotated[Any, InjectedToolArg] = None) -> list[Document]:
    """Read sections start..end (inclusive, at most 3) of a web page."""
    return _read_units(ctx, doc_id, start, end, "html")


def page_windows(hit_units: list[int], n_units: int, cfg: config.Config) -> list[tuple[int, int]]:
    """<= max_windows windows of window_units, centred on the most-hit units, non-overlapping."""
    windows: list[tuple[int, int]] = []
    half = cfg.extract_window_units // 2
    for unit in hit_units or [1]:
        if len(windows) >= cfg.extract_max_windows:
            break
        if any(a <= unit <= b for a, b in windows):
            continue
        a = max(1, unit - half)
        b = min(n_units, a + cfg.extract_window_units - 1)
        a = max(1, b - cfg.extract_window_units + 1)
        for x, y in windows:  # shift away from windows already chosen
            if a <= y and b >= x:
                if unit > y:
                    a, b = y + 1, min(n_units, y + cfg.extract_window_units)
                else:
                    a, b = max(1, x - cfg.extract_window_units), x - 1
        if a <= b:
            windows.append((a, b))
    return sorted(windows)


# --------------------------------------------------------------------------- verification gate

@dataclass
class Verification:
    verified: bool
    method: str            # exact | fuzzy | neighbor | rejected
    score: float
    unit: Optional[int]
    chunk_id: Optional[int]
    reason: str = ""


def _unit_chunks(conn, doc_id: str, unit: int):
    return conn.execute("SELECT chunk_id, norm_text FROM corpus.chunks WHERE doc_id = ? AND unit = ? ORDER BY ord",
                        (doc_id, unit)).fetchall()


def verify_quote(conn, quote: str, doc_id: str, unit: Optional[int], cfg: config.Config) -> Verification:
    """Is `quote` really in the cited page (or a neighbouring one)? Deterministic, no LLM."""
    q = normalize(quote).strip(" \"'")
    if "..." in q:
        return Verification(False, "rejected", 0, unit, None, "quote is not contiguous (contains an ellipsis)")
    if len(q.split()) < cfg.quote_min_words:
        return Verification(False, "rejected", 0, unit, None, f"quote shorter than {cfg.quote_min_words} words")
    doc = get_doc(conn, doc_id)
    if doc is None:
        return Verification(False, "rejected", 0, unit, None, f"unknown document {doc_id}")
    if unit is None or not 1 <= unit <= doc["n_units"]:
        return Verification(False, "rejected", 0, unit, None, f"cited location {unit} does not exist")

    order = [unit] + [u for u in (unit - 1, unit + 1) if 1 <= u <= doc["n_units"]]
    pages = {u: _unit_chunks(conn, doc_id, u) for u in order}
    best = (0.0, None)
    for u in order:  # exact match: cited page first, then neighbours
        text = " ".join(r["norm_text"] for r in pages[u])
        if q in text:
            chunk = max(pages[u], key=lambda r: fuzz.partial_ratio(q, r["norm_text"]))
            return Verification(True, "exact" if u == unit else "neighbor", 100.0, u, chunk["chunk_id"])
        if len(text) < len(q):  # a page shorter than the quote cannot contain it
            continue
        score = fuzz.partial_ratio(q, text)
        if score > best[0]:
            best = (score, u)
    score, u = best
    if u is not None and score >= cfg.fuzzy_threshold:
        chunk = max(pages[u], key=lambda r: fuzz.partial_ratio(q, r["norm_text"]))
        return Verification(True, "fuzzy" if u == unit else "neighbor", round(score, 1), u, chunk["chunk_id"])
    return Verification(False, "rejected", round(score, 1), unit, None,
                        f"best match {score:.0f} < {cfg.fuzzy_threshold:.0f} on page {unit} and neighbours")


def record_claim(conn, cfg: config.Config, *, actor: str, task_id: int, subq_id: int, doc_id: str,
                 text: str, quote: str, unit: Optional[int], stance: str) -> tuple[int, Verification]:
    """The verification gate: verify the quote, then store the claim (verified or rejected)."""
    v = verify_quote(conn, quote, doc_id, unit, cfg)
    cur = conn.execute(
        "INSERT INTO claims (subq_id, doc_id, chunk_id, unit, text, quote, stance, verified, verify_score, "
        "verify_method, reject_reason, task_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (subq_id, doc_id, v.chunk_id, v.unit, text, quote, stance, int(v.verified), v.score, v.method,
         v.reason or None, task_id, time.time()))
    if not v.verified:
        store.log_event(conn, actor, "verify_reject", task_id=task_id, subq_id=subq_id, doc_id=doc_id,
                        reason=v.reason, quote=quote[:160])
    return cur.lastrowid, v


def compute_verdict(conn, subq_id: int) -> dict:
    """Deterministic verdict floor from verified claims.
    contested: verified claims for >= 2 different hypotheses (outranks supported)
    supported: >= 2 independent primary sources (distinct first authors), no opposing claim
    thin:      otherwise. Secondary sources (web pages) never count toward independence."""
    rows = conn.execute(
        "SELECT c.stance, c.doc_id, d.first_author, d.source_kind FROM claims c "
        "JOIN corpus.documents d ON d.doc_id = c.doc_id WHERE c.subq_id = ? AND c.verified = 1",
        (subq_id,)).fetchall()
    stances = sorted({r["stance"] for r in rows if r["stance"] != "neutral"})
    independent = sorted({r["first_author"] for r in rows if r["source_kind"] == "primary"})
    by_stance = {s: sorted({r["doc_id"] for r in rows if r["stance"] == s})
                 for s in sorted({r["stance"] for r in rows})}
    if len(stances) >= 2:
        verdict = "contested"
    elif len(independent) >= 2:
        verdict = "supported"
    else:
        verdict = "thin"
    return {"verdict": verdict, "n_verified": len(rows), "independent_primary_sources": independent,
            "docs_by_stance": by_stance}


def final_verdict(floor: str, proposed: Optional[str]) -> str:
    """The LLM may only move a verdict toward caution (supported -> contested -> thin)."""
    if proposed in VERDICT_STRENGTH and VERDICT_STRENGTH[proposed] < VERDICT_STRENGTH[floor]:
        return proposed
    return floor


CITATION = re.compile(r"\[C(\d+)\]")
GROUPED_CITATION = re.compile(r"\[\s*(C\d+(?:\s*[,;]\s*C\d+)+)\s*\]")


def split_grouped_citations(markdown: str) -> str:
    """'[C12, C15]' -> '[C12][C15]' so every marker is validated and rendered individually."""
    return GROUPED_CITATION.sub(lambda m: "".join(f"[{x.strip()}]" for x in re.split(r"[,;]", m.group(1))), markdown)


# --------------------------------------------------------------------------- structured LLM call

def _classify(err: Exception) -> Exception:
    text = f"{type(err).__name__}: {err}"
    transient = ("429", "Too Many", "500", "502", "503", "504", "Timeout", "timed out", "Connection",
                 "temporarily", "RemoteDisconnected", "ChunkedEncodingError")
    if any(t in text for t in transient):
        return TransientError(text[:400])
    return DeterministicError(text[:400])


def _tool_args(ai: Any, name: str) -> Optional[dict]:
    for call in getattr(ai, "tool_calls", None) or []:
        if call.get("name") == name:
            return call.get("args")
    content = getattr(ai, "content", "")
    if isinstance(content, str) and "{" in content:  # some models answer in text despite tool_choice
        try:
            return json.loads(content[content.index("{"):content.rindex("}") + 1])
        except ValueError:
            return None
    return None


def call_structured(ctx: store.Ctx, model: Any, schema: type[BaseModel], messages: list[BaseMessage], *,
                    step: str, check: Optional[Callable[[Any], None]] = None) -> Any:
    """One validated structured call: forced tool call -> Pydantic (+ semantic `check`) ->
    one repair attempt with the error fed back -> DeterministicError if still invalid."""
    bound = model.bind_tools([schema], tool_choice=schema.__name__)
    msgs, last_error = list(messages), ""
    for attempt in range(1 + ctx.cfg.schema_repairs):
        store.acquire_rate_token(ctx.conn, ctx.cfg)
        try:
            ai = bound.invoke(msgs, config=run_config(ctx, step))
        except Exception as err:  # network / HTTP errors from the provider client
            raise _classify(err) from err
        args = _tool_args(ai, schema.__name__)
        if attempt == 0 and chaos_hit(ctx, "malformed_llm"):
            args = {"injected_fault": "malformed output"}
        try:
            if args is None:
                raise ValueError("response contained no tool call or JSON object")
            out = schema.model_validate(args)
            if check:
                check(out)
            if attempt:
                store.log_event(ctx.conn, ctx.actor, "schema_repaired", task_id=ctx.task_id, step=step)
            return out
        except (ValidationError, ValueError) as err:
            last_error = str(err).replace("\n", " ")[:600]
            store.log_event(ctx.conn, ctx.actor, "schema_invalid", task_id=ctx.task_id, step=step,
                            attempt=attempt + 1, error=last_error)
            raw = json.dumps(args)[:1500] if args is not None else str(getattr(ai, "content", ""))[:1500]
            msgs = list(messages) + [HumanMessage(content=(
                f"Your previous response was invalid: {last_error}\nPrevious response: {raw}\n"
                f"Call {schema.__name__} again with corrected arguments."))]
    raise DeterministicError(f"{step}: output still invalid after repair: {last_error}")
