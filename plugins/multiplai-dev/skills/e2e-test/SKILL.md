---
name: e2e-test
description: End-to-end testing for web applications. Supports frontend (browser-based) and backend (API) testing modes. Spec-aware — reads OpenSpec artifacts when available, falls back to codebase discovery. Use when the user wants to validate that an application actually works, not just that tests pass.
triggers:
  - "e2e test"
  - "end to end test"
  - "smoke test the app"
  - "test the app"
  - "validate the build"
  - "/e2e-test"
model: opus
effort: high
disable-model-invocation: true
---

# E2E Test Orchestrator

Validate that an application actually works by testing real user journeys — in a browser for frontends, via HTTP for backends.

## Design Principles

1. **Spec-aware:** If OpenSpec artifacts exist, use them as ground truth for what to test. Otherwise, discover from codebase.
2. **Sub-agent per journey:** Each user journey gets its own Task agent to keep context manageable.
3. **Self-healing:** Fix critical blockers (max 3 attempts per journey), document non-critical issues.
4. **Two modes:** Frontend (browser) and Backend (API). Auto-detect or override with `--mode`.

---

## Arguments

Parse the user's invocation:

| Arg | Description | Default |
|-----|-------------|---------|
| `--mode` | `frontend`, `backend`, `both`, `auto` | `auto` |
| `--spec` | Path to OpenSpec change directory | Auto-detect |
| `--base-url` | Application base URL | `http://localhost:3000` (frontend) / `http://localhost:8000` (backend) |
| `--skip-start` | Don't attempt to start the dev server | `false` |
| `--report` | Export markdown report to path | *(none — console only)* |

---

## Phase 1: Pre-flight

### 1.1 Platform Detection

Read project config files to understand the stack:

```
Read: package.json, pyproject.toml, Cargo.toml, go.mod, Makefile, docker-compose.yml
```

Extract:
- **Language/framework** (Next.js, FastAPI, Express, Django, etc.)
- **Dev server command** (from `scripts.dev`, `[tool.taskipy]`, Makefile, etc.)
- **Port** (from config or convention)
- **Database** (from `.env`, ORM config, docker-compose)

### 1.2 Mode Detection

If `--mode auto` (default):

| Found | Mode |
|-------|------|
| Frontend framework files (`src/App.tsx`, `pages/`, `app/`, `index.html`) | `frontend` |
| Backend route files (`routes/`, `api/`, `views.py`, `main.py` with FastAPI/Flask) | `backend` |
| Both frontend AND backend indicators | `both` (run API first, then UI) |
| Neither clearly | Ask user |

### 1.3 Spec Awareness

Check for buildme artifacts:

```
Glob: specs/changes/*/requirements/*.md
```

**If requirements exist:**
- Read each requirements file — these define the features that should work
- Extract user-facing behaviors and acceptance criteria
- Map requirements → test journeys (each may produce 1-3 journeys)

**If no requirements:**
- Fall back to codebase discovery (Phase 2)

### 1.4 Tool Installation Check

**Frontend mode:** Verify `agent-browser` is available:

```bash
which agent-browser
```

**If not available:** Report to the user and fall back to backend-only mode. Do NOT auto-install via npx — installing tools is a side effect that should be explicit, not hidden in a check step. If the user wants to install: `npm install -g @anthropic-ai/agent-browser`.

**Backend mode:** Verify `curl` is available (should always be).

---

## Phase 2: Research (Parallel Sub-agents)

Spawn 3 research agents in parallel using the Task tool:

### Agent 2a: Route Discovery

```
Use the Task tool with subagent_type "Explore":

Prompt:
"""
Discover all routes/endpoints/pages in this application.

For frontend: Find all route definitions (React Router, Next.js pages/app dir, Vue Router, etc.)
For backend: Find all API endpoint definitions (FastAPI routes, Express routes, Django URLs, etc.)

Return a structured list:
- Route/path
- HTTP method (backend) or navigation type (frontend)
- Brief description of what it does
- Auth required? (yes/no/unknown)
"""
```

### Agent 2b: Auth Discovery

```
Use the Task tool with subagent_type "Explore":

Prompt:
"""
Discover the authentication setup for this application.

Look for:
- Auth middleware/guards
- Login/signup endpoints or pages
- Token/session management
- Test credentials in fixtures, seeds, or .env.example
- Auth bypass for testing (if any)

Return:
- Auth type (JWT, session, OAuth, API key, none)
- Login endpoint/page
- How to get test credentials
- Any test-specific auth shortcuts
"""
```

### Agent 2c: Data & State Discovery

```
Use the Task tool with subagent_type "Explore":

Prompt:
"""
Discover the data layer for this application.

Look for:
- Database type and connection config (.env, ORM config, docker-compose)
- Seed data / fixtures
- Key models/entities
- How to reset state between tests

Return:
- DB type (postgres, sqlite, mongo, none)
- Connection method (psql, sqlite3, mongosh, ORM CLI)
- Seed command (if any)
- Key entities to verify after mutations
"""
```

