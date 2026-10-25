# The project's private CPU-only Ollama on Cloud Run — `nomic-embed-text`
# (API embeddings, menu upload) + `llama3` (ingestion-job enrichment).
#
# Why CPU at all: Cloud Run GPU quota for this project came back explicitly
# denied in BOTH modes when actually attempted (2026-10-24), and a manual
# quota increase request was rejected too. Swap this service for the real
# GPU Ollama the moment quota clears (docs/gcp-aws-deployment-proposal.md
# §9) — that's also when `/query` can be re-enabled (`QUERY_ENABLED`).
#
# `llama3` added 2026-10-24 for the ingestion Cloud Run Jobs' enrichment
# only. Measured on this exact setup (8 vCPU/32GiB, real emails,
# 2026-10-24): a cold Gmail run of 4 emails ~6 min, Cupertino's 4 events
# ~2.4 min — fine for a cron, far too slow for `/query` (156-225s per
# question), which is why the API's chat stays off on GCP. The service name
# still says "embed" — renaming it would mean a new URL + IAM for no
# functional gain.
#
# Models are pulled at BUILD time, not left to runtime: this service scales
# to zero, and a cold start that had to download ~5GB first would time out.
# llama3 is pulled first, in its own layer, so rebuilds (and pushes) reuse
# that ~4.7GB layer whenever only the smaller layers change.
FROM ollama/ollama:latest

RUN (ollama serve &) \
    && sleep 5 \
    && ollama pull llama3 \
    && pkill ollama

RUN (ollama serve &) \
    && sleep 5 \
    && ollama pull nomic-embed-text \
    && pkill ollama

ENV OLLAMA_HOST=0.0.0.0:11434
EXPOSE 11434
ENTRYPOINT ["ollama", "serve"]
