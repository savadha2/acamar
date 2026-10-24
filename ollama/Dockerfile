# Interim CPU-only Ollama for Cloud Run — serves `nomic-embed-text` only.
#
# Why this exists at all: Cloud Run GPU quota for this project came back
# explicitly denied in BOTH modes when actually attempted (not just
# "ungranted" — a live `gcloud run deploy --gpu=...` was tried and
# rejected with and without zonal redundancy, 2026-10-24), contradicting
# the documented "3 GPU auto-grant on first deployment" behavior for
# reasons not explained by Google's own docs for this account. A manual
# quota increase request was submitted in parallel; this is the
# documented sprint-plan fallback for exactly that situation — not a
# permanent architecture choice. Swap for the real GPU Ollama the moment
# quota clears (see docs/gcp-aws-deployment-proposal.md §9).
#
# Scoped to embeddings only, deliberately: manual_menu ingestion (the
# thing actually blocked without this) needs ZERO LLM chat calls — see
# ingestion/service.py's `_enrich()`, which puts manual_menu in the
# "fully deterministic, no LLM at all" bucket. The only Ollama call
# anywhere in that path is `Embedder.embed()`. `llama3` (needed for
# /query's reasoning pipeline, out of scope for now) is NOT pulled here —
# trivial to add to this same service later when actually needed, but
# pulling an ~4.7GB chat model nobody's calling yet would only slow down
# every cold start for no benefit.
#
# Model is pre-pulled at BUILD time, not left to runtime: this service
# scales to zero like everything else in this project, and a cold start
# that has to download ~274MB before it can answer a single request
# would make the very first menu upload after any idle period time out.
FROM ollama/ollama:latest

RUN (ollama serve &) \
    && sleep 5 \
    && ollama pull nomic-embed-text \
    && pkill ollama

ENV OLLAMA_HOST=0.0.0.0:11434
EXPOSE 11434
ENTRYPOINT ["ollama", "serve"]
