# Role-based MCP tool access

**Date:** 2026-08-24
**Status:** design, approved in chat; implementation plan not yet written
**Scope:** `local-llm-mcp` (`src/auth/`, `src/tools/`), and in this repo
`app/mcp/`, `app/users/`, a migration, and a new admin route

---

## 1. The problem

`local-llm-mcp` exposes 13 tools over one shared bearer token and **no notion of
who is asking**. Every authenticated caller sees every tool. Three of those tools
reach data that most staff must not read:

| tool | what it actually exposes |
|---|---|
| `search_ems_records` | caller-composed read-only SQL over the **whole** expenses database |
| `get_hrms_employee_details` with full detail | **80+ HRMS fields**, including `Salary_Level` and `Salary_Level_Description` |
| `get_hrms_employee_tasks` | pending approval counts per employee — resignation, personal/home/vehicle loan, salary advance |

The gateway is the single authenticated front door and already knows exactly who
is asking (`get_current_user` re-reads the user row on every request), and it
already forwards `x-user-email` to the MCP server. The MCP server already parses
that header into its session as `userEmail` and, in its own README's words, it
"is optional and is not currently used to authorize or scope tools."

So the identity plumbing exists end to end and the decision does not. This design
adds the decision.

### 1.1 What this is not

This is **not** a context-budget problem. `local-llm-mcp` has 13 tools, roughly
3k schema tokens on top of the 17 local tools, against `CONTEXT_WINDOW_TOKENS`
of 32768. (The sibling `odin-marketing-mcp`, read as the reference pattern for
this design, has **198** tools across 14 groups — at the ~230 tokens/schema
measured in CLAUDE.md that is ~45k tokens of schemas alone, more than the whole
window, and for that server role-gating is a prerequisite for basic usability
rather than only an authorization boundary. That is not the situation here.)

Tool exposure here is purely a question of who may reach which backend.

---

## 2. What already exists

### 2.1 `local-llm-mcp` today

- FastMCP, `ServiceSession = {userEmail: string | null, [key: string]: unknown}`.
- `createAuthenticate` compares the bearer token with `timingSafeEqual` and 401s
  on mismatch. It then reads `x-user-email` and stores it, unused.
- 13 tools in four groups: `basic` (3), `hrms` (4), `izone` (4), `ems` (2).
- All read-only. `HOST` defaults to `127.0.0.1`; `MCP_SERVICE_TOKEN` required.
- No `canAccess` anywhere, so every tool is visible to every caller.

### 2.2 The reference pattern (`odin-marketing-mcp`)

Read as the model for this design, and worth recording because two of its choices
transfer and one deliberately does not.

**Transfers:** per-tool `canAccess: (session) => hasRole(session, MCP_ROLES.X)`;
a coarse role per integration; and a separate finer-grained *permission* concept
(`MCP_PERMISSIONS.SYNC_EXECUTE = 'mcp.sync.execute'`) reserved for the single
dangerous action inside an otherwise-read-only group. Its session shape is
`{roles: Set<string>, permissions: Set<string>, isSuperuser: boolean}`.

**Does not transfer:** it resolves roles by calling its own backend
(`GET /v1/mcp/users/by-email/{email}/roles` on BrokerCopilot, 5-minute cache,
failing to empty sets on error). It has to: it is exposed to the public internet
and Claude.ai connects to it directly over Google OAuth, so there is no trusted
front door and the server must resolve roles for itself. `local-llm-mcp` is the
opposite case — bound to localhost, one shared token, and this gateway is its only
client. The architectural reason for the callback is absent here, so §4 chooses
the header instead. The *session shape* is kept identical anyway, so the callback
remains a drop-in replacement later without touching a single `canAccess`.

### 2.3 Two facts read out of the FastMCP source

Both are load-bearing and neither is documented in FastMCP's README.

**`canAccess` is a real boundary, not a list filter.** In
`FastMCP.#createSession`:

```js
const allowedTools = auth ? this.#tools.filter(
  (tool) => tool.canAccess ? tool.canAccess(auth) : true
) : this.#tools;
```

`allowedTools` is what the session is constructed with, so a tool the caller may
not access is never registered in that session's handlers. Calling it fails as an
unknown tool, not as a permission error. This is what makes server-side
enforcement trustworthy, and it is what §4 relies on.

