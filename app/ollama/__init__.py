"""Ollama integration. This slice ships only a reachability check for /health;
the full httpx client (/api/chat, /api/embeddings, /api/tags) is ported in a
later slice from the existing local-ai-model project.
"""