**If specs were found in Phase 1.3:** Skip route discovery (specs are ground truth). Still run auth and data discovery.

---

## Phase 3: Start Application

Unless `--skip-start` is set:

### 3.1 Check if Already Running

```bash
curl -s -o /dev/null -w "%{http_code}" {base_url}/
```

If 200/301/302: App is already running, skip to Phase 4.

### 3.2 Start Dev Server

Run the detected dev server command in the background:

```bash
# Example for Node:
npm run dev &

# Example for Python:
python -m uvicorn main:app --reload &
```

Use the Bash tool with `run_in_background: true`.

### 3.3 Wait for Ready

Poll until the server responds with a success status (max 30 seconds):

```bash
for i in $(seq 1 30); do
  status=$(curl -s -o /dev/null -w "%{http_code}" {base_url}/ 2>/dev/null)
  [ "$status" -ge 200 ] 2>/dev/null && [ "$status" -lt 400 ] 2>/dev/null && break
  sleep 1
done
```

**Note:** Checking the HTTP status code (not just curl's exit code) ensures we don't declare the server ready when it's returning 500 during boot.

If server doesn't start: report error and halt. Don't test against nothing.

---

## Phase 4: Create Test Plan

### From Specs (preferred)

For each spec, generate test journeys:

```
Spec: "User Registration"
→ Journey 1: Happy path registration (fill form, submit, verify account created)
→ Journey 2: Validation errors (empty fields, invalid email, duplicate email)
→ Journey 3: Post-registration state (user appears in DB, welcome email sent)
```

### From Discovery (fallback)

Generate journeys from discovered routes:

```
Routes: GET /, POST /api/auth/login, GET /api/users, POST /api/users, GET /api/users/:id
→ Journey 1: Health check (GET / returns 200)
→ Journey 2: Auth lifecycle (register → login → access protected route → logout)
→ Journey 3: CRUD lifecycle (create user → list users → get user → verify DB)
```

**Priority order:**
1. Auth journeys (if auth exists)
2. Core CRUD journeys
3. Edge cases and error handling
4. Static pages / health checks

Create a TaskCreate entry for each journey to track progress.

---

## Phase 5: Execute Journeys

For each journey, spawn a dedicated sub-agent. Run journeys sequentially (earlier journeys may set up state needed by later ones).

**State management between journeys:**
- If a journey involves mutations AND a reset mechanism was discovered in Phase 2c (seed command, migration reset, etc.), reset state before the journey starts.
- If no reset mechanism exists, design journeys to be **additive** — each builds on prior state rather than assuming a clean DB.
- Document the chosen strategy in the report.

### Mode A: Frontend Journey Agent

```
Use the Task tool with subagent_type "general-purpose":

Prompt:
"""
# Frontend E2E Journey Agent

You are testing a user journey in a web application using the agent-browser CLI tool.

## Journey
{journey_description}

## Application
- Base URL: {base_url}
- Framework: {framework}
- Auth: {auth_info}

## Instructions

### Browser Commands Reference
- `agent-browser open {url}` — Navigate to URL
- `agent-browser snapshot` — Get current page state (DOM summary + screenshot)
- `agent-browser click {selector}` — Click an element
- `agent-browser fill {selector} {value}` — Fill an input field
- `agent-browser screenshot {path}` — Save screenshot to file

### Testing Protocol

1. **Navigate** to the starting page:
   ```bash
   agent-browser open {base_url}{start_path}
   ```

2. **Snapshot** to see current state:
   ```bash
   agent-browser snapshot
   ```

3. **Execute** the journey step by step:
   - For each action: perform it, then snapshot to verify the result
   - ALWAYS re-snapshot after navigation or form submission
   - Save screenshots at key moments to `tests/e2e-screenshots/`

4. **Verify** expected outcomes:
   - Page content matches expectations
   - Navigation works correctly
   - Forms submit successfully
   - Error states display properly

5. **Responsive check** (if journey involves layout):
   - Test at mobile width (375px) and desktop width (1280px)

6. **DB Verification** (if journey mutates data):
   {db_verification_instructions}

### Self-Healing Protocol

If a step fails:
1. Screenshot the failure state
2. Re-read the page DOM to understand what's actually rendered
3. Adjust selector or approach (element may have different class/id than expected)
4. Retry (max 3 attempts per step)
5. If still failing: document as issue and continue to next step

### Output

Return:
- **Status:** PASS / FAIL / PARTIAL
- **Steps completed:** N/M
- **Issues found:** List with severity
- **Issues fixed:** List of self-healed problems
- **Screenshots:** Paths to saved screenshots
- **DB verification:** Results of any data checks
"""
```

### Mode B: Backend Journey Agent

```
Use the Task tool with subagent_type "general-purpose":

Prompt:
"""
# Backend E2E Journey Agent

You are testing an API journey by making HTTP requests.

## Journey
{journey_description}

## Application
- Base URL: {base_url}
- Framework: {framework}
- Auth: {auth_info}

## Instructions

### HTTP Testing Protocol

Use curl for all requests:
```bash
curl -s -w "\n%{http_code}" -X METHOD {base_url}/path \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer {token}" \
  -d '{"key": "value"}'
```

Always capture both body and status code.

### Testing Steps

1. **Happy path first:**
   - Execute the journey's primary flow
   - Verify response status codes (200, 201, etc.)
   - Verify response body structure and key values
   - Save auth tokens for subsequent requests

2. **Error cases:**
   - Missing required fields → expect 400/422
   - Invalid auth → expect 401
   - Non-existent resources → expect 404
   - Duplicate creation → expect 409

3. **Contract testing:**
   - Verify response shapes match expected schemas
   - Check Content-Type headers
   - Verify pagination structure if applicable

4. **Auth lifecycle** (if applicable):
   - Register/login → get token
   - Use token for protected endpoints
   - Verify expired/invalid token is rejected
   - Test refresh flow if it exists

5. **DB Verification** (if journey mutates data):
   {db_verification_instructions}

### Self-Healing Protocol

If a request fails unexpectedly:
1. Check if the endpoint path is correct (read route definitions)
2. Check if request body format is correct (read model/schema definitions)
3. Adjust and retry (max 3 attempts per request)
4. If still failing: document as issue and continue

### Output

Return:
- **Status:** PASS / FAIL / PARTIAL
- **Requests made:** N (pass/fail breakdown)
- **Issues found:** List with severity
- **Issues fixed:** List of self-healed problems
- **Contract mismatches:** Any unexpected response shapes
- **DB verification:** Results of any data checks
"""
```

### DB Verification Instructions (injected into journey agents)

Generate based on Phase 2c discovery:

**PostgreSQL:**
```bash
psql {connection_string} -c "SELECT count(*) FROM {table} WHERE {condition};"
```

**SQLite:**
```bash
sqlite3 {db_path} "SELECT count(*) FROM {table} WHERE {condition};"
```

**No direct DB access:**
```
Verify via API: GET /api/{resource} and check the mutation is reflected.
```

---

## Phase 6: Cleanup & Report

### 6.1 Stop Dev Server

If we started the dev server in Phase 3.2, stop it using the task ID returned by the `run_in_background` Bash call:

```
Use the TaskStop tool with the task_id saved from Phase 3.2.
```

If TaskStop is unavailable, fall back to PID-based cleanup:
```bash
kill $DEV_SERVER_PID 2>/dev/null || true
```

**Important:** Save the task ID or PID when starting the server in Phase 3.2 — don't rely on job control numbering (`%1`), which is fragile when multiple background tasks exist.

### 6.2 Compile Report

Aggregate results from all journey agents into a structured report:

```markdown
# E2E Test Report

## Summary
- **Mode:** {frontend|backend|both}
- **Journeys tested:** {N}
- **Passed:** {N} | **Failed:** {N} | **Partial:** {N}
- **Issues found:** {N} ({N} fixed during testing)
- **Spec coverage:** {N}/{M} specs covered (if spec-aware)

## Journey Results

### Journey 1: {name}
- **Status:** PASS/FAIL/PARTIAL
- **Steps:** {completed}/{total}
- **Issues:** {list}
- **Screenshots:** {paths} (frontend only)

### Journey 2: {name}
...

## Issues Summary

### Fixed During Testing
| # | Journey | Issue | Fix Applied |
|---|---------|-------|-------------|
| 1 | ... | ... | ... |

### Remaining Issues
| # | Severity | Journey | Issue | Suggested Fix |
|---|----------|---------|-------|---------------|
| 1 | ... | ... | ... | ... |

## DB Validation Summary
| Check | Table | Condition | Result |
|-------|-------|-----------|--------|
| ... | ... | ... | PASS/FAIL |

## Screenshots
| Journey | Step | Path |
|---------|------|------|
| ... | ... | `tests/e2e-screenshots/...` |
```

### 6.3 Export (Optional)

If `--report` was specified, write the report to the given path.

Otherwise, output the report to the console.

### 6.4 Final Test Suite Check

After E2E testing, verify the unit/integration tests still pass:

```bash
{test_command}
```

This catches any accidental side effects from E2E testing (e.g., leftover test data).

---

## Integration with Autonomous TDD

When invoked from the autonomous-tdd pipeline (Step 8), this skill receives additional context:

- **OpenSpec change path** — for spec-aware mode
- **Summary of completed tasks** — for understanding what was built
- **Test command** — reuse the project's configured test runner

The autonomous-tdd orchestrator checks the E2E report status and includes it in the final build output.

---

## Limitations

1. **Frontend mode requires agent-browser** — Falls back to backend-only if unavailable
2. **No parallel journeys** — Sequential execution to avoid state conflicts
3. **DB verification is best-effort** — Requires direct DB access tools installed
4. **Self-healing is limited** — 3 attempts per step, then documents and moves on
5. **No visual regression** — Screenshots are for debugging, not pixel-diff comparison
