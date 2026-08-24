# Role-Based MCP Tool Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gate `local-llm-mcp`'s 13 tools behind six per-user grants that this gateway stores, administers, and forwards, so that reading salary data or running SQL over the expenses database requires an explicit, audited grant.

**Architecture:** The gateway stores grants in `user_mcp_grants` and forwards them on every MCP request as `x-user-roles` / `x-user-permissions` beside the existing `x-user-email`. The MCP server parses them into its FastMCP session and enforces per tool with `canAccess`, which FastMCP applies at *session construction* — so an unauthorized tool is never registered and `tools/list` already returns exactly the authorized set. The gateway therefore keeps **no** tool→grant map of its own.

**Tech Stack:** TypeScript + FastMCP 4 + zod + `node:test` (MCP server); Python 3.10 + FastAPI + SQLAlchemy 2 + Alembic + pytest (gateway).

**Spec:** `docs/superpowers/specs/2026-08-24-mcp-role-based-tool-access-design.md`

## Global Constraints

- **Two repos.** MCP server: `/home/manoj/newlaptop/projects/node/local-llm-mcp`. Gateway: `/home/manoj/newlaptop/projects/python/local-ai-model-gateway` (this one). Never edit `../local-ai-model` (the sibling original) or `odin-marketing-mcp` (read-only reference).
- **Gateway venv only:** `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/alembic`. Python 3.10.
- **MCP server tests:** `npm test` runs `node --import tsx test/index.ts`. A new test file is invisible until it is imported from `test/index.ts` — adding the file is not enough.
- **The vocabulary is exactly six strings, verbatim:** `mcp-hrms`, `mcp-izone`, `mcp-ems`, `mcp.hrms.full`, `mcp.hrms.tasks`, `mcp.ems.query`. Roles hyphenated, permissions dotted.
- **Everything fails closed.** Absent headers, an `undefined` session, and an unknown grant string all yield *less* access, never more, and never a 500.
- **No `isSuperuser` anywhere.** Spec §3.4: a global gateway admin holds no MCP grant implicitly. Do not add the field "for parity" — an unused superuser flag is something a later contributor wires up.
- **Alembic must stay at ONE head.** Current head is `b7e1c4d92a03`. `tests/test_alembic_lineage.py` fails if a second appears.
- **Cutover risk (spec §11):** after Task 6 the MCP server refuses everything until Task 11 ships the headers. Tasks 1–11 land together, or accept a window with no HRMS/iZone/EMS tools in chat.

---

## File Structure

**MCP server (`local-llm-mcp`)**

| File | Responsibility |
|---|---|
| `src/auth/mcp-roles.ts` | **New.** The six-string vocabulary and nothing else. |
| `src/auth/access-control.ts` | **New.** `hasRole` / `hasPermission` / `hasBoth` over a session. Pure, no I/O, no cache. |
| `src/auth/service-token.ts` | **Modify.** `ServiceSession` grows two Sets; parse the two headers. |
| `src/config.ts` | **Modify.** Add `enableDevTools`. |
| `src/server.ts` | **Modify.** `createServer` takes the whole config. |
| `src/index.ts` | **Modify.** Pass the whole config. |
| `src/tools/index.ts` | **Modify.** Thread `enableDevTools` to the basic group. |
| `src/tools/basic/index.ts` | **Modify.** Register `get_echo` / `list_examples` only when dev tools are on. |
| `src/tools/hrms/*.ts` (4) | **Modify.** `canAccess`, plus the argument gate in `employee-details.ts`. |
| `src/tools/izone/*.ts` (4) | **Modify.** `canAccess`. |
| `src/tools/ems/*.ts` (2) | **Modify.** `canAccess`. |
| `test/access-control.test.ts` | **New.** Vocabulary + predicate unit tests. |
| `test/tool-gates.test.ts` | **New.** The registry rule and the §10 eval matrix. |

**Gateway**

| File | Responsibility |
|---|---|
| `app/mcp/grants.py` | **New.** Pure: vocabulary, `McpIdentity`, header serialisation. No DB, no HTTP. |
| `app/mcp/models.py` | **New.** The `user_mcp_grants` table. |
| `app/mcp/repository.py` | **New.** Data access for grants. |
| `app/mcp/schemas.py` | **New.** Request/response models for the admin route. |
| `app/mcp/grants_router.py` | **New.** `GET/POST/DELETE /v1/users/{id}/mcp-grants`. |
| `app/mcp/dependencies.py` | **New.** `get_mcp_identity` — the one place grants are loaded for a request. |
| `app/mcp/client.py` | **Modify.** `_session_headers` / `session` / `describe` take an `McpIdentity`. |
| `app/tools/router.py`, `app/mcp/router.py`, `app/chat/router.py`, `app/agent/loop.py` | **Modify.** Thread the identity instead of the bare email. |
| `alembic/versions/a3f7c21e8b04_user_mcp_grants.py` | **New.** The migration. |
| `tests/test_mcp_grants.py` | **New.** Pure-unit tests for `grants.py` and `_session_headers`. |
| `tests/test_mcp_grants_integration.py` | **New.** Table, CHECK reconciliation, repository. |
| `tests/test_mcp_grants_routes.py` | **New.** The admin route over HTTP (TestClient). |

---

# Part A — MCP server (`local-llm-mcp`)

All Part A paths are relative to `/home/manoj/newlaptop/projects/node/local-llm-mcp`.

---

### Task 1: Vocabulary and access predicates

**Files:**
- Create: `src/auth/mcp-roles.ts`
- Create: `src/auth/access-control.ts`
- Create: `test/access-control.test.ts`
- Modify: `test/index.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `MCP_ROLES` / `MCP_PERMISSIONS` const objects; `hasRole(session, roleKey): boolean`, `hasPermission(session, permissionKey): boolean`, `hasBoth(session, roleKey, permissionKey): boolean`. All three accept `ServiceSession | undefined`.

- [ ] **Step 1: Write the failing test**

Create `test/access-control.test.ts`:

```ts
import assert from 'node:assert/strict';
import test from 'node:test';
import { hasBoth, hasPermission, hasRole } from '../src/auth/access-control.js';
import { MCP_PERMISSIONS, MCP_ROLES } from '../src/auth/mcp-roles.js';
import type { ServiceSession } from '../src/auth/service-token.js';

function session(roles: string[] = [], permissions: string[] = []): ServiceSession {
  return { userEmail: 'person@example.com', roles: new Set(roles), permissions: new Set(permissions) };
}

test('the vocabulary is exactly the six agreed strings', () => {
  assert.deepEqual(Object.values(MCP_ROLES).sort(), ['mcp-ems', 'mcp-hrms', 'mcp-izone']);
  assert.deepEqual(
    Object.values(MCP_PERMISSIONS).sort(),
    ['mcp.ems.query', 'mcp.hrms.full', 'mcp.hrms.tasks'],
  );
});

test('a held role is allowed and an unheld one is not', () => {
  const s = session([MCP_ROLES.HRMS]);
  assert.equal(hasRole(s, MCP_ROLES.HRMS), true);
  assert.equal(hasRole(s, MCP_ROLES.EMS), false);
});

test('a permission is not satisfied by holding its role', () => {
  const s = session([MCP_ROLES.EMS]);
  assert.equal(hasPermission(s, MCP_PERMISSIONS.EMS_QUERY), false);
  assert.equal(hasBoth(s, MCP_ROLES.EMS, MCP_PERMISSIONS.EMS_QUERY), false);
});

test('a role is not satisfied by holding only its permission', () => {
  const s = session([], [MCP_PERMISSIONS.EMS_QUERY]);
  assert.equal(hasBoth(s, MCP_ROLES.EMS, MCP_PERMISSIONS.EMS_QUERY), false);
});

test('hasBoth requires both halves', () => {
  const s = session([MCP_ROLES.EMS], [MCP_PERMISSIONS.EMS_QUERY]);
  assert.equal(hasBoth(s, MCP_ROLES.EMS, MCP_PERMISSIONS.EMS_QUERY), true);
});

// Fail-closed rule 2: canAccess and execute can both be reached with no session.
test('an undefined session allows nothing and never throws', () => {
  assert.equal(hasRole(undefined, MCP_ROLES.HRMS), false);
  assert.equal(hasPermission(undefined, MCP_PERMISSIONS.HRMS_FULL), false);
  assert.equal(hasBoth(undefined, MCP_ROLES.EMS, MCP_PERMISSIONS.EMS_QUERY), false);
});

// Fail-closed rule 3: vocabulary skew between independently deployed sides
// must degrade, not throw.
test('an unknown grant string is simply not a match', () => {
  const s = session(['mcp-payroll'], ['mcp.payroll.write']);
  assert.equal(hasRole(s, MCP_ROLES.HRMS), false);
  assert.equal(hasRole(s, 'mcp-payroll'), true);
});

// Empty sets are the shape an absent header produces (Task 2).
test('empty grant sets allow nothing', () => {
  const s = session();
  assert.equal(hasRole(s, MCP_ROLES.HRMS), false);
  assert.equal(hasPermission(s, MCP_PERMISSIONS.HRMS_FULL), false);
});
```

Add to `test/index.ts`, after the `./auth.test.js` line:

```ts
import './access-control.test.js';
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test`
Expected: FAIL — cannot find module `../src/auth/access-control.js`.

- [ ] **Step 3: Write minimal implementation**

Create `src/auth/mcp-roles.ts`:

```ts
// The complete MCP grant vocabulary. Roles say WHICH SYSTEM a caller may touch;
// permissions say WHICH SHARP EDGE inside it. Roles are hyphenated, permissions
// dotted, following the odin-marketing-mcp convention this was modelled on.
//
// The gateway holds a second copy of these six strings (app/mcp/grants.py) and
// Postgres a third (ck_user_mcp_grants_key). That is deliberate, the same way
// ck_api_keys_scopes and policy.ALL_SCOPES are: the CHECK stops a typo being
// STORED, the frozenset stops one being HONOURED, and this file stops one being
// ENFORCED. Adding a grant means editing all three.
export const MCP_ROLES = {
  HRMS: 'mcp-hrms',
  IZONE: 'mcp-izone',
  EMS: 'mcp-ems',
} as const;

export const MCP_PERMISSIONS = {
  HRMS_FULL: 'mcp.hrms.full',
  HRMS_TASKS: 'mcp.hrms.tasks',
  EMS_QUERY: 'mcp.ems.query',
} as const;
```

Create `src/auth/access-control.ts`:

```ts
import type { ServiceSession } from './service-token.js';

/**
 * Whether a session holds a grant. Pure — no HTTP, no cache, no database.
 *
 * Deliberately NOT a port of odin-marketing-mcp's file of the same name. That
 * one calls its backend to resolve roles by email, because it is exposed to the
 * public internet and has no trusted front door. This server is bound to
 * localhost with the gateway as its only client, so the gateway asserts the
 * grants in headers (see service-token.ts) and there is nothing to fetch.
 *
 * Two deliberate departures from that reference, both recorded in the design:
 *
 *  - There is no `isSuperuser` bypass. A global gateway admin holds no MCP
 *    grant implicitly; it must be granted explicitly so there is an audit row.
 *  - `session.userEmail` is NOT a condition. The grant sets are the authority,
 *    so requiring the email too would add a second condition whose failure is
 *    indistinguishable from "no grants" — the email is for logging refusals,
 *    not for deciding them.
 *
 * Every predicate takes `ServiceSession | undefined` because FastMCP types
 * `Context.session` as possibly undefined and `canAccess` can be reached with a
 * falsy auth. None of them may throw, and none may pass on absence.
 */
export function hasRole(session: ServiceSession | undefined, roleKey: string): boolean {
  return session?.roles?.has(roleKey) ?? false;
}

export function hasPermission(
  session: ServiceSession | undefined,
  permissionKey: string,
): boolean {
  return session?.permissions?.has(permissionKey) ?? false;
}

/**
 * A permission never implies its role, so the two dangerous tools require both
 * halves explicitly. `search_ems_records` is useless without `list_ems_tables`
 * anyway — its own description says to call that first for real table names —
 * so granting the SQL permission alone would be a trap, not a capability.
 */