**The default is permissive.** `tool.canAccess ? ... : true` — a tool that
declares no `canAccess` is visible to everyone. So a tool added later without a
gate is silently world-readable. This is the §18 failure class from
`docs/nrb-integration.md`: it looks exactly like a correct deployment. It cannot
be handled by remembering; §8 rule 4 makes it a test.

**A tool's `execute` can read the session.** `Context<T>.session` is typed
`T | undefined`, so argument-level decisions are possible inside a tool body —
which §6.2 needs, and which `canAccess` (it receives the session, never the
arguments) cannot express. The reference server never uses this.

### 2.4 The gateway today

- One process-wide `MCPClient` on `app.state.mcp`, built in `lifespan`.
- `_session_headers(user_email)` builds `Authorization` + `x-user-email`.
- `filter_tools` applies `MCP_TOOL_MODE` (`read_only` | `allowlist` | `all`) — a
  name-token heuristic, deployment-wide, identical for every user. `.env` is
  currently `read_only`.
- Three call sites already thread `user_email`: `GET /v1/tools`,
  `GET /v1/mcp/status`, and `stream_turn` → `mcp.session()`.
- Identity: `users.role` in (`admin`, `member`); per-department
  `user_departments.role` in (`viewer`, `editor`, `owner`).

---

## 3. Decisions taken

Four, each settled in chat before this document existed.

**3.1 Tool access is a property of the PERSON, not of the department.** An
explicit per-user grant, administered by an admin. Rejected: deriving it from the
chat session's department. `user_departments.role` is about *curating documents* —
being an HR document viewer must not imply reading 80-field employee records with
`Salary_Level` in them, and a Finance document viewer must not imply a SQL console
over the expenses database. A department-scoped narrowing (show only the tools
relevant to the current tab) remains available later as a *second* filter on top
of the grant; it is not the authorization mechanism.

**3.2 The gateway forwards grants as headers.** Rejected: a callback from the MCP
to the gateway (§2.2 — the reason it exists over there does not apply here), and
an HMAC-signed assertion (the signing key would live in the same `.env` as the
token it protects, so it buys audit clarity rather than isolation). See §4 for the
trust model this assumes and §11 for when signing becomes mandatory.

**3.3 Roles name systems; permissions name sharp edges.** Three roles for which
backend a caller may touch, three permissions for the dangerous capabilities
inside them. This makes the reference server's two concepts principled rather
than incidental: adding a dangerous tool later adds a permission, never a new
role. Rejected: flat one-role-per-system (the staff directory and the salary
record become one grant); a `viewer < editor < owner`-style ladder (access to
expenses and access to HR records are *orthogonal*, not ordered — Finance needs
EMS and not salary data, HR the reverse, and a ladder forces granting one to get
the other); and per-tool grants (13 names, an unusable admin surface, and a
vocabulary that churns on every tool added).

**3.4 A global gateway admin holds NO MCP grant implicitly.** `users.role =
'admin'` confers the ability to *grant*, not the grants themselves. An admin who
needs EMS SQL grants it to their own account, leaving a `granted_by` /
`granted_at` row. This deliberately departs from
`permissions.effective_level(is_global_admin=True) → owner`, and the departure is
the point: gateway admin is an IT/ops role, and auto-conferring `Salary_Level`
and an expenses SQL console on whoever operates the gateway is precisely the quiet
escalation a bank audit objects to. Note the reference server reached the same
conclusion for its shared identity, hard-pinning `isSuperuser: false` with the
comment "never superuser for the shared channel identity".

Consequence for the code: there is **no `isSuperuser` field** in the session type.
An unused superuser flag is something a later contributor wires up.

---

## 4. Trust model: who enforces what

The bearer token remains the trust boundary. That is already this server's stated
model — `HOST=127.0.0.1`, and the README says "possession of the shared token
grants access to every registered tool. Keep this service on localhost or a
private network that only the gateway can reach."

The gateway holds the token; gateway users never see it. So **a chat user cannot
forge a grant header**, which is the threat that matters: bank staff using the
chat UI. `canAccess` additionally defends against a gateway filtering bug, a
second client added later, and accidental exposure. It does *not* defend against
someone who holds the token and can reach the port — but that person can already
call all 13 tools today, so nothing regresses. §11 records when this stops being
true.

### 4.1 The gateway needs no tool→grant map

This is the load-bearing structural decision and it is easy to get wrong.

