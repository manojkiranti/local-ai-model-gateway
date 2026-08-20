# Department-scoped roles — design

**Date:** 2026-08-20  **Branch:** `feat/role`  **Status:** approved design, not yet
implemented

## 1. The gap

Department **access** is already a database-backed invariant. `user_departments`
(`app/rag/models.py:92`) records who may reach which department;
`access.resolve_department` (`app/rag/access.py`) is the one boundary and re-checks
on every turn, so revocation lands on the next message rather than at token expiry;
and the composite FK `(document_id, department_id) → documents(id, department_id)`
makes a chunk's department *provably* its document's, so `WHERE department_id = ?`
is enforced by Postgres rather than by application code behaving correctly.

What does not exist is any notion of **what** a granted user may do there. There are
exactly two privilege levels and the powerful one is global:

| | scope | powers |
|---|---|---|
| `member` | granted departments | read + chat only |
| `admin` | **every** department, plus users and the NRB pipeline | all 18 admin routes |

`users.role` is two hardcoded strings (`app/users/models.py:22`) and grants are
binary — present or absent. The consequences that motivate this work:

- **Every curation route is `require_admin`** (`app/rag/router.py:251`, `:305`,
  `:507`). An HR manager who should own the HR corpus cannot upload one file.
  Promoting them hands over Finance, Credit, the whole user table, `PATCH
  /users/{id}` (the offboarding switch) and `POST /v1/nrb/runs`. In a bank that is
  not a workaround, it is an audit finding.
- **No read/write distinction inside a department.** "HR editor" versus "HR reader"
  is not expressible.
- **`users.role` has no CHECK constraint**, unlike `auth_provider` and
  `documents.status`. Its vocabulary is open, so a typo'd role is silently a
  non-admin.

This design adds a level to the grant, delegates curation to it, and leaves global
admin exactly as it is.

## 2. Decisions

| Decision | Choice | Why |
|---|---|---|
| Role model | Per-department level on the existing grant | Access and level become one fact that cannot disagree with itself. |
| Levels | `viewer` < `editor` < `owner`, totally ordered | An ordering makes "at least editor" one comparison at every call site. |
| Where it lives | `user_departments.role` | A separate `department_roles` table admits two rows that contradict each other — a grant with no level, a level with no grant. This codebase makes illegal states unrepresentable (`ck_users_credential`, the chunk FK); adding one that needs application code to behave correctly is the wrong direction. |
| Not permission strings | Rejected for v1 | `text[]` has no ordering (set arithmetic at every call site), Postgres cannot CHECK array *contents* (a typo is a silently lost permission — exactly what `ck_documents_status` exists to prevent), and the frontend cannot render "your level here" from a set. The ordered level migrates cleanly into real RBAC later: the level becomes a named role carrying permissions. |
| Delegation | An owner grants `viewer`/`editor`, never `owner` | Bounds the escalation chain at depth 1. Minting peers stays with global admin, mirroring the existing rule that `role` is not patchable through `PATCH /users/{id}` — promotion is an escalation surface wanting its own guard. |
| Column type | `varchar(16)` + CHECK, not a PG enum | Follows `documents.status` and `ck_users_auth_provider`. |
| `DEFAULT 'viewer'` | Kept after backfill | Least privilege is the right failure mode for an insert that forgets the level, and it makes the migration backfill-free. |
| `DepartmentContext` | **Untouched** | The routes needing levels never call `resolve_department`, and nothing in the retrieval path reads a level. Adding it would be speculative surface on the model-facing contextvar. |
| Department rename / deactivate | Stays global admin | Soft-disable is the *only* retirement path (`ON DELETE RESTRICT` on both `documents` and `chat_sessions`) and it hides the department from everyone. |
| NRB pipeline | Stays global admin | `/v1/nrb/*` is cross-department machinery, not a department's corpus. |

## 3. The permission matrix

The contract. `owner` implies `editor` implies `viewer`.

| Capability | viewer | editor | owner | global admin |
|---|:--:|:--:|:--:|:--:|
| Chat + retrieval in the department | ✓ | ✓ | ✓ | ✓ (bypasses grant) |
| Download `ready` documents | ✓ | ✓ | ✓ | ✓ |
| See the department in `GET /v1/departments` | ✓ | ✓ | ✓ | all of them |
| List non-`ready` and archived documents | — | ✓ | ✓ | ✓ |
| Upload / add text documents | — | ✓ | ✓ | ✓ |
| Archive a document | — | ✓ | ✓ | ✓ |
| Poll ingest jobs **for this department** | — | ✓ | ✓ | any department |
| List members | — | — | ✓ | ✓ |
| Grant / revoke `viewer`, `editor` | — | — | ✓ | ✓ |
| Grant / revoke **`owner`** | — | — | — | ✓ |
| Create / rename / deactivate a department | — | — | — | ✓ |
| `/users/*`, `/v1/nrb/*` | — | — | — | ✓ |

