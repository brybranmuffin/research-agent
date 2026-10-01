"""Tools for all agents, plus the deterministic verification checks.

Component: Tool calls + verification (Tier 1); Context management (bounded, paged reads).

Search tools
- fts_search(query, k)        FTS5 over corpus chunks -> (doc_id, unit, chunk_id, snippet, rank)
- list_docs(filter)           document metadata
Read tools
- read_pdf(doc_id, pages)     bounded page window (~3 pages)
- read_html(doc_id, section)  bounded section window
Verification (deterministic, no LLM)
- verify_quote(quote, chunk_id)  exact -> fuzzy -> neighbouring chunk -> rejected
- compute_verdict(subq_id)       supported | contested | thin from verified claims
                                 (independence = distinct docs with different first authors)

Rules: out-of-range requests return a structured error instead of raising; every call is
logged as a tool_call event (counts as a step); chaos mode can inject failures.

LangChain:
- @tool with Pydantic args_schema for each search/read tool. With fixed pipelines, code calls
  tool.invoke(...), which still validates arguments and fires callbacks (tool_call logging);
  the tool schemas also fill the "tool definitions" section of the prompt.
- Tools return langchain_core Document objects (page_content + metadata: doc_id, unit,
  chunk_id), the standard LangChain retrieval type.
- Optional: wrap fts_search as a custom BaseRetriever.

Open: PDF/HTML parsing libraries (ingest); fuzzy-match library and thresholds.
"""
