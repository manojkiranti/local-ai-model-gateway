# MCP Roles & Grants — Reference

A single-page map of the per-user MCP tool access system: where the roles live,
which table stores what, how to add a new one, and how the gateway and the MCP
server connect.

> **One-line answer:** the list of roles is **static in code** (three places).
> The database stores only **who was assigned which role**, in one table:
> `user_mcp_grants`. There is **no `mcp_roles` table** — do not look for one.

---

## 1. The two things people confuse

| | "What roles exist" (the vocabulary) | "Who holds a role" (the assignments) |
|---|---|---|
| **Kind** | Static — compiled into code | Dynamic — rows in the database |
| **Where** | Code, in 3 places (see §2) | ONE table: `user_mcp_grants` (see §3) |
| **Changes when** | You add/remove a tool → code edit + migration + deploy | An admin grants/revokes a user → API call |
| **Ships to a new server via** | The code + a migration | Nothing — each environment's assignments are its own |

If you went looking for a table listing `mcp-hrms`, `mcp-ems`, … you won't find
one. That list is code. The database only knows *who* has been given each string.

---

## 2. Where the roles are kept (STATIC, in code — 3 copies, on purpose)

The six strings exist in **three** places, deliberately. Each copy stops a
different failure: the CHECK stops a typo being **stored**, the frozenset stops
one being **honoured** by the gateway, the Node copy stops one being **enforced**
on a tool. Add a role → you edit all three (see §6).

### Copy 1 — Gateway code (Python)

`app/mcp/grants.py`

```python
# lines 30-36
ROLE_HRMS       = "mcp-hrms"        # roles: which SYSTEM you may touch (hyphenated)
ROLE_IZONE      = "mcp-izone"
ROLE_EMS        = "mcp-ems"

PERM_HRMS_FULL  = "mcp.hrms.full"   # permissions: a SHARP EDGE inside a system (dotted)
PERM_HRMS_TASKS = "mcp.hrms.tasks"
PERM_EMS_QUERY  = "mcp.ems.query"

# lines 38-41 — the sets the gateway validates and forwards against
ROLES       = frozenset({ROLE_HRMS, ROLE_IZONE, ROLE_EMS})
PERMISSIONS = frozenset({PERM_HRMS_FULL, PERM_HRMS_TASKS, PERM_EMS_QUERY})
ALL_GRANTS  = ROLES | PERMISSIONS
```

### Copy 2 — Database CHECK constraint (a rule, NOT a table)

Created by migration `alembic/versions/a3f7c21e8b04_user_mcp_grants.py`. It lists
the six strings as a validation rule on `user_mcp_grants.grant_key`. You cannot
`SELECT` from it; read it via metadata:

```sql
SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_user_mcp_grants_key';
-- CHECK (grant_key = ANY (ARRAY['mcp-ems','mcp-hrms','mcp-izone',
--                               'mcp.ems.query','mcp.hrms.full','mcp.hrms.tasks']))
```

The literal must stay in `sorted(ALL_GRANTS)` order (Python codepoint order, so
`-` before `.`), or a future `alembic revision --autogenerate` proposes a
spurious diff.

### Copy 3 — MCP server code (TypeScript, separate repo `local-llm-mcp`)

`src/auth/mcp-roles.ts`

```ts
// lines 11-19
export const MCP_ROLES = {
  HRMS:  'mcp-hrms',
  IZONE: 'mcp-izone',
  EMS:   'mcp-ems',
} as const;

export const MCP_PERMISSIONS = {
  HRMS_FULL:  'mcp.hrms.full',
  HRMS_TASKS: 'mcp.hrms.tasks',
  EMS_QUERY:  'mcp.ems.query',
} as const;
```

---

## 3. The one table (DYNAMIC — the assignments)

`user_mcp_grants` — defined in `app/mcp/models.py`, created by migration
`a3f7c21e8b04`. **This is the only table in the whole feature.** Ships empty; a
fresh deploy has zero rows and every user therefore holds no grants.

| column | type | meaning |
|---|---|---|
| `user_id` | `integer` | FK → `users.id`, **ON DELETE CASCADE** (a grant is meaningless without its user) |
| `grant_key` | `varchar(64)` | one of the six strings; the CHECK rejects anything else |
| `granted_at` | `timestamptz` | when granted; **never rewritten** on a re-grant (audit fact) |
| `granted_by` | `integer` | FK → `users.id`, **ON DELETE SET NULL** (audit survives the granter leaving) |