export function hasBoth(
  session: ServiceSession | undefined,
  roleKey: string,
  permissionKey: string,
): boolean {
  return hasRole(session, roleKey) && hasPermission(session, permissionKey);
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm test`
Expected: the 8 new tests PASS. `npm run typecheck` also passes.

Note: `access-control.ts` imports the `ServiceSession` type that Task 2 will widen. TypeScript resolves the *current* shape (no `roles`/`permissions`), so the optional chaining above compiles but the test's `session()` helper will fail typecheck until Task 2. If `npm run typecheck` complains about `roles` not existing on `ServiceSession`, do Task 2 next and re-run — do not "fix" it by loosening the type.

- [ ] **Step 5: Commit**

```bash
git add src/auth/mcp-roles.ts src/auth/access-control.ts test/access-control.test.ts test/index.ts
git commit -m "feat(auth): MCP grant vocabulary and access predicates"
```

---

### Task 2: Parse the grant headers into the session

**Files:**
- Modify: `src/auth/service-token.ts`
- Modify: `test/auth.test.ts`

**Interfaces:**
- Consumes: nothing from Task 1 at runtime (Task 1's predicates read the type this task defines).
- Produces: `ServiceSession = {userEmail: string | null, roles: Set<string>, permissions: Set<string>, [key: string]: unknown}`; `createAuthenticate(serviceToken)` unchanged in signature.

- [ ] **Step 1: Write the failing test**

`test/auth.test.ts` already asserts the whole session with `assert.deepEqual(..., { userEmail: null })`. Those two assertions **must be updated** — the session now has two more fields, so they would fail for the right reason but the wrong test. Replace the first two tests and append the new ones:

```ts
test('valid bearer token authenticates at service level', async () => {
  const authenticate = createAuthenticate('shared-secret');
  assert.deepEqual(await authenticate(request({ authorization: 'Bearer shared-secret' })), {
    userEmail: null,
    roles: new Set(),
    permissions: new Set(),
  });
});

test('optional X-User-Email is attached to the session', async () => {
  const authenticate = createAuthenticate('shared-secret');
  assert.deepEqual(await authenticate(request({
    authorization: 'Bearer shared-secret',
    'x-user-email': 'person@example.com',
  })), {
    userEmail: 'person@example.com',
    roles: new Set(),
    permissions: new Set(),
  });
});

test('grant headers are parsed into sets', async () => {
  const authenticate = createAuthenticate('shared-secret');
  const s = await authenticate(request({
    authorization: 'Bearer shared-secret',
    'x-user-email': 'person@example.com',
    'x-user-roles': 'mcp-hrms,mcp-ems',
    'x-user-permissions': 'mcp.ems.query',
  }));
  assert.deepEqual(s.roles, new Set(['mcp-hrms', 'mcp-ems']));
  assert.deepEqual(s.permissions, new Set(['mcp.ems.query']));
});

test('whitespace and empty entries are discarded', async () => {
  const authenticate = createAuthenticate('shared-secret');
  const s = await authenticate(request({
    authorization: 'Bearer shared-secret',
    'x-user-roles': ' mcp-hrms , , mcp-izone ,',
  }));
  assert.deepEqual(s.roles, new Set(['mcp-hrms', 'mcp-izone']));
});

// Fail-closed rule 1: absent headers must mean NO grants, never all of them.
test('absent grant headers yield empty sets, not unrestricted access', async () => {
  const authenticate = createAuthenticate('shared-secret');
  const s = await authenticate(request({ authorization: 'Bearer shared-secret' }));
  assert.equal(s.roles.size, 0);
  assert.equal(s.permissions.size, 0);
});

test('an empty grant header yields an empty set', async () => {
  const authenticate = createAuthenticate('shared-secret');
  const s = await authenticate(request({
    authorization: 'Bearer shared-secret',
    'x-user-roles': '',
  }));
  assert.equal(s.roles.size, 0);
});

// The token check must still run FIRST: grant headers on a bad token are
// worthless, and parsing them before rejecting would be work done for an
// unauthenticated caller.
test('grant headers do not rescue a bad token', async () => {
  const authenticate = createAuthenticate('shared-secret');
  await assert.rejects(
    authenticate(request({ authorization: 'Bearer wrong', 'x-user-roles': 'mcp-ems' })),
    (error: unknown) => error instanceof Response && error.status === 401,
  );
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test`
Expected: FAIL — `s.roles` is undefined; the two deepEqual assertions fail on the missing keys.

- [ ] **Step 3: Write minimal implementation**

In `src/auth/service-token.ts`, widen the type and add the parser:

```ts
export type ServiceSession = {
  userEmail: string | null;
  /** Which systems this caller may touch. Empty means none. */
  roles: Set<string>;
  /** Which sharp edges inside them. Empty means none. */
  permissions: Set<string>;
  [key: string]: unknown;
};

/**
 * Parse a comma-separated grant header into a Set.
 *
 * Unknown values are kept verbatim rather than validated against the
 * vocabulary: this server and the gateway deploy independently, so a gateway on
 * a newer vocabulary will send grants this build has never heard of. An unknown
 * grant simply matches no `canAccess`, which is the correct degradation.
 * Rejecting it would turn a harmless version skew into a 401.
 */
function parseGrants(value: string | string[] | undefined): Set<string> {
  const raw = firstHeader(value) ?? '';
  return new Set(
    raw
      .split(',')
      .map((part) => part.trim())
      .filter((part) => part.length > 0),
  );
}
```

and change the returned session (the token check above it is unchanged, and stays first):

```ts
    return {
      userEmail: firstHeader(request.headers['x-user-email']) ?? null,
      roles: parseGrants(request.headers['x-user-roles']),
      permissions: parseGrants(request.headers['x-user-permissions']),
    };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm test && npm run typecheck`
Expected: all of `auth.test.ts` and `access-control.test.ts` PASS; typecheck clean (this resolves the Task 1 note).

- [ ] **Step 5: Commit**

```bash
git add src/auth/service-token.ts test/auth.test.ts
git commit -m "feat(auth): parse x-user-roles/x-user-permissions into the session"
```

---

### Task 3: Gate the three HRMS tools that need only a role

**Files:**
- Modify: `src/tools/hrms/list-employees.ts`
- Modify: `src/tools/hrms/list-departments.ts`
- Modify: `src/tools/hrms/employee-details.ts`
- Modify: `src/tools/hrms/employee-tasks.ts`

**Interfaces:**
- Consumes: `hasRole`, `hasBoth` (Task 1); `ServiceSession` (Task 2).
- Produces: nothing new — four `addTool` calls gain a `canAccess` property.

`get_hrms_employee_details` gets `canAccess` here; its *argument* gate is Task 4. `get_hrms_employee_tasks` needs both halves because it reads who is requesting a personal loan, a salary advance, or resigning.

- [ ] **Step 1: Write the failing test**

Create `test/tool-gates.test.ts`. This file grows in Tasks 5, 6 and 7; start it with the shared harness plus the HRMS cases.

```ts
import assert from 'node:assert/strict';
import test from 'node:test';
import type { FastMCP } from 'fastmcp';
import { MCP_PERMISSIONS, MCP_ROLES } from '../src/auth/mcp-roles.js';
import type { ServiceSession } from '../src/auth/service-token.js';
import { registerTools } from '../src/tools/index.js';

type Registered = {
  name: string;
  canAccess?: (session: ServiceSession | undefined) => boolean;
};

/**
 * Collect every tool `registerTools` registers.
 *
 * FastMCP keeps its tool list in a private field with no public accessor, so we
 * pass a stand-in that only implements `addTool` — the sole method
 * `registerTools` uses. This reads the real registration code rather than a
 * copy of it, which is the whole point: a gate added to the wrong tool, or
 * forgotten, shows up here.
 */
export function collectTools(options: { enableDevTools?: boolean } = {}): Registered[] {
  const collected: Registered[] = [];
  const stand_in = { addTool: (tool: Registered) => collected.push(tool) };
  registerTools(stand_in as unknown as FastMCP<ServiceSession>, {
    enableDevTools: options.enableDevTools ?? false,
  });
  return collected;
}

export function sessionWith(roles: string[] = [], permissions: string[] = []): ServiceSession {
  return { userEmail: 'person@example.com', roles: new Set(roles), permissions: new Set(permissions) };
}

export function visibleTo(session: ServiceSession | undefined): Set<string> {
  // Mirrors FastMCP's own filter in #createSession:
  //   tool.canAccess ? tool.canAccess(auth) : true
  return new Set(
    collectTools()
      .filter((tool) => (tool.canAccess ? tool.canAccess(session) : true))
      .map((tool) => tool.name),
  );
}

function gateOf(name: string): (session: ServiceSession | undefined) => boolean {
  const tool = collectTools().find((t) => t.name === name);
  assert.ok(tool, `no tool named ${name} is registered`);
  assert.ok(tool.canAccess, `${name} declares no canAccess`);
  return tool.canAccess;
}

for (const name of ['list_hrms_employees', 'list_hrms_departments', 'get_hrms_employee_details']) {
  test(`${name} requires mcp-hrms and nothing more`, () => {
    const gate = gateOf(name);
    assert.equal(gate(sessionWith([MCP_ROLES.HRMS])), true);
    assert.equal(gate(sessionWith()), false);
    assert.equal(gate(sessionWith([MCP_ROLES.EMS])), false);
    assert.equal(gate(undefined), false);
  });
}

test('get_hrms_employee_tasks requires the role AND the permission', () => {
  const gate = gateOf('get_hrms_employee_tasks');
  assert.equal(gate(sessionWith([MCP_ROLES.HRMS], [MCP_PERMISSIONS.HRMS_TASKS])), true);
  // Holding the role alone is the case that matters: pending loan, salary
  // advance and resignation counts are not directory data.
  assert.equal(gate(sessionWith([MCP_ROLES.HRMS])), false);
  assert.equal(gate(sessionWith([], [MCP_PERMISSIONS.HRMS_TASKS])), false);
  assert.equal(gate(undefined), false);
});
```

Add to `test/index.ts`:

```ts
import './tool-gates.test.js';
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test`
Expected: FAIL — `registerTools` takes one argument (the second is Task 6), and every `canAccess` assertion fails with "declares no canAccess".

To keep this task independently runnable before Task 6 exists, `registerTools`' second parameter is added here as **optional** and ignored; Task 6 gives it meaning. Change `src/tools/index.ts` now:

```ts
export type ToolRegistrationOptions = {
  /** Register the development-only basic tools. Off in production. */
  enableDevTools?: boolean;
};

export function registerTools(
  server: FastMCP<ServiceSession>,
  options: ToolRegistrationOptions = {},
): void {
  registerBasicTools(server, options);
  registerHrmsTools(server);
  registerIzoneTools(server);
  registerEmsTools(server);
}
```

and give `registerBasicTools` a matching ignored-for-now parameter in `src/tools/basic/index.ts`:

```ts
export function registerBasicTools(
  server: FastMCP<ServiceSession>,
  _options: { enableDevTools?: boolean } = {},
): void {
  registerGetServerTime(server);
  registerGetEcho(server);
  registerListExamples(server);
}
```

- [ ] **Step 3: Write minimal implementation**

In each of the four HRMS tool files, add the import and the `canAccess` property. `canAccess` goes immediately after `name`, matching the reference server's placement.

`src/tools/hrms/list-employees.ts`, `list-departments.ts`, and `employee-details.ts` each get:

```ts
import { hasRole } from '../../auth/access-control.js';
import { MCP_ROLES } from '../../auth/mcp-roles.js';
```

and inside their `server.addTool({ ... })`, directly after the `name:` line:

```ts
    canAccess: (session) => hasRole(session, MCP_ROLES.HRMS),
```

`src/tools/hrms/employee-tasks.ts` gets:

```ts
import { hasBoth } from '../../auth/access-control.js';
import { MCP_PERMISSIONS, MCP_ROLES } from '../../auth/mcp-roles.js';
```

and:

```ts
    canAccess: (session) => hasBoth(session, MCP_ROLES.HRMS, MCP_PERMISSIONS.HRMS_TASKS),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm test && npm run typecheck`
Expected: the 4 new gate tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tools/hrms/ src/tools/index.ts src/tools/basic/index.ts test/tool-gates.test.ts test/index.ts
git commit -m "feat(hrms): gate the HRMS tools behind mcp-hrms and mcp.hrms.tasks"
```

---

### Task 4: The full-detail argument gate, and the `employeeNo` second door

**Files:**
- Modify: `src/tools/hrms/employee-details.ts`
- Create: `test/hrms-full-gate.test.ts`
- Modify: `test/index.ts`

**Interfaces:**
- Consumes: `hasPermission` (Task 1); the `canAccess` added in Task 3.
- Produces: `FULL_WITHHELD_NOTE`, exported from `src/tools/hrms/employee-details.ts` so the test asserts the same constant the tool emits rather than a copy of its wording.

**This is the subtle task.** `canAccess` receives the session and never the arguments, so `mcp.hrms.full` cannot be expressed as a gate — the tool stays visible to every `mcp-hrms` holder and the decision moves inside `execute`. And the existing code has **two** ways to ask for full detail:

```js
const wantsFull = full || Boolean(employeeNo);
```

Gating the `full` parameter alone leaves `employeeNo` wide open. The gate belongs on `wantsFull`. The fix is *not* to refuse `employeeNo` — looking up EMP-1001's name, title and branch is ordinary directory use — so an ungranted caller gets the 11-field summary plus an explicit note.

- [ ] **Step 1: Write the failing test**

Create `test/hrms-full-gate.test.ts`:

```ts
import assert from 'node:assert/strict';
import test from 'node:test';
import type { FastMCP } from 'fastmcp';
import { MCP_PERMISSIONS, MCP_ROLES } from '../src/auth/mcp-roles.js';
import type { ServiceSession } from '../src/auth/service-token.js';
import { FULL_WITHHELD_NOTE, registerGetHrmsEmployeeDetails } from '../src/tools/hrms/employee-details.js';
import { SUMMARY_FIELDS } from '../src/tools/hrms/project.js';

type Executor = (args: Record<string, unknown>, context: { session?: ServiceSession }) => Promise<string>;

function theTool(): { execute: Executor } {
  let captured: { execute: Executor } | undefined;
  const stand_in = { addTool: (tool: { execute: Executor }) => { captured = tool; } };
  registerGetHrmsEmployeeDetails(stand_in as unknown as FastMCP<ServiceSession>);
  assert.ok(captured, 'the tool did not register');
  return captured;
}

function session(permissions: string[] = []): ServiceSession {
  return {
    userEmail: 'person@example.com',
    roles: new Set([MCP_ROLES.HRMS]),
    permissions: new Set(permissions),
  };
}

// Defaults the zod schema would normally supply; the executor is called
// directly here, so they are passed explicitly.
const BASE = { limit: 10, offset: 0, full: false };

async function run(args: Record<string, unknown>, permissions: string[] = []) {
  const body = await theTool().execute({ ...BASE, ...args }, { session: session(permissions) });
  return JSON.parse(body) as Record<string, unknown> & { value: Array<Record<string, unknown>> };
}

test('full=true without mcp.hrms.full returns the summary and says so', async () => {
  const out = await run({ full: true });
  assert.equal(out.fullDetailWithheld, FULL_WITHHELD_NOTE);
  const fields = Object.keys(out.value[0]).sort();
  assert.deepEqual(fields, [...SUMMARY_FIELDS].sort());
});

// THE SECOND DOOR. `wantsFull = full || Boolean(employeeNo)`, so gating only
// the `full` parameter leaves this path returning all 80+ fields.
test('employeeNo without mcp.hrms.full returns the summary and says so', async () => {
  const out = await run({ employeeNo: 'EMP-1001' });
  assert.equal(out.fullDetailWithheld, FULL_WITHHELD_NOTE);
  const fields = Object.keys(out.value[0]).sort();
  assert.deepEqual(fields, [...SUMMARY_FIELDS].sort());
});

test('the directory lookup itself still works without the permission', async () => {
  // Withholding full detail must not withhold the RECORD. Looking up EMP-1001's
  // name and branch is ordinary directory use.
  const out = await run({ employeeNo: 'EMP-1001' });
  assert.ok(out.value.length >= 1, 'the employee lookup returned nothing at all');
});

test('full=true with mcp.hrms.full returns more than the summary', async () => {
  const out = await run({ full: true }, [MCP_PERMISSIONS.HRMS_FULL]);
  assert.equal(out.fullDetailWithheld, undefined);
  assert.ok(
    Object.keys(out.value[0]).length > SUMMARY_FIELDS.length,
    'granted caller did not receive the full field set',
  );
});

test('employeeNo with mcp.hrms.full returns more than the summary', async () => {
  const out = await run({ employeeNo: 'EMP-1001' }, [MCP_PERMISSIONS.HRMS_FULL]);
  assert.equal(out.fullDetailWithheld, undefined);
  assert.ok(Object.keys(out.value[0]).length > SUMMARY_FIELDS.length);
});

test('a summary-only request carries no note at all', async () => {
  // An always-present warning trains the model to ignore it — the same reason
  // TRUNCATION_NOTE is absent when nothing was dropped.
  const out = await run({ department: 'Engineering' });
  assert.equal(out.fullDetailWithheld, undefined);
});

test('an absent session fails closed to the summary', async () => {
  const out = await theTool().execute({ ...BASE, full: true }, {});
  const parsed = JSON.parse(out) as Record<string, unknown>;
  assert.equal(parsed.fullDetailWithheld, FULL_WITHHELD_NOTE);
});

test('the note tells the model to report a permission limit, not a missing field', () => {
  // If the model reports "that data is unavailable", the user concludes the
  // bank does not hold it rather than that they may not see it.
  assert.match(FULL_WITHHELD_NOTE, /mcp\.hrms\.full/);
  assert.match(FULL_WITHHELD_NOTE, /HR access/i);
});
```

Add to `test/index.ts`:

```ts
import './hrms-full-gate.test.js';
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test`
Expected: FAIL — `FULL_WITHHELD_NOTE` is not exported from `employee-details.ts`.

- [ ] **Step 3: Write minimal implementation**

In `src/tools/hrms/employee-details.ts`:

Add the imports:

```ts
import { hasPermission } from '../../auth/access-control.js';
import { MCP_PERMISSIONS, MCP_ROLES } from '../../auth/mcp-roles.js';
```

(`MCP_ROLES` and `hasRole` are already imported from Task 3 — keep both import lines merged.)

Add the exported constant above `registerGetHrmsEmployeeDetails`:

```ts
/**
 * Announced when full detail was asked for and withheld.
 *
 * The reduction must announce itself, for the same reason agent/loop.py appends
 * `[TRUNCATED …]` and read_image emits a `PARTIAL:` line: a quiet downgrade
 * reads to the model as a complete answer, and it then tells the user the field
 * does not exist rather than that they may not see it. The last sentence is
 * doing that work and is not decoration.
 */
export const FULL_WITHHELD_NOTE =
  'Full HRMS detail was withheld: this account does not hold the mcp.hrms.full permission. ' +
  'The compact field summary is returned instead. Tell the user that full employee detail ' +
  'requires HR access — do NOT report the missing fields as unavailable, empty, or not held ' +
  'by the bank.';
```

Extend the tool's `description` so the requirement is stated statically rather than discovered by a failed call. Append to the existing description string, before the closing `,`:

```ts
      'Full detail (full=true, or passing employeeNo) additionally requires the mcp.hrms.full ' +
      'permission; without it the compact summary is returned and the response says so in ' +
      'fullDetailWithheld. ' +
```

Change the `execute` signature to receive the context, and gate on `wantsFull`:

```ts
    execute: async ({
      employeeNo,
      fullName,
      department,
      province,
      branch,
      search,
      full,
      limit,
      offset,
    }, { session }) => {
      // BOTH doors. `employeeNo` implies full detail, so gating the `full`
      // parameter alone would leave every 80+ field record reachable by
      // employee number. The gate is on wantsFull, never on `full`.
      const wantsFull = full || Boolean(employeeNo);
      const maySeeFull = hasPermission(session, MCP_PERMISSIONS.HRMS_FULL);
      const returnFull = wantsFull && maySeeFull;
      // Absent only when nothing was withheld: an unconditional warning trains
      // the model to ignore it.
      const withheld = wantsFull && !maySeeFull
        ? { fullDetailWithheld: FULL_WITHHELD_NOTE }
        : {};
```

Then replace the three uses of `wantsFull` in the response builders with `returnFull`, and spread `withheld` into each `meta`. The live branch becomes:

```ts
      if (isHrmsConfigured()) {
        try {
          const { page, hasMore } = await fetchEmployeePage(filters);
          return buildOffsetPagedResponse({
            meta: { source: 'hrms', ...withheld },
            page: returnFull ? page : page.map(toSummary),
            hasMore,
            limit,
            offset,
          });
        } catch (error) {
          const message = error instanceof Error ? error.message : 'Unknown HRMS error.';
          return JSON.stringify({ source: 'hrms', error: message, count: 0, value: [] }, null, 2);
        }
      }
```

and the sample branch:

```ts
      const matched = listSampleEmployees({ ...filters, limit: limit + 1 });
      const { page, hasMore } = paginateSample(matched, limit);
      return buildOffsetPagedResponse({
        meta: { source: 'sample', ...withheld },
        page: returnFull ? page : page.map(toSummary),
        hasMore,
        limit,
        offset,
      });
```

`meta` is spread into the JSON top level by `buildOffsetPagedResponse`, so `fullDetailWithheld` appears as a sibling of `count`/`value` — where the model reads it, not buried per row.

- [ ] **Step 4: Run test to verify it passes**

Run: `npm test && npm run typecheck`
Expected: the 8 new tests PASS. If the "granted caller did not receive the full field set" assertion fails, the sample data's records may have fewer keys than `SUMMARY_FIELDS` — check `src/tools/hrms/sample-data.ts` and compare against `types.ts`'s `Employee` rather than weakening the assertion.

- [ ] **Step 5: Commit**

```bash
git add src/tools/hrms/employee-details.ts test/hrms-full-gate.test.ts test/index.ts
git commit -m "feat(hrms): gate full employee detail on mcp.hrms.full, closing the employeeNo door"
```

---

### Task 5: Gate the iZone and EMS tools

**Files:**
- Modify: `src/tools/izone/list-lists.ts`, `list-items.ts`, `list-documents.ts`, `search-country-circulars.ts`
- Modify: `src/tools/ems/list-tables.ts`, `search-records.ts`
- Modify: `test/tool-gates.test.ts`

**Interfaces:**
- Consumes: `hasRole`, `hasBoth` (Task 1); the `gateOf`/`sessionWith` harness (Task 3).
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Append to `test/tool-gates.test.ts`:

```ts
for (const name of [
  'list_izone_lists',
  'list_izone_list_items',
  'list_izone_documents',
  'search_izone_country_circulars',
]) {
  test(`${name} requires mcp-izone`, () => {
    const gate = gateOf(name);
    assert.equal(gate(sessionWith([MCP_ROLES.IZONE])), true);
    assert.equal(gate(sessionWith()), false);
    assert.equal(gate(sessionWith([MCP_ROLES.HRMS])), false);
    assert.equal(gate(undefined), false);
  });
}

test('list_ems_tables requires only mcp-ems', () => {
  const gate = gateOf('list_ems_tables');
  assert.equal(gate(sessionWith([MCP_ROLES.EMS])), true);
  assert.equal(gate(sessionWith()), false);
  assert.equal(gate(undefined), false);
});

test('search_ems_records requires mcp-ems AND mcp.ems.query', () => {
  const gate = gateOf('search_ems_records');
  assert.equal(gate(sessionWith([MCP_ROLES.EMS], [MCP_PERMISSIONS.EMS_QUERY])), true);
  // The whole point: mcp-ems alone is schema discovery, not a SQL console over
  // the entire expenses database.
  assert.equal(gate(sessionWith([MCP_ROLES.EMS])), false);
  assert.equal(gate(sessionWith([], [MCP_PERMISSIONS.EMS_QUERY])), false);
  assert.equal(gate(undefined), false);
});
```

Note `gateOf`, `sessionWith`, `MCP_ROLES` and `MCP_PERMISSIONS` are already in scope from Task 3's version of this file — do not re-import or redefine them.

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test`
Expected: FAIL — 6 assertions of "declares no canAccess".

- [ ] **Step 3: Write minimal implementation**

Each of the four iZone files gets:

```ts
import { hasRole } from '../../auth/access-control.js';
import { MCP_ROLES } from '../../auth/mcp-roles.js';
```

and, directly after its `name:` line:

```ts
    canAccess: (session) => hasRole(session, MCP_ROLES.IZONE),
```

`src/tools/ems/list-tables.ts` gets the same two imports and:

```ts
    canAccess: (session) => hasRole(session, MCP_ROLES.EMS),
```

`src/tools/ems/search-records.ts` gets:

```ts
import { hasBoth } from '../../auth/access-control.js';
import { MCP_PERMISSIONS, MCP_ROLES } from '../../auth/mcp-roles.js';
```

and:

```ts
    canAccess: (session) => hasBoth(session, MCP_ROLES.EMS, MCP_PERMISSIONS.EMS_QUERY),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm test && npm run typecheck`
Expected: the 6 new tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tools/izone/ src/tools/ems/ test/tool-gates.test.ts
git commit -m "feat(tools): gate iZone behind mcp-izone and EMS SQL behind mcp.ems.query"
```

---

### Task 6: Production tool registration, and the ungated-tool guard

**Files:**
- Modify: `src/config.ts`
- Modify: `src/server.ts`
- Modify: `src/index.ts`
- Modify: `src/tools/basic/index.ts`
- Modify: `test/tool-gates.test.ts`
- Modify: `test/config.test.ts`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `registerTools(server, options)` (Task 3's optional parameter, given meaning here).
- Produces: `ServerConfig.enableDevTools: boolean`; `createServer(config: ServerConfig)` — **a signature change**, from `createServer(serviceToken: string)`.

FastMCP's filter is `tool.canAccess ? tool.canAccess(auth) : true`, so **a tool with no `canAccess` is visible to everyone.** The default is permissive and a forgotten gate is invisible in every log on both sides. That is what the registry test below exists for; it is not a style check.

- [ ] **Step 1: Write the failing test**

Append to `test/tool-gates.test.ts`:

```ts
/**
 * Tools that are deliberately reachable by every authenticated caller.
 * Adding a name here is a security decision and should be reviewed as one.
 */
const PUBLIC_TOOLS = new Set(['get_server_time']);

test('every registered tool declares canAccess unless explicitly public', () => {
  // FastMCP's filter is `tool.canAccess ? tool.canAccess(auth) : true`, so a
  // tool without a gate is world-readable and nothing anywhere reports it.
  const ungated = collectTools()
    .filter((tool) => !tool.canAccess && !PUBLIC_TOOLS.has(tool.name))
    .map((tool) => tool.name);
  assert.deepEqual(ungated, [], `ungated tools: ${ungated.join(', ')}`);
});

test('the development-only basic tools are not registered in production', () => {
  const names = collectTools().map((t) => t.name);
  assert.equal(names.includes('get_echo'), false);
  assert.equal(names.includes('list_examples'), false);
  assert.equal(names.includes('get_server_time'), true);
});

test('the development tools come back when explicitly enabled', () => {
  const names = collectTools({ enableDevTools: true }).map((t) => t.name);
  assert.equal(names.includes('get_echo'), true);
  assert.equal(names.includes('list_examples'), true);
});

test('production registers exactly eleven tools', () => {
  // 13 tools exist; get_echo and list_examples are development harness only.
  assert.equal(collectTools().length, 11);
});
```

Append to `test/config.test.ts`:

```ts
test('dev tools are off unless explicitly enabled', () => {
  assert.equal(getServerConfig({ MCP_SERVICE_TOKEN: 't' }).enableDevTools, false);
  assert.equal(
    getServerConfig({ MCP_SERVICE_TOKEN: 't', MCP_ENABLE_DEV_TOOLS: 'false' }).enableDevTools,
    false,
  );
  assert.equal(
    getServerConfig({ MCP_SERVICE_TOKEN: 't', MCP_ENABLE_DEV_TOOLS: 'true' }).enableDevTools,
    true,
  );
  // Anything that is not exactly "true" is off — an env var set to "1" or "yes"
  // must not silently enable a non-production surface.
  assert.equal(
    getServerConfig({ MCP_SERVICE_TOKEN: 't', MCP_ENABLE_DEV_TOOLS: '1' }).enableDevTools,
    false,
  );
});
```

Check the existing import line in `test/config.test.ts`; if `getServerConfig` is imported differently there, match the file's own convention.

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test`
Expected: FAIL — `enableDevTools` is not on `ServerConfig`; `get_echo` is still registered; the count is 13.

- [ ] **Step 3: Write minimal implementation**

`src/config.ts` — extend the type and the parser:

```ts
export type ServerConfig = {
  serviceToken: string;
  host: string;
  port: number;
  endpoint: '/mcp';
  /**
   * Register get_echo and list_examples. Off by default: they are a
   * development harness, and every registered tool costs schema tokens in
   * every turn's prompt on the gateway side.
   */
  enableDevTools: boolean;
};
```

and in the returned object:

```ts
    enableDevTools: env.MCP_ENABLE_DEV_TOOLS?.trim().toLowerCase() === 'true',
```

`src/tools/basic/index.ts` — give the parameter meaning:

```ts
export function registerBasicTools(
  server: FastMCP<ServiceSession>,
  options: { enableDevTools?: boolean } = {},
): void {
  // Ungated on purpose: the server clock is not privileged information, and it
  // gives an unprovisioned caller one working tool so a misconfigured
  // deployment is distinguishable from an unreachable one.
  registerGetServerTime(server);
  if (options.enableDevTools) {
    registerGetEcho(server);
    registerListExamples(server);
  }
}
```

`src/server.ts` — take the whole config:

```ts
export function createServer(config: ServerConfig) {
  const server = new FastMCP<ServiceSession>({
    name: 'local-llm-tools',
    version: '1.0.0',
    authenticate: createAuthenticate(config.serviceToken),
  });
  registerTools(server, { enableDevTools: config.enableDevTools });
  return server;
}
```

with `import type { ServerConfig } from './config.js';` added.

`src/index.ts` — update the one call site:

```ts
const server = createServer(config);
```

`.env.example` — append:

```
# Register the development-only basic tools (get_echo, list_examples).
# Leave unset in production: every registered tool costs schema tokens in the
# gateway's prompt on every turn. Only the exact string "true" enables them.
MCP_ENABLE_DEV_TOOLS=
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm test && npm run typecheck && npm run build`
Expected: all tests PASS, typecheck clean, build clean. `npm run build` matters here because `createServer`'s signature changed and `src/index.ts` is the only caller.

- [ ] **Step 5: Commit**

```bash
git add src/config.ts src/server.ts src/index.ts src/tools/basic/index.ts test/ .env.example
git commit -m "feat(tools): drop dev tools from production and guard against ungated tools"
```

---

### Task 7: The grant × tool eval matrix

**Files:**
- Create: `test/grant-matrix.test.ts`
- Modify: `test/index.ts`

**Interfaces:**
- Consumes: `collectTools`, `sessionWith`, `visibleTo` (exported from `test/tool-gates.test.ts` in Task 3).
- Produces: nothing — this is the spec §10 eval.

Six identities × all 13 tool names = 78 assertions from one table, scored as exact set equality. All 13 names, not the 11 registered: the two dev tools must be absent for every identity, so the matrix doubles as the guard that Task 6's drop stays dropped. Target is **100%** — this is a boundary, not a judgment, so anything below 100% is a defect rather than a score.

- [ ] **Step 1: Write the failing test**

Create `test/grant-matrix.test.ts`:

```ts
import assert from 'node:assert/strict';
import test from 'node:test';
import { sessionWith, visibleTo } from './tool-gates.test.js';

/** Every tool name that exists, including the two dev-only ones. */
const ALL_TOOLS = [
  'get_server_time',
  'get_echo',
  'list_examples',
  'list_hrms_employees',
  'get_hrms_employee_details',
  'get_hrms_employee_tasks',
  'list_hrms_departments',
  'list_izone_lists',
  'list_izone_list_items',
  'list_izone_documents',
  'search_izone_country_circulars',
  'list_ems_tables',
  'search_ems_records',
] as const;

const MATRIX: Array<{
  label: string;
  grants: { roles: string[]; permissions: string[] };
  expected: string[];
}> = [
  {
    label: 'none',
    grants: { roles: [], permissions: [] },
    expected: ['get_server_time'],
  },
  {
    label: 'hrms_directory',
    grants: { roles: ['mcp-hrms'], permissions: [] },
    // get_hrms_employee_details IS visible: mcp.hrms.full gates its argument,
    // not the tool. get_hrms_employee_tasks is not — it needs its permission.
    expected: [
      'get_server_time',
      'list_hrms_employees',
      'get_hrms_employee_details',
      'list_hrms_departments',
    ],
  },
  {
    label: 'hrms_full',
    grants: { roles: ['mcp-hrms'], permissions: ['mcp.hrms.full', 'mcp.hrms.tasks'] },
    expected: [
      'get_server_time',
      'list_hrms_employees',
      'get_hrms_employee_details',
      'get_hrms_employee_tasks',
      'list_hrms_departments',
    ],
  },
  {
    label: 'izone',
    grants: { roles: ['mcp-izone'], permissions: [] },
    expected: [
      'get_server_time',
      'list_izone_lists',
      'list_izone_list_items',
      'list_izone_documents',
      'search_izone_country_circulars',
    ],
  },
  {
    label: 'ems_schema',
    grants: { roles: ['mcp-ems'], permissions: [] },
    expected: ['get_server_time', 'list_ems_tables'],
  },
  {
    label: 'ems_query',
    grants: { roles: ['mcp-ems'], permissions: ['mcp.ems.query'] },
    expected: ['get_server_time', 'list_ems_tables', 'search_ems_records'],
  },
];

for (const row of MATRIX) {
  const visible = () => visibleTo(sessionWith(row.grants.roles, row.grants.permissions));

  test(`identity "${row.label}" sees exactly its expected tool set`, () => {
    assert.deepEqual([...visible()].sort(), [...row.expected].sort());
  });

  // The 13 per-identity assertions the exact-set check above summarises,
  // stated individually so a failure names the tool rather than a diff.
  for (const tool of ALL_TOOLS) {
    const shouldSee = row.expected.includes(tool);
    test(`identity "${row.label}" ${shouldSee ? 'sees' : 'cannot see'} ${tool}`, () => {
      assert.equal(visible().has(tool), shouldSee);
    });
  }
}

test('no identity can see a development-only tool', () => {
  for (const row of MATRIX) {
    const visible = visibleTo(sessionWith(row.grants.roles, row.grants.permissions));
    assert.equal(visible.has('get_echo'), false, `${row.label} sees get_echo`);
    assert.equal(visible.has('list_examples'), false, `${row.label} sees list_examples`);
  }
});

test('an absent session sees only the ungated tool', () => {
  assert.deepEqual([...visibleTo(undefined)], ['get_server_time']);
});
```

Add to `test/index.ts`:

```ts
import './grant-matrix.test.js';
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test`
Expected: FAIL if `sessionWith` / `visibleTo` are not exported from `test/tool-gates.test.ts`. Add the `export` keyword to both (Task 3's code already declares them with `export`; verify it).

- [ ] **Step 3: Write minimal implementation**

No production code changes. If any matrix row fails, the *gate* is wrong, not the matrix — fix the tool, and only change `MATRIX` if the spec's §6.1 table is what was misread.

- [ ] **Step 4: Run test to verify it passes**

Run: `npm test`
Expected: 6 exact-set tests + 78 per-tool tests + 2 guards, all PASS. Record the count in the commit message — this is the eval's pass rate for spec §10.

- [ ] **Step 5: Commit**

```bash
git add test/grant-matrix.test.ts test/index.ts
git commit -m "test: grant x tool eval matrix, 6 identities x 13 tools, 86/86 passing"
```

---

# Part B — Gateway

All Part B paths are relative to `/home/manoj/newlaptop/projects/python/local-ai-model-gateway`.

---

### Task 8: `app/mcp/grants.py` — the pure core

**Files:**
- Create: `app/mcp/grants.py`
- Create: `tests/test_mcp_grants.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ROLE_HRMS`/`ROLE_IZONE`/`ROLE_EMS`, `PERM_HRMS_FULL`/`PERM_HRMS_TASKS`/`PERM_EMS_QUERY`; `ROLES`, `PERMISSIONS`, `ALL_GRANTS` (frozensets); `EMAIL_HEADER`, `ROLES_HEADER`, `PERMISSIONS_HEADER`; `McpIdentity(email, roles, permissions)` frozen dataclass with `McpIdentity.from_grants(email=..., grant_keys=...)`; `header_values(identity) -> dict[str, str]`.

Pure — no DB, no HTTP — for the reason `app/rag/permissions.py` and `app/users/policy.py` are pure: this decides a user-visible capability boundary and should be provable without a database or a model.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_grants.py`:

```python
"""Unit tests for the pure MCP grant core.

No database, no HTTP, no app import beyond the module under test — the same
rule `tests/test_department_permissions.py` follows for `app/rag/permissions.py`.
"""

import ast
import pathlib

import pytest

from app.mcp import grants


def test_the_vocabulary_is_exactly_the_six_agreed_strings():
    assert grants.ROLES == frozenset({"mcp-hrms", "mcp-izone", "mcp-ems"})
    assert grants.PERMISSIONS == frozenset(
        {"mcp.hrms.full", "mcp.hrms.tasks", "mcp.ems.query"}
    )
    assert grants.ALL_GRANTS == grants.ROLES | grants.PERMISSIONS
    assert len(grants.ALL_GRANTS) == 6


def test_roles_and_permissions_do_not_overlap():
    # A string in both sets would be sorted into both header fields and the
    # MCP server would see it twice under different meanings.
    assert not (grants.ROLES & grants.PERMISSIONS)


def test_from_grants_splits_a_flat_key_set_by_kind():
    identity = grants.McpIdentity.from_grants(
        email="person@example.com",
        grant_keys=["mcp-hrms", "mcp.hrms.full", "mcp-ems"],
    )
    assert identity.roles == frozenset({"mcp-hrms", "mcp-ems"})
    assert identity.permissions == frozenset({"mcp.hrms.full"})


def test_an_unknown_grant_key_is_dropped_not_raised():
    """Fail-closed rule 3: the two sides deploy independently.

    A row that predates a vocabulary change, or a hand-inserted key, must not
    500 the chat endpoint — and must not be forwarded either, or the MCP server
    would be asked to reason about a grant this build does not define.
    """
    identity = grants.McpIdentity.from_grants(
        email="person@example.com", grant_keys=["mcp-hrms", "mcp-payroll", "nonsense"]
    )
    assert identity.roles == frozenset({"mcp-hrms"})
    assert identity.permissions == frozenset()


def test_an_identity_with_no_grants_is_representable():
    identity = grants.McpIdentity.from_grants(email="a@b.c", grant_keys=[])
    assert identity.roles == frozenset()
    assert identity.permissions == frozenset()
    # The email still travels: the MCP server logs refusals against it.
    assert grants.header_values(identity) == {"x-user-email": "a@b.c"}


def test_header_values_omits_empty_fields_entirely():
    """An empty header and an absent one mean the same thing to the server, but
    sending `x-user-roles: ` invites a future parser to treat '' as a grant."""
    identity = grants.McpIdentity(email=None, roles=frozenset(), permissions=frozenset())
    assert grants.header_values(identity) == {}


def test_header_values_are_sorted_so_they_are_deterministic():
    identity = grants.McpIdentity.from_grants(
        email="p@e.com", grant_keys=["mcp-izone", "mcp-ems", "mcp-hrms"]
    )
    assert grants.header_values(identity)["x-user-roles"] == "mcp-ems,mcp-hrms,mcp-izone"


def test_header_values_of_none_is_empty():
    # An unauthenticated or identity-less call must not forward anything.
    assert grants.header_values(None) == {}


def test_the_identity_is_frozen():
    """It is captured by a streaming generator and read after the request
    scope ends; a mutable identity could be edited mid-turn."""
    identity = grants.McpIdentity.from_grants(email="a@b.c", grant_keys=["mcp-hrms"])
    with pytest.raises(Exception):
        identity.email = "other@b.c"  # type: ignore[misc]


def test_the_module_is_pure_no_database_and_no_http():
    """AST-asserted, not grepped: the value of this module is that a capability
    boundary can be proved without a database or a network."""
    source = pathlib.Path("app/mcp/grants.py").read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"sqlalchemy", "httpx", "fastapi", "app"}
    assert not (imported & forbidden), f"grants.py must stay pure; found {imported & forbidden}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_grants.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.mcp.grants'`.

- [ ] **Step 3: Write minimal implementation**

Create `app/mcp/grants.py`:

```python
"""Pure decisions about which MCP grants a caller holds, and how they travel.

No database, no HTTP, no FastAPI — for the reason `app/rag/permissions.py` and
`app/users/policy.py` are pure. This is the code that decides whether somebody
can read salary data or run SQL over the expenses database, and proving that
should not require standing up Postgres.

WHAT IS DELIBERATELY NOT HERE: a tool -> grant map. `canAccess` on the MCP
server is applied at session construction, so its `tools/list` already returns
exactly the tools an identity may call. A second copy of that mapping here would
drift, and the dangerous direction of drift is silent: "the gateway hides a tool
the MCP would allow" is a lost capability with no error on either side. One
mapping, in the enforcer, read by both.

The six strings below are the second of three copies (the third is
`ck_user_mcp_grants_key`, the first is the MCP server's `src/auth/mcp-roles.ts`).
That is the `ck_api_keys_scopes` / `policy.ALL_SCOPES` arrangement on purpose:
the CHECK stops a typo being STORED, this frozenset stops one being HONOURED,
and the server's copy stops one being ENFORCED.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# --- the vocabulary ------------------------------------------------------- #
# Roles name a SYSTEM (hyphenated); permissions name a SHARP EDGE inside one
# (dotted). Adding a dangerous tool adds a permission, never a new role.
ROLE_HRMS = "mcp-hrms"
ROLE_IZONE = "mcp-izone"
ROLE_EMS = "mcp-ems"

PERM_HRMS_FULL = "mcp.hrms.full"
PERM_HRMS_TASKS = "mcp.hrms.tasks"
PERM_EMS_QUERY = "mcp.ems.query"

ROLES: frozenset[str] = frozenset({ROLE_HRMS, ROLE_IZONE, ROLE_EMS})
PERMISSIONS: frozenset[str] = frozenset(
    {PERM_HRMS_FULL, PERM_HRMS_TASKS, PERM_EMS_QUERY}
)
ALL_GRANTS: frozenset[str] = ROLES | PERMISSIONS

# --- the wire ------------------------------------------------------------- #
EMAIL_HEADER = "x-user-email"
ROLES_HEADER = "x-user-roles"
PERMISSIONS_HEADER = "x-user-permissions"


@dataclass(frozen=True)
class McpIdentity:
    """Who is asking, and what they hold.

    Frozen because it is captured by the streaming generator in
    `app/chat/router.py` and read after the request scope has ended.
    """

    email: str | None
    roles: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()

    @classmethod
    def from_grants(
        cls, *, email: str | None, grant_keys: Iterable[str]
    ) -> "McpIdentity":
        """Split a flat set of stored grant keys into roles and permissions.

        Unknown keys are dropped by intersection rather than raised on. The
        gateway and the MCP server deploy independently, so a key this build
        does not define may legitimately exist in the table; forwarding it would
        ask the server to reason about a grant neither side agrees on, and
        raising would 500 a chat turn over a row nobody is using.
        """
        keys = set(grant_keys)
        return cls(
            email=email,
            roles=frozenset(keys & ROLES),
            permissions=frozenset(keys & PERMISSIONS),
        )


def header_values(identity: McpIdentity | None) -> dict[str, str]:
    """The headers this identity contributes to an MCP request.

    Empty fields are omitted rather than sent blank: `x-user-roles: ` invites a
    future parser to treat the empty string as a grant, and an absent header
    already means "none" on the other side. Values are sorted so a request is
    reproducible and a test can assert on the exact string.
    """
    if identity is None:
        return {}
    headers: dict[str, str] = {}
    if identity.email:
        headers[EMAIL_HEADER] = identity.email
    if identity.roles:
        headers[ROLES_HEADER] = ",".join(sorted(identity.roles))
    if identity.permissions:
        headers[PERMISSIONS_HEADER] = ",".join(sorted(identity.permissions))
    return headers
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_mcp_grants.py -v`
Expected: 10 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp/grants.py tests/test_mcp_grants.py
git commit -m "feat(mcp): pure grant vocabulary, McpIdentity, and header serialisation"
```

---

### Task 9: The `user_mcp_grants` table

**Files:**
- Create: `app/mcp/models.py`
- Create: `alembic/versions/a3f7c21e8b04_user_mcp_grants.py`
- Create: `tests/test_mcp_grants_integration.py`

**Interfaces:**
- Consumes: `grants.ALL_GRANTS` (Task 8).
- Produces: `UserMcpGrant` ORM model with columns `user_id: int`, `grant_key: str`, `granted_at: datetime`, `granted_by: int | None`.

`users.id` is an **INTEGER** (`Mapped[int]`, not a UUID); `user_departments.user_id` is the precedent to copy.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_grants_integration.py`:

```python
"""Integration tests for user_mcp_grants and the admin route.

Builds a throwaway NullPool engine per call rather than using the app's
module-level `engine`: that one pools connections bound to the first event loop,
and each `asyncio.run` creates a new one, so the second test in the file would
die with "Event loop is closed". Same rule as the RAG and api-keys integration
tests.
"""

import asyncio
import os
import re

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.mcp import grants
from app.mcp.models import UserMcpGrant

DB_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="DATABASE_URL not set")


def _run(coro_fn):
    async def main():
        engine = create_async_engine(DB_URL, poolclass=NullPool)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                return await coro_fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(main())


async def _an_admin_id(session):
    from app.users.models import User

    row = await session.scalar(select(User.id).where(User.role == "admin").limit(1))
    assert row is not None, "seed an admin first (admin@example.com)"
    return row


def test_the_check_constraint_and_the_frozenset_are_the_same_vocabulary():
    """The two copies are deliberate; drifting apart is not.

    Reads the LIVE constraint rather than a hand-written literal: a literal here
    would be a third copy, which is exactly the drift it is meant to detect.
    """

    async def go(session):
        definition = await session.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_user_mcp_grants_key'"
            )
        )
        assert definition, "ck_user_mcp_grants_key does not exist"
        in_check = set(re.findall(r"'([^']+)'", definition))
        assert in_check == set(grants.ALL_GRANTS), (
            f"CHECK has {sorted(in_check)}, grants.py has {sorted(grants.ALL_GRANTS)}"
        )

    _run(go)


def test_an_unknown_grant_key_cannot_be_stored():
    """ck_user_mcp_grants_key: a typo'd grant must not reach the table.

    A stored 'mcp-hmrs' would be silently powerless — a privilege bug that
    reads as a typo, which is why `ck_users_role` exists too.
    """

    async def go(session):
        admin_id = await _an_admin_id(session)
        session.add(
            UserMcpGrant(user_id=admin_id, grant_key="mcp-hmrs", granted_by=admin_id)
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    _run(go)


def test_every_vocabulary_key_is_storable():
    """The mirror of the test above: the CHECK must not be tighter than the code."""

    async def go(session):
        admin_id = await _an_admin_id(session)
        for key in sorted(grants.ALL_GRANTS):
            session.add(
                UserMcpGrant(user_id=admin_id, grant_key=key, granted_by=admin_id)
            )
            await session.flush()
        await session.rollback()

    _run(go)


def test_the_same_grant_cannot_be_stored_twice():
    async def go(session):
        admin_id = await _an_admin_id(session)
        session.add(
            UserMcpGrant(user_id=admin_id, grant_key=grants.ROLE_HRMS, granted_by=admin_id)
        )
        await session.flush()
        session.add(
            UserMcpGrant(user_id=admin_id, grant_key=grants.ROLE_HRMS, granted_by=admin_id)
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    _run(go)


def test_the_audit_columns_survive_the_granter_being_deleted():
    """granted_by is ON DELETE SET NULL, not CASCADE: the fact that access was
    granted at a time outlives the admin who granted it."""

    async def go(session):
        definition = await session.scalar(
            text(
                "SELECT confdeltype FROM pg_constraint "
                "WHERE conrelid = 'user_mcp_grants'::regclass "
                "AND confrelid = 'users'::regclass "
                "AND 'granted_by' = ANY("
                "  SELECT attname FROM pg_attribute "
                "  WHERE attrelid = conrelid AND attnum = ANY(conkey)"
                ")"
            )
        )
        assert definition == "n", "granted_by must be ON DELETE SET NULL"

    _run(go)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_grants_integration.py -v`
Expected: FAIL — `No module named 'app.mcp.models'`.

- [ ] **Step 3: Write minimal implementation**

Create `app/mcp/models.py`:

```python
"""The `user_mcp_grants` table: which MCP grants a user holds.

Deliberately separate from `users.role`. A global gateway admin holds NO grant
implicitly (design §3.4) — admin confers the ability to GRANT, not the grants
themselves — because gateway admin is an IT/ops role and auto-conferring
`Salary_Level` plus a SQL console over the expenses database on whoever operates
the gateway is precisely the quiet escalation a bank audit objects to. This
departs from `permissions.effective_level(is_global_admin=True) -> owner` on
purpose; do not "fix" it back.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base
from .grants import ALL_GRANTS

# Rendered into the CHECK below and into the migration. Sorted so a future
# autogenerate run does not propose a spurious diff.
_VOCABULARY = ", ".join(f"'{key}'" for key in sorted(ALL_GRANTS))


class UserMcpGrant(Base):
    __tablename__ = "user_mcp_grants"
    __table_args__ = (
        # Closes the vocabulary, the same way ck_user_departments_role and
        # ck_documents_status do. The CHECK stops a typo being STORED;
        # grants.ALL_GRANTS stops one being HONOURED. Adding a grant means
        # editing both, plus the MCP server's own copy.
        CheckConstraint(
            f"grant_key IN ({_VOCABULARY})",
            name="ck_user_mcp_grants_key",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    grant_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # SET NULL, not CASCADE: the audit fact outlives the admin who granted it.
    granted_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
```

Verify the `Base` import path against `app/users/models.py` — use whatever that file imports, not this guess.

Create `alembic/versions/a3f7c21e8b04_user_mcp_grants.py`:

```python
"""user_mcp_grants

Revision ID: a3f7c21e8b04
Revises: b7e1c4d92a03
Create Date: 2026-08-24

Per-user MCP tool grants. The CHECK enumerates the vocabulary as a literal, so
adding a grant is a schema change and not a config change — the same
arrangement as ck_api_keys_scopes. The literal below must stay in the sorted
order `app/mcp/models.py` generates (`sorted(ALL_GRANTS)`), or a future
autogenerate run will propose a spurious diff.
"""

import sqlalchemy as sa
from alembic import op

revision = "a3f7c21e8b04"
down_revision = "b7e1c4d92a03"
branch_labels = None
depends_on = None

_VOCABULARY = (
    "grant_key IN ('mcp-ems', 'mcp-hrms', 'mcp-izone', "
    "'mcp.ems.query', 'mcp.hrms.full', 'mcp.hrms.tasks')"
)


def upgrade() -> None:
    op.create_table(
        "user_mcp_grants",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("grant_key", sa.String(length=64), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("granted_by", sa.Integer(), nullable=True),
        sa.CheckConstraint(_VOCABULARY, name="ck_user_mcp_grants_key"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("user_id", "grant_key"),
    )


def downgrade() -> None:
    op.drop_table("user_mcp_grants")
```

Import the model where the app's metadata is assembled, so `alembic` autogenerate and `Base.metadata` both see it. Find the file that imports `app.rag.models` / `app.apikeys.models` for this purpose (check `alembic/env.py` and `app/db/base.py`) and add `app.mcp.models` beside them.

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/alembic upgrade head
.venv/bin/pytest tests/test_mcp_grants_integration.py tests/test_alembic_lineage.py -v
```
Expected: 5 integration tests PASS; `test_alembic_lineage.py` PASSES (still one head).

Also confirm no drift: `.venv/bin/alembic revision --autogenerate -m "drift check"` should produce an empty migration. **Delete that file** afterwards — it must not be committed.

- [ ] **Step 5: Commit**

```bash
git add app/mcp/models.py alembic/versions/a3f7c21e8b04_user_mcp_grants.py tests/test_mcp_grants_integration.py alembic/env.py app/db/base.py
git commit -m "feat(mcp): user_mcp_grants table with a closed grant vocabulary"
```

---

### Task 10: Repository and the admin route

**Files:**
- Create: `app/mcp/repository.py`
- Create: `app/mcp/schemas.py`
- Create: `app/mcp/grants_router.py`
- Modify: `app/main.py`
- Modify: `tests/test_mcp_grants_integration.py`

**Interfaces:**
- Consumes: `UserMcpGrant` (Task 9); `grants.ALL_GRANTS` (Task 8).
- Produces: `repository.list_grants(session, user_id) -> list[UserMcpGrant]`, `repository.grant_keys_for(session, user_id) -> set[str]`, `repository.grant(session, *, user_id, grant_key, granted_by) -> None`, `repository.revoke(session, *, user_id, grant_key) -> bool`; router mounted at `/v1/users/{user_id}/mcp-grants`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_grants_integration.py`:

```python
def test_re_granting_does_not_rewrite_the_audit_timestamp():
    """The `POST .../members` lesson: an upsert that overwrites audit columns
    reports success while destroying the record of when access was given."""

    async def go(session):
        from app.mcp import repository as repo

        admin_id = await _an_admin_id(session)
        await repo.grant(
            session, user_id=admin_id, grant_key=grants.ROLE_IZONE, granted_by=admin_id
        )
        await session.flush()
        first = await session.scalar(
            select(UserMcpGrant.granted_at).where(
                UserMcpGrant.user_id == admin_id,
                UserMcpGrant.grant_key == grants.ROLE_IZONE,
            )
        )
        await repo.grant(
            session, user_id=admin_id, grant_key=grants.ROLE_IZONE, granted_by=admin_id
        )
        await session.flush()
        second = await session.scalar(
            select(UserMcpGrant.granted_at).where(
                UserMcpGrant.user_id == admin_id,
                UserMcpGrant.grant_key == grants.ROLE_IZONE,
            )
        )
        assert first == second, "a re-grant rewrote granted_at"
        await session.rollback()

    _run(go)


def test_revoking_is_idempotent_and_reports_whether_anything_changed():
    async def go(session):
        from app.mcp import repository as repo

        admin_id = await _an_admin_id(session)
        await repo.grant(
            session, user_id=admin_id, grant_key=grants.ROLE_EMS, granted_by=admin_id
        )
        await session.flush()
        assert await repo.revoke(session, user_id=admin_id, grant_key=grants.ROLE_EMS) is True
        assert await repo.revoke(session, user_id=admin_id, grant_key=grants.ROLE_EMS) is False
        await session.rollback()

    _run(go)


def test_grant_keys_for_returns_a_flat_set():
    async def go(session):
        from app.mcp import repository as repo

        admin_id = await _an_admin_id(session)
        await repo.grant(
            session, user_id=admin_id, grant_key=grants.ROLE_EMS, granted_by=admin_id
        )
        await repo.grant(
            session, user_id=admin_id, grant_key=grants.PERM_EMS_QUERY, granted_by=admin_id
        )
        await session.flush()
        keys = await repo.grant_keys_for(session, admin_id)
        assert grants.ROLE_EMS in keys and grants.PERM_EMS_QUERY in keys
        await session.rollback()

    _run(go)


def test_an_admin_role_alone_confers_no_grant():
    """Design §3.4. This is the departure from effective_level that a later
    refactor is most likely to "fix" back — hence a test that names it."""

    async def go(session):
        from app.mcp import repository as repo

        admin_id = await _an_admin_id(session)
        await session.execute(
            text("DELETE FROM user_mcp_grants WHERE user_id = :uid"), {"uid": admin_id}
        )
        keys = await repo.grant_keys_for(session, admin_id)
        assert keys == set(), "a global admin was handed grants implicitly"
        await session.rollback()

    _run(go)
```

Also create `tests/test_mcp_grants_routes.py` for the HTTP surface. It uses a
TestClient rather than the NullPool-engine harness above, so it is a separate
file — mirroring `tests/test_document_upload.py`, whose `_auth` helper this
copies. **Those helpers skip on auth failure**, so a broken signature turns
these into silent skips that a green run hides: compare the skip count, not
just the pass count.

```python
"""HTTP tests for the MCP grant admin routes.

A TestClient per test with a local `_auth()` that registers, logs in, and skips
when Postgres is down — the same shape as tests/test_document_upload.py.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.mcp import grants

MEMBER = "mcpgrant-member@example.com"
PASSWORD = "supersecret123"
SEEDED_ADMIN_EMAIL = "admin@example.com"
SEEDED_ADMIN_PASSWORD = "supersecret123"


def _ensure_user(client, email, password):
    """Create the user if absent. `POST /auth/register` is admin-only, so this
    borrows the seeded admin's token exactly as the other route tests do."""
    headers = {}
    if email != SEEDED_ADMIN_EMAIL:
        resp = client.post(
            "/auth/login",
            json={"email": SEEDED_ADMIN_EMAIL, "password": SEEDED_ADMIN_PASSWORD},
        )
        if resp.status_code == 200:
            headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    client.post(
        "/auth/register", json={"email": email, "password": password}, headers=headers
    )


def _auth(client, email):
    err = resp = None
    try:
        _ensure_user(client, email, PASSWORD)
        resp = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    except Exception as exc:  # noqa: BLE001
        err = exc
    if err is not None:
        pytest.skip(f"Postgres unreachable: {type(err).__name__}")
    if resp.status_code != 200:
        pytest.skip(f"auth failed (login -> {resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _my_id(client, headers):
    resp = client.get("/users/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _clear(client, admin, user_id):
    """Leave no grant behind: this table is global with no fixture scope."""
    for key in sorted(grants.ALL_GRANTS):
        client.delete(f"/v1/users/{user_id}/mcp-grants/{key}", headers=admin)


def test_an_unknown_grant_key_is_422_not_a_500_from_the_check():
    """Both the route and the CHECK reject it; only one gives a usable error."""
    with TestClient(app) as client:
        admin = _auth(client, SEEDED_ADMIN_EMAIL)
        uid = _my_id(client, admin)
        resp = client.post(
            f"/v1/users/{uid}/mcp-grants", json={"grant_key": "mcp-hmrs"}, headers=admin
        )
        assert resp.status_code == 422, resp.text
        assert "mcp-hmrs" in resp.text


def test_an_unexpected_field_is_refused_loudly():
    """extra="forbid", matching UserUpdate: a silently ignored field means the
    caller believes they set something they did not."""
    with TestClient(app) as client:
        admin = _auth(client, SEEDED_ADMIN_EMAIL)
        uid = _my_id(client, admin)
        resp = client.post(
            f"/v1/users/{uid}/mcp-grants",
            json={"grant_key": grants.ROLE_HRMS, "role": "admin"},
            headers=admin,
        )
        assert resp.status_code == 422, resp.text


def test_granting_appears_in_the_list_and_re_granting_is_still_201():
    with TestClient(app) as client:
        admin = _auth(client, SEEDED_ADMIN_EMAIL)
        member = _auth(client, MEMBER)
        uid = _my_id(client, member)
        _clear(client, admin, uid)
        try:
            first = client.post(
                f"/v1/users/{uid}/mcp-grants",
                json={"grant_key": grants.ROLE_HRMS},
                headers=admin,
            )
            assert first.status_code == 201, first.text
            keys = [item["grant_key"] for item in first.json()["items"]]
            assert keys == [grants.ROLE_HRMS]
            assert first.json()["items"][0]["granted_by"] == _my_id(client, admin)

            again = client.post(
                f"/v1/users/{uid}/mcp-grants",
                json={"grant_key": grants.ROLE_HRMS},
                headers=admin,
            )
            assert again.status_code == 201, again.text
            # Idempotent, and the audit timestamp is untouched.
            assert again.json()["items"][0]["granted_at"] == (
                first.json()["items"][0]["granted_at"]
            )

            listed = client.get(f"/v1/users/{uid}/mcp-grants", headers=admin)
            assert listed.status_code == 200
            assert [i["grant_key"] for i in listed.json()["items"]] == [grants.ROLE_HRMS]
        finally:
            _clear(client, admin, uid)


def test_revoking_is_idempotent_204():
    with TestClient(app) as client:
        admin = _auth(client, SEEDED_ADMIN_EMAIL)
        member = _auth(client, MEMBER)
        uid = _my_id(client, member)
        client.post(
            f"/v1/users/{uid}/mcp-grants",
            json={"grant_key": grants.PERM_EMS_QUERY},
            headers=admin,
        )
        first = client.delete(
            f"/v1/users/{uid}/mcp-grants/{grants.PERM_EMS_QUERY}", headers=admin
        )
        second = client.delete(
            f"/v1/users/{uid}/mcp-grants/{grants.PERM_EMS_QUERY}", headers=admin
        )
        assert first.status_code == 204, first.text
        assert second.status_code == 204, second.text


def test_a_member_cannot_read_or_write_grants():
    with TestClient(app) as client:
        member = _auth(client, MEMBER)
        uid = _my_id(client, member)
        assert client.get(f"/v1/users/{uid}/mcp-grants", headers=member).status_code == 403
        assert (
            client.post(
                f"/v1/users/{uid}/mcp-grants",
                json={"grant_key": grants.ROLE_EMS},
                headers=member,
            ).status_code
            == 403
        )
        assert (
            client.delete(
                f"/v1/users/{uid}/mcp-grants/{grants.ROLE_EMS}", headers=member
            ).status_code
            == 403
        )


def test_an_unauthenticated_caller_is_401():
    with TestClient(app) as client:
        assert client.get("/v1/users/1/mcp-grants").status_code == 401


def test_an_unknown_user_is_404():
    with TestClient(app) as client:
        admin = _auth(client, SEEDED_ADMIN_EMAIL)
        resp = client.post(
            f"/v1/users/999999999/mcp-grants",
            json={"grant_key": grants.ROLE_HRMS},
            headers=admin,
        )
        assert resp.status_code == 404, resp.text


def test_a_fresh_admin_holds_no_grants():
    """Design §3.4 through the HTTP surface: admin confers the ability to
    grant, never the grants. The single most likely thing a later refactor
    "fixes" back into an implicit bypass."""
    with TestClient(app) as client:
        admin = _auth(client, SEEDED_ADMIN_EMAIL)
        uid = _my_id(client, admin)
        _clear(client, admin, uid)
        listed = client.get(f"/v1/users/{uid}/mcp-grants", headers=admin)
        assert listed.status_code == 200
        assert listed.json()["items"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_grants_integration.py -v`
Expected: FAIL — `No module named 'app.mcp.repository'`.

- [ ] **Step 3: Write minimal implementation**

Create `app/mcp/repository.py`:

```python
"""Data access for `user_mcp_grants`."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import UserMcpGrant


async def list_grants(session: AsyncSession, user_id: int) -> list[UserMcpGrant]:
    result = await session.execute(
        select(UserMcpGrant)
        .where(UserMcpGrant.user_id == user_id)
        .order_by(UserMcpGrant.grant_key)
    )
    return list(result.scalars())


async def grant_keys_for(session: AsyncSession, user_id: int) -> set[str]:
    """Just the keys — what `McpIdentity.from_grants` consumes."""
    result = await session.execute(
        select(UserMcpGrant.grant_key).where(UserMcpGrant.user_id == user_id)
    )
    return set(result.scalars())


async def grant(
    session: AsyncSession, *, user_id: int, grant_key: str, granted_by: int | None
) -> None:
    """Idempotent grant.

    ON CONFLICT DO NOTHING, deliberately not DO UPDATE: re-granting must leave
    `granted_at` and `granted_by` alone. An upsert that overwrites them reports
    success while destroying the record of when access was actually given —
    the same defect `test_omitting_role_on_a_RE_grant_does_not_demote` guards
    against on the department member route.
    """
    await session.execute(
        insert(UserMcpGrant)
        .values(user_id=user_id, grant_key=grant_key, granted_by=granted_by)
        .on_conflict_do_nothing(index_elements=["user_id", "grant_key"])
    )


async def revoke(session: AsyncSession, *, user_id: int, grant_key: str) -> bool:
    """Remove a grant. Returns whether a row was actually removed."""
    result = await session.execute(
        delete(UserMcpGrant).where(
            UserMcpGrant.user_id == user_id, UserMcpGrant.grant_key == grant_key
        )
    )
    return bool(result.rowcount)
```

Create `app/mcp/schemas.py`:

```python
"""Request and response models for the MCP grant admin route."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from .grants import ALL_GRANTS


class GrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    grant_key: str
    granted_at: datetime
    granted_by: int | None


class GrantListResponse(BaseModel):
    user_id: int
    items: list[GrantOut]


class GrantCreate(BaseModel):
    """`extra="forbid"` matches `UserUpdate`: an unexpected field is refused
    loudly rather than silently ignored."""

    model_config = ConfigDict(extra="forbid")

    grant_key: str

    @field_validator("grant_key")
    @classmethod
    def _must_be_known(cls, value: str) -> str:
        # Validated against ALL_GRANTS rather than restated as a Literal, which
        # would be a fourth copy of the vocabulary. Pydantic turns this into a
        # 422 with the offending value named — the CHECK would give a 500.
        if value not in ALL_GRANTS:
            raise ValueError(
                f"unknown grant: {value!r}; expected one of {sorted(ALL_GRANTS)}"
            )
        return value
```

Create `app/mcp/grants_router.py`:

```python
"""Admin routes for per-user MCP grants.

Deliberately NOT folded into `PATCH /users/{id}`, which already refuses `role`
with `extra="forbid"` because promotion is an escalation surface wanting its own
guards. Granting somebody a SQL console over the expenses database is the same
kind of surface, so it gets the same treatment: its own route, its own
validation, and `granted_by` written on every insert.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_admin
from ..db.session import get_session
from ..users import repository as users_repo
from ..users.models import User
from . import repository as repo
from .schemas import GrantCreate, GrantListResponse, GrantOut

router = APIRouter(prefix="/v1/users", tags=["mcp-grants"])


async def _known_user(session: AsyncSession, user_id: int) -> User:
    user = await users_repo.get_by_id(session, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user"
        )
    return user


@router.get(
    "/{user_id}/mcp-grants",
    response_model=GrantListResponse,
    summary="List a user's MCP tool grants (admin only)",
    responses={
        401: {"description": "Missing/invalid JWT."},
        403: {"description": "Caller is not an admin."},
        404: {"description": "Unknown user."},
    },
)
async def list_user_grants(
    user_id: int,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> GrantListResponse:
    await _known_user(session, user_id)
    rows = await repo.list_grants(session, user_id)
    return GrantListResponse(
        user_id=user_id, items=[GrantOut.model_validate(row) for row in rows]
    )


@router.post(
    "/{user_id}/mcp-grants",
    response_model=GrantListResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Grant an MCP role or permission (admin only)",
    responses={
        401: {"description": "Missing/invalid JWT."},
        403: {"description": "Caller is not an admin."},
        404: {"description": "Unknown user."},
        422: {"description": "Unknown grant key, or an unexpected field."},
    },
)
async def add_user_grant(
    user_id: int,
    body: GrantCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> GrantListResponse:
    await _known_user(session, user_id)
    # Idempotent: re-granting is a 201 with the same list and an untouched
    # granted_at, not a 409. The caller's intent is already satisfied.
    await repo.grant(
        session, user_id=user_id, grant_key=body.grant_key, granted_by=admin.id
    )
    await session.commit()
    rows = await repo.list_grants(session, user_id)
    return GrantListResponse(
        user_id=user_id, items=[GrantOut.model_validate(row) for row in rows]
    )


@router.delete(
    "/{user_id}/mcp-grants/{grant_key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an MCP role or permission (admin only)",
    responses={
        401: {"description": "Missing/invalid JWT."},
        403: {"description": "Caller is not an admin."},
        404: {"description": "Unknown user."},
    },
)
async def remove_user_grant(
    user_id: int,
    grant_key: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _known_user(session, user_id)
    # 204 whether or not a row existed: revocation is idempotent, and a 404
    # here would leak which grants a user holds to a caller who may list them
    # anyway — noise without a boundary.
    await repo.revoke(session, user_id=user_id, grant_key=grant_key)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

In `app/main.py`, beside the existing router imports and includes:

```python
from .mcp.grants_router import router as mcp_grants_router
```
```python
app.include_router(mcp_grants_router)
```

Confirm `users_repo.get_by_id` exists with that name (it is used by `app/users/router.py`'s `update_user`).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_mcp_grants_integration.py tests/test_mcp_grants_routes.py -v`
Expected: all PASS. Then confirm the routes are mounted — `include_router` mounts lazily in Starlette 1.x, so check `/openapi.json`, never `isinstance` over `app.routes`:

```bash
.venv/bin/python -c "
from app.main import app
print([p for p in app.openapi()['paths'] if 'mcp-grants' in p])
"
```
Expected: both paths listed.

- [ ] **Step 5: Commit**

```bash
git add app/mcp/repository.py app/mcp/schemas.py app/mcp/grants_router.py app/main.py tests/test_mcp_grants_integration.py tests/test_mcp_grants_routes.py
git commit -m "feat(mcp): admin routes to grant and revoke MCP tool access"
```

---

### Task 11: Forward the identity on every MCP request

**Files:**
- Create: `app/mcp/dependencies.py`
- Modify: `app/mcp/client.py`
- Modify: `app/tools/router.py`
- Modify: `app/mcp/router.py`
- Modify: `app/chat/router.py`
- Modify: `app/agent/loop.py`
- Modify: `tests/test_mcp_grants.py`

**Interfaces:**
- Consumes: `McpIdentity`, `header_values` (Task 8); `repository.grant_keys_for` (Task 10).
- Produces: `get_mcp_identity` FastAPI dependency returning `McpIdentity`; `MCPClient.session(*, identity=None)`, `MCPClient.describe(*, identity=None)`; `loop.stream_turn(..., identity=None)` and `loop.run_turn(..., identity=None)` replacing their `user_email` parameter.

This is the step that both restores and enforces access. The identity is a **per-call argument, never client state** — `MCPClient` is a process-wide singleton on `app.state.mcp`, so storing it would let two concurrent users race each other's grants.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_grants.py`:

```python
def _client():
    from app.mcp.client import MCPClient

    return MCPClient(
        server_url="http://127.0.0.1:3333/mcp",
        auth_token="secret",
        tool_mode="read_only",
        allowlist=[],
        read_prefixes=["get", "list"],
        write_keywords=["create"],
    )


def test_session_headers_carry_the_identity():
    identity = grants.McpIdentity.from_grants(
        email="person@example.com",
        grant_keys=["mcp-hrms", "mcp.hrms.full"],
    )
    headers = _client()._session_headers(identity)
    assert headers["Authorization"] == "Bearer secret"
    assert headers["x-user-email"] == "person@example.com"
    assert headers["x-user-roles"] == "mcp-hrms"
    assert headers["x-user-permissions"] == "mcp.hrms.full"


def test_session_headers_omit_grant_fields_for_an_ungranted_caller():
    identity = grants.McpIdentity.from_grants(email="person@example.com", grant_keys=[])
    headers = _client()._session_headers(identity)
    assert "x-user-roles" not in headers
    assert "x-user-permissions" not in headers
    # The auth token still goes: the caller is authenticated, just unprovisioned.
    assert headers["Authorization"] == "Bearer secret"


def test_no_identity_forwards_no_user_headers():
    headers = _client()._session_headers(None)
    assert headers == {"Authorization": "Bearer secret"}


def test_the_client_holds_no_identity_state_between_calls():
    """MCPClient is a process-wide singleton on app.state.mcp. If the identity
    were stored on it, two concurrent turns would race each other's grants and
    one user would act with another's permissions."""
    client = _client()
    hrms = grants.McpIdentity.from_grants(email="a@b.c", grant_keys=["mcp-hrms"])
    ems = grants.McpIdentity.from_grants(email="d@e.f", grant_keys=["mcp-ems"])

    first = client._session_headers(hrms)
    second = client._session_headers(ems)
    third = client._session_headers(hrms)

    assert first["x-user-roles"] == "mcp-hrms"
    assert second["x-user-roles"] == "mcp-ems"
    assert third == first
    assert "identity" not in vars(client)


def test_the_returned_headers_are_a_fresh_dict_each_call():
    """A shared dict would let one turn's mutation reach another's request."""
    client = _client()
    identity = grants.McpIdentity.from_grants(email="a@b.c", grant_keys=["mcp-hrms"])
    first = client._session_headers(identity)
    first["x-user-roles"] = "mcp-ems"
    assert client._session_headers(identity)["x-user-roles"] == "mcp-hrms"


def test_no_call_site_still_passes_user_email_to_the_mcp_client():
    """AST check across the three call sites: a forgotten one would compile,
    forward no grants, and present as 'the tools stopped working' with no error
    anywhere — the §18 failure class."""
    import ast
    import pathlib

    for path in ("app/tools/router.py", "app/mcp/router.py", "app/agent/loop.py"):
        tree = ast.parse(pathlib.Path(path).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                assert keyword.arg != "user_email", (
                    f"{path} still passes user_email= to an MCP call"
                )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_grants.py -v`
Expected: FAIL — `_session_headers` takes `user_email`, not an identity, and the AST check finds `user_email=` at all three sites.

- [ ] **Step 3: Write minimal implementation**

Create `app/mcp/dependencies.py`:

```python
"""Resolve the calling user's MCP identity — the ONE place grants are loaded.

Costs one small query per request that touches MCP. The precedent is
`resolve_department`, which folds its grant check into an existing query and
measures 0.518 ms against a multi-second turn; `get_current_user` already reads
the user row on every request, so the request is DB-bound regardless.
"""

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..db.session import get_session
from ..users.models import User
from . import repository as repo
from .grants import McpIdentity


async def get_mcp_identity(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> McpIdentity:
    keys = await repo.grant_keys_for(session, user.id)
    # NOTE: user.role is not consulted. A global admin holds no grant
    # implicitly (design §3.4) and must grant themselves explicitly.
    return McpIdentity.from_grants(email=user.email, grant_keys=keys)
```

In `app/mcp/client.py`, replace `_session_headers` and retype the two entry points:

```python
    def _session_headers(self, identity: McpIdentity | None) -> dict[str, str]:
        """Auth header plus the caller's identity and grants.

        The gateway is the front door, so it — not the model — asserts who is
        asking and what they hold. The identity is a per-call ARGUMENT and never
        client state: this object is a process-wide singleton, so storing it
        would let two concurrent turns race each other's grants.

        A fresh dict every call, for the same reason.
        """
        headers = dict(self._auth_headers)
        headers.update(grants.header_values(identity))
        return headers
```

with `from . import grants` and `from .grants import McpIdentity` at the top. Then change the two public entry points from `user_email: str | None = None` to `identity: McpIdentity | None = None`, passing it straight to `_session_headers(identity)`:

```python
    @asynccontextmanager
    async def session(self, *, identity: McpIdentity | None = None) -> AsyncIterator[ClientSession]:
```
```python
    async def describe(self, *, identity: McpIdentity | None = None) -> ToolSet:
        async with self.session(identity=identity) as session:
            return await self.load_toolset(session)
```

Update the `session()` docstring's `user_email` sentence to describe the grant headers.

In `app/agent/loop.py`, `stream_turn` and `run_turn`: replace the `user_email: str | None = None` parameter with `identity: McpIdentity | None = None`, and `mcp.session(user_email=user_email)` with `mcp.session(identity=identity)`. `run_turn` passes it through to `stream_turn`.

In `app/tools/router.py` and `app/mcp/router.py`: add the dependency and use it.

```python
from ..mcp.dependencies import get_mcp_identity
from ..mcp.grants import McpIdentity
```
```python
async def list_tools(
    request: Request,
    user: User = Depends(get_current_user),
    identity: McpIdentity = Depends(get_mcp_identity),
) -> ToolsResponse:
```
and `await mcp.describe(identity=identity)`.

`app/mcp/router.py` takes the same two edits inside `mcp_status`.

In `app/chat/router.py`: add `identity: McpIdentity = Depends(get_mcp_identity)` to the `chat` signature and pass `identity=identity` wherever `user_email=user.email` currently reaches `stream_turn` / `run_turn`. `McpIdentity` is a frozen dataclass, so closing over it inside the streaming generator is safe — unlike `turn_files` and `rag_context`, it is **not** a contextvar and needs no set-inside-the-generator treatment.

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_mcp_grants.py tests/test_agent_loop.py tests/test_chat_auth.py -v
.venv/bin/pytest -q
```
Expected: the new tests PASS and nothing else regresses. **Compare the skip count against a pre-change run** — CLAUDE.md warns the shared `_auth` helpers skip on auth failure, so a broken signature can turn ~86 tests into silent skips that a green run hides.

Then verify end to end against a running MCP server, which is the only check that proves the wire format:

```bash
# terminal 1, in local-llm-mcp
npm start
# terminal 2, in the gateway
.venv/bin/uvicorn app.main:app --port 8000
# terminal 3 — as an admin with NO grants, then with mcp-hrms
curl -s localhost:8000/v1/tools -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```
Expected: with no grants the MCP-backed tools are only `get_server_time`; after
`POST /v1/users/{id}/mcp-grants {"grant_key":"mcp-hrms"}` the three HRMS tools appear
and `get_hrms_employee_tasks` does not.

- [ ] **Step 5: Commit**

```bash
git add app/mcp/ app/tools/router.py app/chat/router.py app/agent/loop.py tests/test_mcp_grants.py
git commit -m "feat(mcp): forward the caller's grants on every MCP request"
```

---

### Task 12: Refusal logging and the grants-held / tools-available pairing

**Files:**
- Modify: `local-llm-mcp/src/auth/service-token.ts`
- Modify: `local-llm-mcp/src/tools/hrms/employee-details.ts`
- Modify: `app/agent/loop.py`
- Modify: `tests/test_mcp_grants.py`

**Interfaces:**
- Consumes: `McpIdentity` (Task 8), the withheld branch (Task 4).
- Produces: no new API — three log lines that make the spec §10 metrics observable.

Spec §10's success metric is two-sided, and only one side is loud. "A tool ran for someone without the grant" would be a visible bug. "A tool the caller was entitled to never appeared" presents as a clean deployment where the user simply never gets their answer — the §18 failure class. Making it observable needs **grants-held and tools-available in the same place**, which today live on opposite sides of the wire.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_grants.py`:

Add `from app.agent.loop import describe_identity` to the file's existing
imports, then:

```python
def test_the_log_line_pairs_grants_held_with_tools_available():
    """The §10 metric: a lost capability is only detectable if the grants and
    the resulting tool list appear together. Two log lines on opposite sides of
    the wire cannot be correlated after the fact."""
    identity = grants.McpIdentity.from_grants(
        email="person@example.com", grant_keys=["mcp-hrms", "mcp.hrms.full"]
    )
    rendered = describe_identity(identity)
    assert "person@example.com" in rendered
    assert "mcp-hrms" in rendered
    assert "mcp.hrms.full" in rendered


def test_it_says_no_grants_rather_than_printing_an_empty_list():
    """`roles=[]` reads as a logging artefact; "no grants" reads as the fact it
    is — which is what someone scanning for a mis-provisioned account needs."""
    identity = grants.McpIdentity.from_grants(email="a@b.c", grant_keys=[])
    assert "no grants" in describe_identity(identity)


def test_it_handles_no_identity_at_all():
    assert describe_identity(None) == "no identity"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_grants.py -v -k describe_identity`
Expected: FAIL — `cannot import name 'describe_identity' from 'app.agent.loop'`.

- [ ] **Step 3: Write minimal implementation**

In `app/agent/loop.py`, add the renderer beside the existing log call:

```python
def describe_identity(identity: McpIdentity | None) -> str:
    """One-line rendering of who is asking and what they hold.

    Logged next to the tool count so a MISSING capability is detectable. The
    loud failure (a tool ran without its grant) would show up on its own; the
    quiet one — an entitled user whose tool never appeared — is only visible if
    the grants and the resulting list sit in the same line.
    """
    if identity is None:
        return "no identity"
    held = sorted(identity.roles) + sorted(identity.permissions)
    who = identity.email or "anonymous"
    return f"{who} ({', '.join(held) if held else 'no grants'})"
```

and change both `logger.info("agent run: %d tool(s) available %s", ...)` calls in `stream_turn` to include it:

```python
            logger.info(
                "agent run: %s -> %d tool(s) available %s",
                describe_identity(identity),
                len(registry.tool_names()),
                registry.tool_names(),
            )
```

In `local-llm-mcp/src/auth/service-token.ts`, log what each session resolved to, immediately before returning it:

```ts
    const session: ServiceSession = {
      userEmail: firstHeader(request.headers['x-user-email']) ?? null,
      roles: parseGrants(request.headers['x-user-roles']),
      permissions: parseGrants(request.headers['x-user-permissions']),
    };
    // The other half of the gateway's paired log line. Cheap, and the only
    // record that a caller arrived with no grants because the gateway sent
    // none — as opposed to because they hold none.
    console.log(
      `mcp session: ${session.userEmail ?? 'anonymous'} ` +
        `roles=[${[...session.roles].sort().join(',')}] ` +
        `permissions=[${[...session.permissions].sort().join(',')}]`,
    );
    return session;
```

In `local-llm-mcp/src/tools/hrms/employee-details.ts`, log the withheld case inside the branch Task 4 added:

```ts
      if (wantsFull && !maySeeFull) {
        // Spec §10 feedback capture: an over-tight grant shows up as one
        // account's repeated refusals for one key, which is actionable, rather
        // than as a capability nobody noticed was gone.
        console.warn(
          `hrms full detail withheld for ${session?.userEmail ?? 'unknown'}: ` +
            `missing ${MCP_PERMISSIONS.HRMS_FULL}`,
        );
      }
```

The two MCP-side lines are log statements with no branching logic, so they carry no test of their own — asserting on `console` output would test Node's stdout rather than a decision. The gateway-side renderer *is* a pure function with three cases, which is why it has one.

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_mcp_grants.py -v
cd /home/manoj/newlaptop/projects/node/local-llm-mcp && npm test && npm run typecheck
```
Expected: PASS on both sides. Then confirm the pairing is actually visible: run one chat turn against a running MCP server and grep the gateway log for `agent run:` — it must name the caller, their grants, and the tool count on one line.

- [ ] **Step 5: Commit**

```bash
git add app/agent/loop.py tests/test_mcp_grants.py
git commit -m "feat(mcp): log grants held beside tools available so a lost capability is visible"
```

(The two MCP-server log lines commit in that repo.)

---

### Task 13: Documentation

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-08-24-mcp-role-based-tool-access-design.md` (status line only)
- Modify: `local-llm-mcp/README.md`
- Modify: `local-llm-mcp/CLAUDE.md`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing executable.

- [ ] **Step 1: Write the failing test**

`tests/test_env_templates.py` already checks that documented env vars exist in the templates. Confirm it covers `MCP_ENABLE_DEV_TOOLS` on the MCP side or that the MCP side is out of its scope:

Run: `.venv/bin/pytest tests/test_env_templates.py -v`
Expected: PASS (this task adds no gateway env var — the grants live in Postgres, not in `.env`).

- [ ] **Step 2: Record the gotchas in the gateway's CLAUDE.md**

Add to the **Conventions / gotchas** list:

```markdown
- **MCP tool access is a per-user GRANT, and the gateway keeps no tool→grant
  map.** Six strings live in `app/mcp/grants.py` (`mcp-hrms`/`mcp-izone`/`mcp-ems`
  name a SYSTEM; `mcp.hrms.full`/`mcp.hrms.tasks`/`mcp.ems.query` name a SHARP
  EDGE inside one), stored in `user_mcp_grants` behind
  `ck_user_mcp_grants_key`, administered by `GET|POST|DELETE
  /v1/users/{id}/mcp-grants` (admin-only, `granted_by` on every row), and
  forwarded as `x-user-roles`/`x-user-permissions` beside the existing
  `x-user-email`. Five things a rewrite must not lose: (1) **the gateway does
  NOT know which grant gates which tool** — FastMCP applies `canAccess` at
  session construction, so `tools/list` already returns the authorized set, and
  a second copy of that mapping here would drift in the silent direction
  ("gateway hides a tool the MCP would allow" is a lost capability with no error
  on either side, the §18 failure class); (2) **a global admin holds no grant
  implicitly** — deliberately unlike `permissions.effective_level`, because
  gateway admin is an IT/ops role and auto-conferring `Salary_Level` plus an
  expenses SQL console on whoever operates the gateway is the escalation an
  audit objects to, so admins grant themselves explicitly and leave a row;
  (3) the identity is a **per-call argument to `MCPClient`, never client
  state** — it is a process-wide singleton and two concurrent turns would race
  each other's grants; (4) **absent headers mean NO grants**, inverting
  `app/rag/ranking.py`'s fail-open rule for the reason `app/nrb/` fails closed —
  withholding an answer asserts something false, withholding a tool withholds
  somebody's salary data; (5) an **unknown grant key is dropped, never raised**
  — the two sides deploy independently and version skew must cost access, not
  produce a 500. `McpIdentity` is a frozen dataclass and NOT a contextvar, so
  unlike `turn_files`/`rag_context` it needs no set-inside-the-generator care.
- **`x-user-roles` is only as trustworthy as the MCP token, and that is the
  documented deployment contract.** `local-llm-mcp` binds `127.0.0.1` and its
  README already states that possession of the shared token grants every tool.
  The gateway holds that token and its users never see it, so a chat user cannot
  forge a grant; `canAccess` additionally covers a gateway filtering bug or a
  second client added later. **If that server is ever bound to `0.0.0.0`, the
  header becomes forgeable by any token holder and the transport must move to a
  signed assertion or a callback** — the same class of deployment prerequisite as
  `docs/external-api.md`'s `--root-path` note.
```

Add to the **Endpoints** section under Authed (JWT):

```markdown
`GET|POST|DELETE /v1/users/{id}/mcp-grants` (admin; per-user MCP tool grants —
POST is idempotent and does NOT rewrite `granted_at`, DELETE is 204 whether or
not the row existed, 422 an unknown grant key or an extra field, 404 unknown
user),
```

- [ ] **Step 3: Document the MCP server side**

In `local-llm-mcp/README.md`, replace the paragraph beginning "`X-User-Email` is accepted when supplied…" with a Roles section:

```markdown
## Roles and permissions

Every MCP request carries the caller's grants, asserted by the gateway in
`x-user-roles` and `x-user-permissions` (comma-separated) beside `x-user-email`.
Tools declare `canAccess`, which FastMCP applies when the session is built — an
ungranted tool is never registered, so it is invisible to `tools/list` and
unknown to `tools/call`.

| grant | reaches |
|---|---|
| `mcp-hrms` | `list_hrms_employees`, `list_hrms_departments`, `get_hrms_employee_details` (compact summary) |
| `mcp.hrms.full` | full employee detail — 80+ fields including `Salary_Level` |
| `mcp.hrms.tasks` | `get_hrms_employee_tasks` |
| `mcp-izone` | all four iZone tools |
| `mcp-ems` | `list_ems_tables` |
| `mcp.ems.query` | `search_ems_records` |

A permission never implies its role: `search_ems_records` needs `mcp-ems` AND
`mcp.ems.query`, and `get_hrms_employee_tasks` needs `mcp-hrms` AND
`mcp.hrms.tasks`.

`get_server_time` is ungated. `get_echo` and `list_examples` are development
tools and are registered only under `MCP_ENABLE_DEV_TOOLS=true`.

**`mcp-izone` is coarse by nature.** `list_izone_list_items` reads any
SharePoint list by exact title, so the grant is effectively "everything the
iZone service account can see". Narrowing it means changing the tool — an
allowlist of list titles — not the role vocabulary. Do not read `mcp-izone` as
narrower than that.

**Absent grant headers mean no grants**, so a caller the gateway has not
provisioned sees `get_server_time` alone. The shared token remains the trust
boundary: it authenticates the gateway, and the gateway asserts the grants.
Keep this service on localhost or a private network — if it is ever bound
publicly, the headers become forgeable by any token holder and the transport
must move to a signed assertion.
```

In `local-llm-mcp/CLAUDE.md`, add:

```markdown
- **FastMCP's tool filter is FAIL-OPEN: `tool.canAccess ? tool.canAccess(auth)
  : true`.** A tool registered without a `canAccess` is visible to every
  caller, and nothing anywhere reports it. `test/tool-gates.test.ts` enumerates
  the real registration code through a stand-in `addTool` and fails if any tool
  lacks a gate and is not named in `PUBLIC_TOOLS`. Adding a name to that set is
  a security decision.
- **`get_hrms_employee_details` has TWO doors to the 80+ field record.**
  `wantsFull = full || Boolean(employeeNo)`, so gating the `full` parameter
  alone leaves every full record reachable by employee number. The gate is on
  `wantsFull` and lives in `execute`, not `canAccess` — which receives the
  session and never the arguments. An ungranted caller gets the 11-field
  summary plus `fullDetailWithheld`, never a refusal: looking up an employee
  number in the directory is legitimate. The note tells the model to report a
  permission limit rather than a missing field, because "that data is
  unavailable" makes the user conclude the bank does not hold it.
```

- [ ] **Step 4: Flip the spec's status line**

In the spec header, change:

```markdown
**Status:** design, approved in chat; implementation plan not yet written
```
to
```markdown
**Status:** implemented 2026-08-24; see docs/superpowers/plans/2026-08-24-mcp-role-based-tool-access.md
```

Then run the full suites once more in both repos:

```bash
.venv/bin/pytest -q          # gateway
cd /home/manoj/newlaptop/projects/node/local-llm-mcp && npm test && npm run build
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-08-24-mcp-role-based-tool-access-design.md
git commit -m "docs: record the MCP grant model and its two fail-open traps"
```

(The MCP server's README/CLAUDE.md commit happens in that repo.)

---

## Verification checklist

Run before calling this done. Evidence, not assertion.

- [ ] `npm test` in `local-llm-mcp` — all pass, including 86 grant-matrix assertions.
- [ ] `npm run typecheck && npm run build` in `local-llm-mcp` — clean.
- [ ] `.venv/bin/pytest -q` in the gateway — pass count up, **skip count unchanged**.
- [ ] `.venv/bin/alembic heads` prints exactly one head.
- [ ] `.venv/bin/alembic revision --autogenerate -m x` produces an empty migration; delete it.
- [ ] Live check: an admin with no grants sees only `get_server_time` from MCP on `GET /v1/tools`; granting `mcp-hrms` adds exactly three HRMS tools and not `get_hrms_employee_tasks`.
- [ ] Live check: `get_hrms_employee_details` with `employeeNo` set returns 11 fields and `fullDetailWithheld` for a caller without `mcp.hrms.full`.
- [ ] Live check: the gateway log's `agent run:` line names the caller, their grants, and the tool count on ONE line (the §10 metric).
- [ ] Live check: a withheld full-detail request logs `hrms full detail withheld for <email>: missing mcp.hrms.full` on the MCP server.