## 4. Schema

One migration, parent = current head `b7e3d95a41c8`.

```sql
ALTER TABLE user_departments
  ADD COLUMN role varchar(16) NOT NULL DEFAULT 'viewer';
ALTER TABLE user_departments
  ADD CONSTRAINT ck_user_departments_role
  CHECK (role IN ('viewer', 'editor', 'owner'));

ALTER TABLE users
  ADD CONSTRAINT ck_users_role CHECK (role IN ('admin', 'member'));
```

No new index: the level is read by primary key, `(user_id, department_id)`.

The `users` CHECK is folded in here deliberately. It is the same class of
constraint, it closes a gap found while reading this code, and adding it in a
separate migration would mean two revisions touching role vocabularies for no
benefit. `alembic heads` must remain **one** — `tests/test_alembic_lineage.py`
fails if a second appears.

## 5. Enforcement

### 5.1 `app/rag/permissions.py` — pure

Mirrors `app/users/policy.py`, for the reason stated there: the guard *is* the
security property, and the escalation branch cannot be safely rehearsed against a
real database.

```python
LEVELS = ("viewer", "editor", "owner")   # ordered; index is the rank

def allows(level: str | None, required: str) -> bool
def effective_level(grant: str | None, *, is_global_admin: bool) -> str | None
def grant_refusal(*, caller_level, caller_is_global_admin,
                  requested_level, existing_target_level) -> str | None
```

**The trap this module exists to prevent.** A global admin is `owner`-equivalent
for *capabilities* but must NOT be run through the owner escalation rule. Collapse
the two and "an owner cannot grant `owner`" also stops global admins creating
owners — the feature becomes unusable, and whoever debugs it deletes the guard. So
`allows()` takes a level, while `grant_refusal()` takes `caller_is_global_admin` as
its own argument. Both callers pass both facts; neither derives one from the other.

`grant_refusal` also covers **revocation** (`requested_level=None`): an owner may
neither modify nor remove a row whose existing level is `owner` — demotion is the
same escalation surface as promotion. That is the lateral-attack case — owner A
evicting owner B — and it needs no "last owner" guard, because global admin is
always the backstop. This is unlike `LAST_ADMIN` in `app/users/policy.py`, where no
backstop exists.

Two resolutions stated explicitly, because both are decidable either way:

- **`effective_level` takes the MAXIMUM**, so a global admin who also holds a
  `viewer` grant is `owner`, not `viewer`. A global admin never loses a capability
  by being granted a department.
- **An owner SEES every member, including other owners**; `grant_refusal` restricts
  writes, not reads. Hiding owners would leave an owner unable to explain why a
  revoke was refused.

### 5.2 Wiring

| Change | Detail |
|---|---|
| `repo.has_department_access` → `repo.get_department_level` | Returns `str \| None`. Non-None means access, so `access._require_grant` is a one-line change and chat works at any level. Same single query, so slice 3's zero-additional-round-trips property holds. |
| `_require_department_access` → `_require_level(session, user, code, minimum)` | Used by every `/v1/departments/*` route. Resolves the effective level, then one `allows()` call. |
| `DepartmentContext`, `rag_context`, `search_department_docs` | Untouched. |

## 6. Flow bugs a column-only version ships with

Naming these because they are why this is more than a column.

1. **`GET /v1/ingest-jobs/{id}` is global-admin-only** (`app/rag/jobs_router.py`).
   An editor uploads, receives `{document_id, job_id}`, then 403s polling their own
   job. The gate becomes editor-on-the-job's-department (job → document →
   department), and a no-access answer is **404**, matching the download route's
   refusal to leak the corpus's shape.
2. **`MemberOut` returns `user_id` only.** An owner opening the members screen sees
   bare integers. It gains `email` and `role` via a join to `users`. These are staff
   identities and an owner deciding who keeps access needs them.
3. **An owner cannot resolve an email to an id**, because `GET /users` is
   global-admin. No change needed — and this is why `GrantCreate`'s `email` XOR
   `user_id` is now load-bearing: an owner grants by email and never touches the
   user table.

## 7. API contract

