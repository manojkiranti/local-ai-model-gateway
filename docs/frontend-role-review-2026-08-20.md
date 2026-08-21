# Frontend review of `feat/role` — findings for the gateway

Written 2026-08-20 from the FRONTEND repo (`local-ai-model-frontend`) while
building the per-department roles UI against
[`docs/frontend-sync-prompt.md`](frontend-sync-prompt.md). No gateway code was
changed. Verified against `feat/role` at `6606f79`.

The contract held up: the four traps in the frontend brief are all real and all
match the code. What follows is what did **not** match, worst first.

## 1. `GET /{code}/members` now 404s for a soft-disabled department — regression

`list_members` moved from `_require_department` (on `main`) to `_require_level`,
which resolves through `_require_active_department` and answers
`404 "Unknown department"` when `is_active` is false — **for global admins too**
(`app/rag/router.py:141-152`, `:249-288`).

On `main` an admin could manage grants on a disabled department. On `feat/role`
they cannot see them at all.

Active-gating the *corpus* routes is clearly deliberate and documented ("a
soft-disabled department is gone from the product"). But membership is not a
corpus operation, and just-disabled is exactly when someone wants to clean up a
stale grant. Nothing in `frontend-sync-prompt.md` mentions it, so a client that
follows the doc shows an admin a bogus "Unknown department" error on a department
it just listed to them.

**Suggested fix:** have the three member routes resolve with `_require_department`
plus the level check, skipping the active check — or document the behaviour if it
is intended.

**Meanwhile the frontend** does not call the members route for an inactive
department and says "Enable this department to manage its members." That
workaround should be removed if this is fixed.

## 2. `DepartmentOut.role` is `str | None = None`

`app/rag/schemas.py:39`. All three construction sites pass a real level
(`_department_out` from create, list, patch), so the `None` is currently
unreachable — which is the problem. A fourth site that forgets `role` ships
`null`, and a correctly fail-closed client then hides **every** control with no
error anywhere to explain it. The doc calls this field "the ONE field that
decides what to draw"; the type should say so.

**Suggested fix:** `role: DepartmentRole` (required, the `Literal`) on
`DepartmentOut`, and `MemberOut.role: DepartmentRole` (`:72`) likewise. Both are
currently `str`, so a typo in a future code path serialises rather than failing.

## 3. `POST` / `PATCH /v1/departments` are absent from the contract doc

Both are `require_admin` (`app/rag/router.py:85`, `:121`) and neither appears in
`frontend-sync-prompt.md`'s DEPARTMENTS section, which says only that `role` is
"the ONE field that decides what to draw" and that owner "also gets the members
screen".

Read literally that puts a department owner in front of Create / rename /
enable-disable forms the API then 403s — precisely the failure the doc's own
trap 2 warns about ("a UI that shows an upload button the API then refuses is
worse than no button"). The admin screen genuinely needs two gates: the global
role for department CRUD and `GET /users`, the department's `role` for everything
scoped inside a department.

**Suggested fix:** one line in the DEPARTMENTS section — department CRUD is
global-admin-only, and `owner` does not reach it.

## 4. A third 403 detail is undocumented

`_require_level` answers `"You do not have access to this department"` when the
caller holds no grant at all (`app/rag/router.py:279-282`), before the level
check. The doc lists the two escalation refusals and
`"Editor access to this department is required"` but not this one. Same handling
(render verbatim, no re-login), so it is a cheap addition and clients should not
have to discover it from the source.

## 5. Minor: granting by email does not check `is_active`

`users_repo.get_by_email` (`app/users/repository.py:24-27`) does not filter on
`is_active`, so an owner can grant a deactivated account, and it will then appear
in the members list. Harmless — that user cannot sign in — but it is a difference
between the two grant paths: the frontend's old admin-only user dropdown filtered
deactivated users out, and the email path cannot.

## Deploy coupling, for whoever merges this

The frontend's `atLeast` helper **fails closed**: an absent or unrecognised level
grants nothing. A gateway without `feat/role` sends no `role` field, so the RAG
admin screen's controls all disappear — silently, with no error to diagnose, and
it looks like a frontend bug. Ship the two together.

---

## Resolution (gateway, 2026-08-21)

All six items were triaged against an independent gateway-side code review. Five
accepted, one rejected with reasons; the code review also found one issue this
document did not, which outranked everything here.

| Item | Outcome |
|---|---|
| 1. Members 404 on a soft-disabled department | **Accepted.** The three member routes pass `require_active=False`. Remove the frontend workaround. Corpus routes still 404 there — that part was deliberate. |
| 2. Stale task block | **Accepted.** `frontend-sync-prompt.md` now reads "verify the department-role UI" with the corrected contract. |
| 3. `DepartmentOut.role` optional/untyped | **Accepted.** `role: DepartmentRole`, required, on `DepartmentOut` and `MemberOut`. |
| 4. Two doc gaps | **Accepted.** Department CRUD is documented as global-admin-only, and all three 403 details are listed. |
| 5. Granting a deactivated account | **Accepted, as a refusal.** Both identifier paths now load the target and answer **409** "That account is deactivated; reactivate it before granting access". |
| 6. Hardcoded `LEVEL_OWNER` on create/update | **Accepted.** Both derive through `access.effective_department_level`. |
| Question: owner cannot change/revoke themselves | **Was a real gap; fixed.** `grant_refusal(target_is_caller=True)` lets an owner step down or leave. Defaults to False so a forgetful caller gets the strict answer. |
| Related question: an owner loses the members screen when a department is disabled | **Intended, and now harmless.** `list_departments_for_user` still filters on active, so a non-admin owner has no UI path — but the route works, so an admin can always clean up. |

### Rejected

**The members route discloses department existence.** `_require_level` answers 404
for an unknown code and 403 for a real one, so an authenticated user can probe which
codes exist. Not changed: 403-for-ungranted is the existing documented contract on
every department-scoped route and the frontend branches on it, making members the
only inconsistent route if changed. Department codes are org unit names; the secret
is the corpus, which stays 404 at document granularity. Documented in `CLAUDE.md`
as an accepted trade-off rather than silently left.

### Found here, NOT in this document — and it outranked all of it

**Omitting `role` on a re-grant silently demoted an existing member, with a 204.**
`GrantCreate.role` defaulted to `"viewer"` and the route upserts, so "field absent"
was indistinguishable from "set to viewer": re-adding an owner stripped them to
viewer and overwrote `granted_at`. Reproduced live before fixing.
`test_granting_twice_is_idempotent` missed it because it only exercises
viewer→viewer.

**This affects the frontend too.** `api.ts`'s `grantDepartmentMember` takes
`role: DepartmentRole = 'viewer'` and always sends it, which reproduces the same
demotion from the client side even now the server preserves on absence. Omit the
field unless the user actually chose a level.

**And a second one:** the read-then-write in `grant_member`/`revoke_member` was an
unlocked TOCTOU — a global admin promoting someone to owner could race a department
owner demoting them, skipping the escalation guard. Membership writes now serialise
on the department row.