Primary key is `(user_id, grant_key)` — the same grant can't be stored twice for
one user.

```sql
SELECT * FROM user_mcp_grants;                    -- who holds what (empty until assigned)
SELECT grant_key, count(*) FROM user_mcp_grants GROUP BY grant_key;
```

---

## 4. How the two servers are connected

The gateway is the **only** client of the MCP server. They connect over
**streamable HTTP** and authenticate with **one shared bearer token** — the MCP
server has no user database and no login of its own. Per-user identity is carried
in headers the gateway adds on every request.

```
Browser / frontend
   │  JWT (per user)
   ▼
GATEWAY  (app/, this repo)                         MCP SERVER (local-llm-mcp)
   │  1. get_current_user  → who is this JWT?      bound to 127.0.0.1
   │  2. get_mcp_identity  → reads user_mcp_grants ── the ONLY DB read of grants
   │  3. MCPClient forwards, per request:
   │        Authorization: Bearer <shared token>
   │        x-user-email:        alice@…            createAuthenticate():
   │        x-user-roles:        mcp-hrms,mcp-ems   4. parseGrants() → session.roles / .permissions
   │        x-user-permissions:  mcp.ems.query      5. canAccess(session) runs at SESSION BUILD:
   │                                                     tools/list returns ONLY the allowed tools
   ▼                                                6. an ungranted tool is never registered at all
```

Header names are defined once on each side and must match:

- Gateway: `app/mcp/grants.py:45-47` (`EMAIL_HEADER`, `ROLES_HEADER`, `PERMISSIONS_HEADER`)
- MCP server: `src/auth/service-token.ts:54-56` (reads `x-user-email` / `x-user-roles` / `x-user-permissions`)

Values are **comma-separated, sorted**; empty fields are omitted (never sent
blank). The gateway forwards only strings in its `ROLES`/`PERMISSIONS` sets —
`McpIdentity.from_grants` intersects and drops anything else — so a stray value
in the DB is never sent, and an unknown value the MCP server does receive simply
matches no tool.

### The trust model (deployment prerequisite, not a code property)

The shared token is the boundary. The gateway holds it; its users never see it,
so a chat user **cannot forge** `x-user-roles`. `canAccess` additionally guards
against a gateway bug or a second client. **This rests entirely on the MCP server
being bound to `127.0.0.1`.** If it is ever bound to `0.0.0.0`, the header
becomes forgeable by any token holder and the transport must move to a signed
assertion or a callback.

---

## 5. Which grant maps to which tool (lives ONLY in the MCP server)