Because `canAccess` is applied at *session construction* (§2.3), the MCP server's
`tools/list` **already returns exactly the tools this identity may call**. The
gateway sends identity + grants and receives the authorized set back. It never
re-derives which grant gates which tool.

The alternative — the gateway keeping its own copy of the mapping so it can filter
before the model sees the list — produces two copies that drift. And the drift is
asymmetric: "gateway shows a tool the MCP refuses" is merely confusing (the model
gets an unknown-tool error and retries), while "gateway hides a tool the MCP would
allow" is a **silently lost capability** with no error anywhere, in any log, on
either side. That is the same shape as every defect in `docs/nrb-integration.md`
§18. One mapping, in the enforcer, read by both.

This also satisfies "the model must never be offered a tool it cannot call" for
free: tool descriptions are the routing prompt, a listed tool is an invitation,
and the list the gateway receives is already filtered.

The gateway's existing `MCP_TOOL_MODE` heuristic is untouched and stays orthogonal
— it is a deployment-wide reads-vs-writes policy, not a per-user one. The two
compose as intersection.

### 4.2 The one thing `tools/list` cannot express

`mcp.hrms.full` gates an **argument**, not a tool (§6.2). `canAccess` receives the
session and never the arguments, so `get_hrms_employee_details` stays listed for
every `mcp-hrms` holder and the full-detail decision happens inside `execute`.
The tool's description states the requirement statically, so the model is not
guessing, and the refusal announces itself in the result.

---

## 5. The vocabulary

Six strings. Roles are hyphenated, permissions dotted, following the reference
server (`mcp-hubspot` vs `mcp.sync.execute`).

```
mcp-hrms         list_hrms_employees, list_hrms_departments,
                 get_hrms_employee_details (summary detail only)
mcp-izone        list_izone_lists, list_izone_list_items,
                 list_izone_documents, search_izone_country_circulars
mcp-ems          list_ems_tables            (schema discovery only)

mcp.hrms.full    full employee detail — 80+ fields incl. Salary_Level
mcp.hrms.tasks   get_hrms_employee_tasks
mcp.ems.query    search_ems_records         (free-form SQL)
```

`get_server_time` is ungated and available to everyone. `get_echo` and
`list_examples` are **not registered** in production: they are development
harness tools, and every registered tool costs schema tokens in every turn's
prompt. They stay in the tree behind the same switch the sample-data fallbacks
use, so `npm run inspect` keeps working.

### 5.1 A permission never implies its role

`search_ems_records` requires `mcp-ems` **and** `mcp.ems.query`, checked as an
explicit conjunction. No implication magic. This falls out of the tool's own
contract anyway — its README entry says to call `list_ems_tables` first "to learn
real table/column names" — so the SQL grant is useless without the schema grant,
and granting them separately would be a trap rather than a feature.

Same for `get_hrms_employee_tasks`: `mcp-hrms` **and** `mcp.hrms.tasks`.

### 5.2 What the vocabulary cannot fix

`mcp-izone` is coarse by nature. `list_izone_list_items` reads any SharePoint list
by exact title, so the grant is effectively "everything the iZone service account
can see". Narrowing that is a change to the *tool* (an allowlist of list titles),
not to the role vocabulary, and is out of scope here. Recorded so nobody reads
`mcp-izone` as narrower than it is.

---

## 6. MCP server design (`local-llm-mcp`)

```
src/auth/mcp-roles.ts       MCP_ROLES (3) + MCP_PERMISSIONS (3) — the vocabulary
src/auth/access-control.ts  hasRole / hasPermission — pure, no I/O
src/auth/service-token.ts   parse the two headers into Sets
```

`access-control.ts` is deliberately *not* a port of the reference server's file of
the same name: there is no `fetchUserAccess`, no HTTP client and no cache, because
§3.2 chose the header. What is kept is the shape of `hasRole`/`hasPermission` so
the file can gain a resolver later without its callers changing.

```ts
export type ServiceSession = {
  userEmail: string | null;
  roles: Set<string>;
  permissions: Set<string>;
  [key: string]: unknown;
};
```

No `isSuperuser` (§3.4).

`createAuthenticate` parses `x-user-roles` and `x-user-permissions` as
comma-separated lists, trimming and dropping empties. Absent header ⇒ empty Set.
The token check is unchanged and still runs first.

### 6.1 Gates

