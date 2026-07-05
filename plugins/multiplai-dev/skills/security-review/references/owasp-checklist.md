# OWASP Security Checklist

Load when performing deep (`--depth deep`) security reviews, or when the codebase handles auth, user data, or external integrations.

## OWASP Top 10:2025

| # | Vulnerability | Key Prevention | What to Look For |
|---|--------------|----------------|-----------------|
| A01 | Broken Access Control | Deny by default, enforce server-side, verify ownership | Missing authz checks, IDOR, path traversal, CORS misconfig, metadata manipulation |
| A02 | Security Misconfiguration | Harden configs, disable defaults, minimize features | Debug mode on, default credentials, unnecessary features enabled, missing security headers, verbose errors |
| A03 | Supply Chain Failures | Lock versions, verify integrity, audit dependencies | Unpinned deps, no lockfile, unverified packages, missing SRI for CDN, no SBOM |
| A04 | Cryptographic Failures | TLS 1.2+, AES-256-GCM, Argon2/bcrypt for passwords | MD5/SHA1 for security, ECB mode, hardcoded keys, weak RNG, plaintext storage, missing TLS |
| A05 | Injection | Parameterized queries, input validation, safe APIs | String concat in SQL, `eval()`, `os.system()`, template injection, LDAP injection, XXE |
| A06 | Insecure Design | Threat model, rate limit, design security controls | Missing rate limits, no abuse case consideration, trust boundaries not defined, business logic flaws |
| A07 | Auth Failures | MFA, check breached passwords, secure sessions | Weak password policy, no brute-force protection, session fixation, credential stuffing exposure |
| A08 | Integrity Failures | Sign packages, SRI for CDN, safe serialization | Unsigned updates, unsafe deserialization (pickle, Marshal, BinaryFormatter), missing integrity checks on CI/CD |
| A09 | Logging Failures | Log security events, structured format, alerting | No auth event logging, sensitive data in logs, no monitoring/alerting, logs not tamper-protected |
| A10 | Exception Handling | Fail-closed, hide internals, log with context | Stack traces in responses, fail-open on error, generic catch-all without logging, missing error IDs |

### How to Use During Review

For each finding, map to the relevant A0X category. This enables:
- Consistent severity calibration across reviews
- Tracking recurring vulnerability patterns
- Alignment with industry-standard reporting

## ASVS 5.0 — Tiered Requirements

Use ASVS levels to calibrate review depth based on application criticality.

### Level 1: All Applications

Every application should meet these minimums:

**Authentication:**
- [ ] Passwords minimum 12 characters
- [ ] Checked against breached password lists (e.g., HaveIBeenPwned)
- [ ] Rate limiting on authentication endpoints
- [ ] Session tokens 128+ bits entropy
- [ ] Sessions invalidated on logout and password change

**Transport:**
- [ ] HTTPS everywhere (no mixed content)
- [ ] HSTS header present
- [ ] No sensitive data in URL parameters

**Input:**
- [ ] All user input validated server-side
- [ ] Output encoding for context (HTML, JS, URL, CSS)
- [ ] Parameterized queries for all database access

**Error Handling:**
- [ ] No stack traces or internal details in error responses
- [ ] Fail-closed on all error paths
- [ ] Security events logged (login, failed login, access denied)

### Level 2: Sensitive Data Applications

All L1 requirements plus:

**Authentication:**
- [ ] MFA available for sensitive operations
- [ ] Account lockout or progressive delays after failed attempts
- [ ] Password change requires current password

**Cryptography:**
- [ ] Cryptographic key management documented
- [ ] No deprecated algorithms (MD5, SHA1, DES, RC4)
- [ ] Secrets stored in vault/env, never in code

**Access Control:**
- [ ] Role-based access control (RBAC) consistently enforced
- [ ] Every API endpoint has explicit authorization check
- [ ] Indirect object references (not sequential IDs)

**Logging:**
- [ ] Comprehensive security event logging
- [ ] Log injection prevention
- [ ] Alerting on suspicious patterns (multiple failed logins, privilege escalation attempts)

**Data Protection:**
- [ ] Sensitive data encrypted at rest
- [ ] PII handling documented and minimized
- [ ] Data retention policy implemented

### Level 3: Critical Systems

All L1/L2 requirements plus:

- [ ] Hardware security modules (HSM) for key management
- [ ] Threat model documented and maintained
- [ ] Advanced monitoring and anomaly detection
- [ ] Penetration testing validation (annual minimum)
- [ ] Incident response plan tested
- [ ] Code signing for all deployments
- [ ] Supply chain security controls (SBOM, dependency review)

### Choosing the Right Level

