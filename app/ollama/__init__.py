"""Ollama integration. This slice ships only a reachability check for /health;
the full httpx client (/v1/chat/completions, /v1/embeddings, /v1/models) is
ported in a later slice from the existing local-ai-model project.
"""