| tool | gate |
|---|---|
| `get_server_time` | none — ungated, listed for everyone |
| `get_echo`, `list_examples` | unregistered in production |
| `list_hrms_employees` | `mcp-hrms` |
| `list_hrms_departments` | `mcp-hrms` |
| `get_hrms_employee_details` | `mcp-hrms`; full detail additionally needs `mcp.hrms.full` (§6.2) |
| `get_hrms_employee_tasks` | `mcp-hrms` **and** `mcp.hrms.tasks` |
| `list_izone_lists` | `mcp-izone` |
| `list_izone_list_items` | `mcp-izone` |
| `list_izone_documents` | `mcp-izone` |
| `search_izone_country_circulars` | `mcp-izone` |
| `list_ems_tables` | `mcp-ems` |
| `search_ems_records` | `mcp-ems` **and** `mcp.ems.query` |

### 6.2 The full-detail gate, and the second door

`get_hrms_employee_details` is the only path to the 80+ field record, and it has
**two** ways in. From `src/tools/hrms/employee-details.ts`:

```js
const wantsFull = full || Boolean(employeeNo);
```

So gating the `full` parameter alone leaves `employeeNo` wide open — pass an
employee number and full detail is returned regardless. **The gate belongs on
`wantsFull`.**

The fix is not to gate `employeeNo`. Looking up EMP-1001's name, title, branch
and work email is ordinary directory use and must keep working for a plain
`mcp-hrms` holder. So:

- `wantsFull && !hasPermission(session, MCP_PERMISSIONS.HRMS_FULL)` ⇒ return the
  **summary** projection (`toSummary`, 11 fields) for the same query, plus an
  explicit note in the response that full detail was withheld and why.
- Never refuse the whole call, and never silently downgrade. The note is the same
  rule as `agent/loop.py`'s `[TRUNCATED …]` and `read_image`'s `PARTIAL:` line: a
  quiet reduction reads to the model as a complete answer, and it will then tell
  the user the field does not exist rather than that they may not see it.
- `session` is `T | undefined`, so absence fails closed to summary.

`list_hrms_employees` needs no equivalent gate — it maps `toSummary`
unconditionally and has no full-detail path.

---

## 7. Gateway design

### 7.1 Migration

```sql
CREATE TABLE user_mcp_grants (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    grant_key   VARCHAR(64) NOT NULL,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    PRIMARY KEY (user_id, grant_key),
    CONSTRAINT ck_user_mcp_grants_key CHECK (grant_key IN (
        'mcp-hrms', 'mcp-izone', 'mcp-ems',
        'mcp.hrms.full', 'mcp.hrms.tasks', 'mcp.ems.query'
    ))
);
```

`ON DELETE CASCADE` on `user_id` because a grant has no meaning without its user;
`ON DELETE SET NULL` on `granted_by` because the audit fact survives the granter
leaving. `users.id` is an **INTEGER** (`Mapped[int]`, not a UUID) and
`user_departments.user_id` is the precedent to copy.

The CHECK closes the vocabulary for the reason `ck_user_departments_role` and
`ck_documents_status` do: a typo'd grant must not be *storable*. A Python-side
frozenset stops one being *honoured*. Those are two copies on purpose, exactly as
`ck_api_keys_scopes` and `policy.ALL_SCOPES` are. Adding a grant means editing
both.

Note the migration must sit on the current single Alembic head —
`tests/test_alembic_lineage.py` fails if a second appears.

### 7.2 `app/mcp/grants.py` — pure

No DB, no HTTP, for the reason `app/rag/permissions.py` and `app/users/policy.py`
are pure: this is the code that decides a user-visible capability boundary, and it
should be provable without a database or a model. It holds:

- `ROLES` / `PERMISSIONS` frozensets — the second copy of the CHECK.
- `McpIdentity(email, roles, permissions)` — a frozen dataclass.
- `header_values(identity) -> dict[str, str]` — the serialisation, so exactly one
  place knows the header names and the comma format.
- Unknown grant strings are dropped on construction, not raised on (§8 rule 3).

### 7.3 Threading the identity

`McpIdentity` replaces the bare `user_email` parameter at the three sites that
already thread it: `tools/router.py`, `mcp/router.py`, and `agent/loop.py`'s
`stream_turn` / `run_turn` → `MCPClient.session()` / `describe()`. All three pass
the same three facts today-and-forever, so one value object beats three
parameters.