| Application Type | ASVS Level | Examples |
|-----------------|:---:|---------|
| Internal tools, prototypes, personal projects | L1 | Admin dashboards, scripts, dev tools |
| User-facing apps with personal data | L2 | SaaS products, e-commerce, social apps |
| Financial, healthcare, critical infrastructure | L3 | Payment processing, medical records, auth providers |

## Agentic AI Security (OWASP 2026)

When reviewing AI agent systems, LLM integrations, or MCP servers, check for these risks:

| ID | Risk | Description | Mitigation |
|----|------|-------------|------------|
| ASI01 | Goal Hijack | Prompt injection alters agent objectives | Input sanitization, goal boundaries, behavioral monitoring, system prompt protection |
| ASI02 | Tool Misuse | Agent uses tools in unintended ways | Least privilege per tool, fine-grained permissions, validate all tool I/O, allowlist operations |
| ASI03 | Privilege Abuse | Credential escalation across agents | Short-lived scoped tokens, identity verification per request, no shared credentials |
| ASI04 | Supply Chain | Compromised plugins/MCP servers | Verify signatures, sandbox plugins, allowlist trusted sources, audit plugin code |
| ASI05 | Code Execution | Unsafe code generation or execution | Sandbox all execution, static analysis before run, human approval for sensitive ops |
| ASI06 | Memory Poisoning | Corrupted RAG/context data | Validate stored content, segment by trust level, integrity checks on retrieval |
| ASI07 | Agent Comms | Spoofing between agents | Authenticate all inter-agent messages, encrypt in transit, verify message integrity |
| ASI08 | Cascading Failures | Errors propagate across system | Circuit breakers, graceful degradation, isolation between agent components, timeout on all calls |
| ASI09 | Trust Exploitation | Social engineering via AI | Label AI-generated content, user education, verification steps for sensitive actions |
| ASI10 | Rogue Agents | Compromised agent acting maliciously | Behavior monitoring, kill switches, anomaly detection, audit trails on all actions |

### Agent Security Checklist

- [ ] All agent inputs sanitized and validated
- [ ] Tools operate with minimum required permissions
- [ ] Credentials are short-lived and scoped per operation
- [ ] Third-party plugins/MCP servers verified and sandboxed
- [ ] Code execution happens in isolated environments
- [ ] Agent communications authenticated and encrypted
- [ ] Circuit breakers between agent components
- [ ] Human approval gates for sensitive operations (financial, destructive, external)
- [ ] Behavior monitoring with anomaly detection
- [ ] Kill switch available for all agent systems
- [ ] Audit trail captures all tool invocations and decisions

### When to Apply Agent Security

- Building MCP servers or Claude tools
- Integrating LLMs with external APIs
- Multi-agent orchestration systems
- RAG pipelines with external data sources
- Any system where AI agents can take real-world actions (send emails, modify databases, execute code)

## Secure Code Patterns — Quick Reference

### Fail-Closed (Critical Pattern)

```python
# DANGEROUS: Fail-open
def check_permission(user, resource):
    try:
        return auth_service.check(user, resource)
    except Exception:
        return True  # Attacker triggers exception → full access

# SAFE: Fail-closed
def check_permission(user, resource):
    try:
        return auth_service.check(user, resource)
    except Exception as e:
        logger.error(f"Auth check failed: {e}")
        return False  # Deny on error
```

### Access Control

```python
# DANGEROUS: No authorization
@app.route('/api/user/<user_id>')
def get_user(user_id):
    return db.get_user(user_id)  # Any user can access any other user

# SAFE: Authorization enforced
@app.route('/api/user/<user_id>')
@login_required
def get_user(user_id):
    if current_user.id != user_id and not current_user.is_admin:
        abort(403)
    return db.get_user(user_id)
```

### Error Handling

```python
# DANGEROUS: Leaks internals
@app.errorhandler(Exception)
def handle_error(e):
    return str(e), 500  # Stack trace, SQL errors, file paths visible

# SAFE: Opaque to user, detailed in logs
@app.errorhandler(Exception)
def handle_error(e):
    error_id = uuid.uuid4()
    logger.exception(f"Error {error_id}: {e}")
    return {"error": "An error occurred", "id": str(error_id)}, 500
```

### Password Storage

```python
# DANGEROUS
hashlib.md5(password.encode()).hexdigest()

# SAFE
from argon2 import PasswordHasher
PasswordHasher().hash(password)
```

### SQL Injection

```python
# DANGEROUS
cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")

# SAFE
cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
```

### Command Injection

```python
# DANGEROUS
os.system(f"convert {filename} output.png")

# SAFE
subprocess.run(["convert", filename, "output.png"], shell=False)
```