| Endpoint | Change |
|---|---|
| `GET /v1/departments` | `DepartmentOut` gains **`role`** = the caller's *effective* level (`owner` for a global admin with no grant row). One server-side rule the frontend reads: `role >= editor` → draw the upload button. No policy duplicated on the client. |
| `GET /v1/departments/{code}/members` | `MemberOut` gains `email` and `role`. Owner-accessible. |
| `POST /v1/departments/{code}/members` | `GrantCreate` gains optional `role` (`Literal["viewer","editor","owner"]`, default `viewer`). Also **upserts** the level on an existing grant, making this the promote/demote route. |
| `DELETE /v1/departments/{code}/members/{user_id}` | Owner-accessible, subject to `grant_refusal`. |
| `POST /v1/departments/{code}/documents`, `.../documents/text`, `DELETE .../documents/{id}` | Gate moves from global admin to editor. Shapes unchanged. |
| `GET /v1/departments/{code}/documents` | Editor and above get `DocumentAdminOut` and may pass `?include_archived=`. Viewer keeps today's `ready`-only `DocumentOut`. |
| `GET /v1/departments/{code}/documents/{id}/download` | Viewer: `ready` only. Editor and above: any status. |
| `GET /v1/ingest-jobs/{id}` | Editor of the job's department, or global admin. |
| `POST /v1/departments`, `PATCH /v1/departments/{code}`, `/users/*`, `/v1/nrb/*` | Unchanged. |

`role` is the field name on all three of the column, `MemberOut` and
`DepartmentOut`. It reads unambiguously in context ("your role in this
department"), and one name for one concept beats a second vocabulary.

## 8. Error semantics

| Situation | Status | Why |
|---|---|---|
| No grant at all | **403** | Existing behaviour, unchanged |
| Grant present, level too low | **403**, `detail` naming the level required | Nothing to leak: they already know the department exists |
| Owner grants or revokes an `owner` | **403**, `detail` from `grant_refusal()` | Rendered verbatim by the frontend, the convention `PATCH /users/{id}`'s 409 already sets |
| Unknown or inactive department | **404** | Unchanged, admins included |
| Document not visible at your level | **404** | Unchanged — do not leak the corpus's shape |
| Unknown role value in a request body | **422** | Pydantic `Literal`, before the DB CHECK is reached |

## 9. Testing

| Test | Covers |
|---|---|
| `tests/test_department_permissions.py` (new, pure, no DB) | The ordering; `allows()` over every level × capability; `grant_refusal()` **exhaustively** over caller level × `is_global_admin` × requested × existing. This is the security proof, and it must not need Postgres. |
| `tests/test_rag_departments_api.py` (extend) | editor uploads → 202; viewer uploads → 403; owner grants editor → 204; owner grants owner → 403; **global admin grants owner → 204**; owner revokes owner → 403; `GET /v1/departments` reports `role` |
| `tests/test_rag_documents_api.py` (extend) | list and download status visibility per level; `include_archived` at viewer → 403 |
| `tests/test_rag_access_integration.py` (extend) | all three levels can chat; revocation still lands on the next turn |
| `tests/test_rag_jobs_integration.py` (extend) | editor polls their own job → 200; unrelated member → 404 |
| `tests/test_rag_schema_integration.py` (extend) | `ck_user_departments_role` rejects a typo; pre-existing grants read `viewer` |

**Regression watch.** The `_auth` helpers duplicated across the integration modules
**skip on auth failure**, so breaking them turns ~86 tests into silent skips that a
green run hides. Compare the **skip count** before and after, not just the pass
count. Blast radius should be nil: `viewer` is byte-for-byte today's member
semantics, and no existing test uploads as a non-admin because it could not.

## 10. Rollout

Behaviour-neutral on deploy. Every existing grant becomes `viewer`, which is
exactly today's member powers; every global admin is unchanged. Nobody gains or
loses a capability until an admin sets a level.

`alembic upgrade head` against **both** `local_ai_gateway` and the NRB scratch
database `local_ai_gateway_p4`, with one head afterwards.

Docs to update in the same branch: `CLAUDE.md` (endpoint list, plus a gotcha bullet
for the global-admin-versus-owner trap in §5.1), `README.md` §Auth model,
`STATUS.md`, and `docs/frontend-sync-prompt.md` — the frontend is a separate repo
(`local-ai-model-frontend`) and cannot render the upload button without `role`.

## 11. How we know it works

- **Success metric:** the number of global-admin promotions required for ordinary
  corpus curation drops to zero. Proxy, checkable in SQL: no user holds
  `users.role='admin'` whose only need is uploading to one department.
- **Eval:** §9's permission matrix suite is the labelled set — every (level,
  capability) pair in §3 has a test asserting allow or deny. Target is 100%; the
  matrix in §3 and the tests must be kept in step, and a new capability means a new
  row in both.
- **Feedback capture:** a refused request returns a `detail` naming the level
  required, so a user reporting "I cannot upload" carries the diagnosis in the
  message. `user_departments.granted_by` and `granted_at` already record who
  granted what.
- **Review loop:** revisit when the first request arrives that the three levels
  cannot express. That request is the signal to reconsider real RBAC (§2), not a
  fourth level.

## 12. Out of scope (YAGNI)

Named or global roles and a `roles` table · permission strings · per-document ACLs ·
making `users.role` patchable through the API · department-scoped access to the NRB
pipeline · owners renaming or deactivating their department · a "last owner" guard
(global admin is the backstop).