`MCPClient` stays a process-wide singleton and the identity stays a **per-call
argument, never client state** — two concurrent users would otherwise race each
other's grants through `app.state.mcp`.

Grants load alongside the user row. Precedent for the cost: `resolve_department`
folds its grant check into `open_turn`'s existing query and measures 0.518 ms
against a multi-second turn, and `get_current_user` already reads the user row
every request.

`/v1/tools` and `/v1/mcp/status` become genuinely per-user as a side effect, which
is what the frontend's MCP badge should have shown all along.

### 7.4 Admin API

`GET | POST | DELETE /v1/users/{id}/mcp-grants`, admin-only via `require_admin`.

Deliberately **not** folded into `PATCH /users/{id}`, which already refuses
`role` with `extra="forbid"` on the grounds that an escalation surface wants its
own guards. Same reasoning, same shape.

`POST` validates against the frozenset and 422s an unknown key (the CHECK would
catch it as a 500 otherwise). `granted_by` is the calling admin. Re-granting an
existing key is idempotent and must **not** rewrite `granted_at` — the
`test_omitting_role_on_a_RE_grant_does_not_demote` lesson from
`POST .../members`: an upsert that quietly overwrites audit columns reports
success while destroying the record of when access was actually given.

---

## 8. Fail-closed rules

Four, each with a named failure it prevents.

1. **Absent or empty headers ⇒ empty Sets ⇒ only `get_server_time`.** Never "no
   grants means unrestricted". This inverts `app/rag/ranking.py`, which fails
   *open* — there, withholding an answer asserts something false about the bank's
   own policies; here, withholding a tool withholds someone's salary data, so the
   `app/nrb/` rule applies instead.
2. **`hasRole` / `hasPermission` return false on a missing session.**
   `Context.session` is `T | undefined` and `canAccess` can be reached with a
   falsy auth; neither may throw, and neither may pass.
3. **Unknown grant strings are ignored, never errors** — on both sides. The
   gateway and the MCP server deploy independently, so vocabulary skew happens in
   both directions: a gateway on a newer vocabulary sending a grant the server has
   never heard of, and a server expecting one the gateway cannot yet store. Both
   must degrade to less access, not to a 500.
4. **Every registered tool declares `canAccess`, or sits in an explicit
   `PUBLIC_TOOLS` allowlist** — asserted by a test over the registered tool list,
   because FastMCP's default is permissive (§2.3) and a forgotten gate is
   invisible in every log on both sides.

---

## 9. Testing

**In `local-llm-mcp`:**

- `hasRole` / `hasPermission` unit tests, including `undefined` session, empty
  Sets, and an unknown grant string.
- Header parsing: absent, empty, whitespace, single, many, unknown values mixed
  with known ones.
- The §8 rule 4 registry test: enumerate registered tools, assert each has
  `canAccess` or is named in `PUBLIC_TOOLS`.
- The §6.2 gate: `full=true` without the permission, **`employeeNo` without the
  permission** (the second door — this is the test that would have caught it),
  both with the permission, and the note's presence in each withheld case.

**In the gateway:**

- `grants.py` purity and vocabulary tests. The two copies of the vocabulary are
  reconciled by one **integration** test that reads the live constraint
  (`pg_constraint.consrc` / `pg_get_constraintdef` for
  `ck_user_mcp_grants_key`) and compares the parsed string set against
  `ROLES | PERMISSIONS`. Asserting the frozenset against a hand-written literal
  was considered and rejected: it makes a third copy, which is the drift it is
  supposed to detect.
- The identity reaches `_session_headers`; a second concurrent identity does not
  leak into the first (the singleton trap in §7.3).
- Admin route: 401 unauthenticated, 403 non-admin, 422 unknown grant, idempotent
  re-grant leaves `granted_at` unchanged, DELETE is idempotent.
- `users.role='admin'` alone yields an **empty** grant set (§3.4 — the departure
  from `effective_level` is the kind of thing a later refactor "fixes" back).

---

## 10. Evaluation & improvement

**Success metric.** Two-sided, because only one side is loud:

- No tool is ever executed for an identity lacking its grant. Target 0.
- No authorized tool is hidden from an identity that holds its grant. Target 0.