This mapping is **not** in the gateway and **not** in any table — it is the
`canAccess` line on each tool in `local-llm-mcp/src/tools/`. The gateway
deliberately keeps no copy: a second copy would drift silently ("gateway hides a
tool the MCP would allow" = a lost capability with no error anywhere).

| tool (`name:`) | file | gate |
|---|---|---|
| `get_server_time` | `basic/get-server-time.ts` | **none** — always available |
| `list_hrms_employees` | `hrms/list-employees.ts:16` | `hasRole(HRMS)` |
| `list_hrms_departments` | `hrms/list-departments.ts:15` | `hasRole(HRMS)` |
| `get_hrms_employee_details` | `hrms/employee-details.ts:31` | `hasRole(HRMS)` — the full 80+ field record additionally needs `mcp.hrms.full`, gated **inside `execute`** (`canAccess` never sees arguments) |
| `get_hrms_employee_tasks` | `hrms/employee-tasks.ts:12` | `hasBoth(HRMS, HRMS_TASKS)` |
| `list_izone_lists` | `izone/list-lists.ts:20` | `hasRole(IZONE)` |
| `list_izone_list_items` | `izone/list-items.ts:19` | `hasRole(IZONE)` |
| `list_izone_documents` | `izone/list-documents.ts:15` | `hasRole(IZONE)` |
| `search_izone_country_circulars` | `izone/search-country-circulars.ts:23` | `hasRole(IZONE)` |
| `list_ems_tables` | `ems/list-tables.ts:18` | `hasRole(EMS)` |
| `search_ems_records` | `ems/search-records.ts:20` | `hasBoth(EMS, EMS_QUERY)` |

`get_echo` and `list_examples` exist but are **not registered in production**
(behind `MCP_ENABLE_DEV_TOOLS=true`).

**A permission never implies its role.** `search_ems_records` needs
`mcp-ems` **AND** `mcp.ems.query`; `get_hrms_employee_tasks` needs `mcp-hrms`
**AND** `mcp.hrms.tasks`. Granting only the permission does nothing.

### Tool-naming convention

- Read-style verbs: `get_`, `list_`, `search_` (all current tools are read-only).
- Name must be unique across the whole server.
- Registering a tool is **not** enough to expose it — its exact name must also be
  in the gateway's `MCP_TOOL_ALLOWLIST` when `MCP_TOOL_MODE=allowlist` (the
  Docker default), and it must have a `canAccess` gate or the registry test
  fails (FastMCP's filter is fail-open — an ungated tool is visible to everyone).

---

## 6. How to ADD a new role (the full checklist)

Adding a role/permission is a coordinated change — it is **not** an API call or a
row insert, because there is no vocabulary table. In order:

1. **MCP server** (`local-llm-mcp`)
   - Add the constant to `src/auth/mcp-roles.ts` (`MCP_ROLES` or `MCP_PERMISSIONS`).
   - Put it on a tool: `canAccess: s => hasRole(s, MCP_ROLES.NEWTHING)`
     (or `hasBoth(...)` for a permission that also needs a role).
   - Add the tool's name to the grant-matrix test and to `MCP_TOOL_ALLOWLIST`.
   - Deploy the MCP server. **This step is unavoidable — a new tool is new code.**
2. **Gateway** (this repo)
   - Add the constant + set membership in `app/mcp/grants.py:30-41`.
   - New Alembic migration widening `ck_user_mcp_grants_key` to include the string
     (drop + recreate the CHECK; keep the literal in `sorted(ALL_GRANTS)` order).
   - `alembic upgrade head`, then confirm `alembic revision --autogenerate` is empty.
   - Deploy the gateway.
3. **Assign it** to users via the API (§7). Only now does anyone hold it.

Removing a role is the reverse, and note the CHECK downgrade must first delete
any rows holding the value or the new constraint won't apply.

> Considering making this dynamic (a `mcp_grant_kinds` table + admin API)? The
> tradeoff: you'd lose the typo-proof guarantee (a mistyped grant becomes
> silently inert instead of rejected), and the DB list can drift from what the
> MCP server's `canAccess` actually enforces. It also does **not** remove the MCP
> code deploy, because the tool and its gate are always code. Decide against that
> backdrop.

---

## 7. Administering assignments (the API)

All admin-only (JWT, `require_admin`). Router: `app/mcp/grants_router.py`,
mounted at `/v1/users/{user_id}/mcp-grants`.

| method / path | does | returns |
|---|---|---|
| `GET  /v1/users/{id}/mcp-grants` | list a user's grants | `200 {user_id, items:[{grant_key,granted_at,granted_by}]}` |
| `POST /v1/users/{id}/mcp-grants` | grant one (`{"grant_key":"mcp-hrms"}`) | `201` + the full list; **idempotent**, does not rewrite `granted_at` |
| `DELETE /v1/users/{id}/mcp-grants/{grant_key}` | revoke one | `204` whether or not the row existed |

Errors: `401` no/invalid JWT · `403` caller not admin · `404` unknown user ·
`422` unknown `grant_key` (names the offender) or an unexpected body field.

A **global gateway admin holds no grant implicitly** — `users.role` is never
consulted to decide grants. An admin grants themselves explicitly, which leaves
an audit row. This is deliberate (auto-conferring salary data + an expenses SQL
console on whoever runs the gateway is the escalation an audit objects to).

---

## 8. Quick answers

- **Where are the roles kept?** Code — `app/mcp/grants.py:30-40` and
  `local-llm-mcp/src/auth/mcp-roles.ts:11-19`. Plus a DB CHECK constraint.
- **Which table?** `user_mcp_grants` — but it stores *assignments*, not the role
  list.
- **Is there an `mcp_roles` table?** No.
- **Will a migration create the roles on the live server?** It creates the
  *table* and the *CHECK* (the valid list). It grants nobody anything — the table
  is empty; you assign via §7.
- **Are the six strings available on the live server automatically?** Yes — they
  ship with the code + migration, identically everywhere. Only the assignments
  differ per environment.
