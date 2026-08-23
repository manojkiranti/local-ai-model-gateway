"""API-key credentials for external (non-human) callers.

An API key identifies an `ApiClient`, which is deliberately NOT a `User`. That
separation is the point of this package: `app/auth/dependencies.py` resolves
humans and never sees a key, so no route written for a JWT user can be reached
with one, and no key can inherit admin or a department grant.

`keygen` and `policy` are PURE — no session, no ORM, no FastAPI — for the same
reason `app/rag/ranking.py` and `app/users/policy.py` are: the code that decides
whether a credential is accepted should be provable with no database.
"""