The second is the one that hides — it presents as a clean deployment where a user
simply never gets an answer they were entitled to. A per-turn log line pairing
grants-held against tools-available makes it observable rather than inferable,
which is the §18 lesson applied ("verify a worker image by its route split on a
known blob, never by whether ingestion succeeded").

**Eval.** One labelled matrix, six synthetic identities × all 13 tool names =
78 assertions generated from a single table, scored as **exact set equality** on
the visible tool list. All 13 names, not the 11 registered in production: the two
dev tools are expected **absent for every identity**, so the matrix doubles as the
guard that §5's dropped tools stay dropped.

| identity | grants held |
|---|---|
| `none` | — |
| `hrms_directory` | `mcp-hrms` |
| `hrms_full` | `mcp-hrms`, `mcp.hrms.full`, `mcp.hrms.tasks` |
| `izone` | `mcp-izone` |
| `ems_schema` | `mcp-ems` |
| `ems_query` | `mcp-ems`, `mcp.ems.query` |

Plus four argument-level cases on `get_hrms_employee_details`: `full=true` and
`employeeNo` set, each with and without `mcp.hrms.full`. Plus the §8 rule 4
registry test.

Target is **100%**, and the target is 100% because this is a boundary and not a
judgment — unlike the 8/8 document eval or the retrieval cohort, there is no
tolerable disagreement rate. Anything below 100% is a defect, not a score.
Current pass rate: not yet implemented.

**Feedback capture.** Every refusal logs the grant that was missing — the tool
name, the identity, and the specific key. An over-tight grant then surfaces as one
user's repeated refusals for one key, which is actionable, instead of as a
capability nobody noticed was gone. The withheld-full-detail note travels back to
the model and thus into the answer, so the *user* can say "I should have that"
rather than concluding the data does not exist.

**Review loop.** Quarterly review of who holds `mcp.ems.query` and
`mcp.hrms.full` — the two grants that read the entire expenses database and
salary data respectively; `granted_by` / `granted_at` make that review a query
rather than an investigation. The registry test runs per commit and catches a new
ungated tool on the commit that adds it.

---

## 11. Implementation order

Two repos, and the dependency runs one way only, so this splits cleanly.

1. **`local-llm-mcp` first, and it is independently shippable.** Vocabulary,
   `hasRole`/`hasPermission`, header parsing, the 12 gates, the §6.2 argument gate,
   the §8 rule 4 registry test. Deployed on its own it changes nothing observable:
   the gateway sends no grant headers yet, so every caller resolves to empty Sets
   and — by §8 rule 1 — sees only `get_server_time`. That is a **hard cutover, not
   a soft one**, and it is the one ordering risk in this plan: HRMS, iZone and EMS
   tools all disappear from chat the moment this ships and stay gone until step 3.
   Either ship steps 1–3 together, or accept a window with no MCP business tools.
2. **Gateway migration + `grants.py` + the admin route.** No behaviour change yet;
   grants can be provisioned before anything reads them, which is what makes a
   same-day cutover practical.
3. **Gateway threading** — `McpIdentity` through the three call sites and into
   `_session_headers`. This is the step that restores and then enforces access.
4. **The §10 eval matrix**, once both halves are live.

## 12. Out of scope, and what would change these decisions

- **Signing becomes mandatory if `HOST` ever becomes `0.0.0.0`.** The header trust
  model in §4 rests on the port being reachable only by the gateway. If this
  server is ever bound publicly — as the reference server is — an unsigned
  `x-user-roles` header becomes forgeable by anyone who obtains the token, and
  §3.2 must be revisited in favour of the signed variant or the callback. This is
  a deployment prerequisite, stated here rather than guessed at, the way
  `docs/external-api.md` states the `--root-path` requirement for the upload
  guard.
- **Department-scoped narrowing** (§3.1) — showing only the tools relevant to the
  current chat tab, on top of the grant — is a plausible second slice for
  relevance and schema budget. It is not authorization and must never be the only
  filter.
- **Per-list iZone scoping** (§5.2) is a tool change, needing its own design.
- **Write tools.** Everything here is read-only. The first write tool needs its
  own permission and a confirmation story, not a role.
- **Self-scoping** — "you may read your own HRMS record in full but nobody
  else's" — is a genuinely different model from grants and is not attempted. It
  would need the tool to compare `session.userEmail` against
  `Company_E_Mail`, and the header trust model to be strong enough to bear that
  weight.
