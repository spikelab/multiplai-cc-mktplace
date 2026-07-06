---
name: devops-gcp
description: |
  Working operator's knowledge of Google Cloud Platform for DevOps tasks —
  Cloud Run, Cloud SQL, IAM, Terraform, Cloud Logging/Monitoring, Cloud
  Storage, networking, and multi-project setups. Load when the task touches
  GCP. For project-specific conventions, also load the matching reference
  file under `references/conventions/`.
when_to_use: 'Triggers: gcloud, gcp, google cloud, cloud run, cloud sql, terraform (GCP context), IAM policy / service account (GCP context), cloud logging / monitoring, artifact registry, workload identity federation / WIF'
model: opus
effort: high
---

# DevOps GCP Skill

A working operator's reference for Google Cloud. The skill itself stays
short — extensive material lives in `references/` and is loaded on demand
based on the task.

## When to load what

| Task | Load reference |
|------|----------------|
| Cloud Run deploy / debug / scaling | `references/gcp-serverless.md`, `references/cloud-run-sa-scoping.md` |
| Cloud SQL ops, schema migration | `references/gcp-databases.md`, `references/mariadb-cloudsql-migration.md` |
| IAM, SA design, audit | `references/gcp-iam-security.md`, `references/cloud-run-sa-scoping.md` |
| Terraform multi-env, state, WIF | `references/terraform-multi-env.md` |
| VM, GKE, Compute Engine | `references/gcp-compute.md` |
| VPC, peering, firewalls, private services | `references/gcp-networking.md` |
| Cloud Storage, signed URLs, lifecycle policies | `references/gcp-storage.md` |
| Multi-project architecture, isolation boundaries | `references/project-isolation.md` |
| Read-only Claude Code access to GCP | `references/claudecode-ro-debug-setup.md` |
| **Project-specific conventions** | `gcp-conventions.md` discovered in the project tree (see below) |

The 6 cc-polymath files (compute, databases, iam-security, networking,
serverless, storage) cover the general patterns. The remaining files cover
specific patterns (SA scoping, migration playbook, project isolation,
multi-env Terraform). Don't preload — references total ~5,000 lines.

## Project conventions (auto-discover)

Before doing any GCP work, search upward from the current working directory
for a file named `gcp-conventions.md` and load it if found. This file lives
with the project that owns those conventions (not in the skill), so it
stays in sync as the project evolves.

```bash
# From $PWD, walk up to /, stop at the first match:
d=$PWD; while [ "$d" != "/" ]; do
  [ -f "$d/gcp-conventions.md" ] && { echo "$d/gcp-conventions.md"; break; }
  d=$(dirname "$d")
done
```

If found, **read it before proceeding** — it overrides or refines anything
in this skill (region pinning, SA naming, environment names, escalation
rules, repo layout, design invariants).

If absent, proceed with the generic patterns below; surface a note to the
user that no conventions file was found, in case they want to create one.

## Core operational principles

**1. Project = blast radius.** Use separate GCP projects to isolate
environments (dev/staging/prod) and tenants. IAM bindings, quotas, billing,
and audit trails are project-scoped. Don't multi-tenant inside a single
project unless you've explicitly accepted the trade-offs.

**2. Service accounts are identities, not roles.** One SA per workload
(runtime, deployer, debugger, batch job). Grant the minimum roles at the
minimum scope (project < folder < org). Never reuse a runtime SA across
workloads with different blast radii.

**3. Region pinning is forever.** Pick a region per environment and stick
to it. Cross-region data transfer is expensive, latency is real, and
managed services (Cloud Run, Cloud SQL) cannot be moved without recreate.

**4. Image deployment is orthogonal to infra.** Cloud Run service shape
(scaling, SA, memory, domains) belongs in Terraform with
`lifecycle { ignore_changes = [image] }`. CI/CD owns the `--image=` swap
via `gcloud run deploy`. This lets `terraform apply` and `git push` happen
independently without one stomping the other.

**5. State is sacred.** Terraform state goes in a per-environment GCS
bucket with versioning enabled. Never edit state by hand — use
`terraform state mv/import/rm` and commit the resulting code change.
Lock the bucket with uniform access and tight IAM.

**6. Workload Identity Federation > static keys.** For CI/CD (GitHub
Actions, GitLab, etc.), use WIF + short-lived OIDC tokens, not SA JSON
keys. SA keys are long-lived secrets; treat any creation of one as a
security event with explicit justification.

**7. Logs leak secrets.** `roles/logging.viewer` exposes whatever apps
write to stdout/stderr — JWTs, request bodies, stack traces with creds.
Either scrub at the source or accept that "read-only logging" is
effectively "read-anything-the-app-logged."

**8. Secret Manager is the boundary, not roles/viewer.** `roles/viewer`
lists secrets but doesn't expose payloads (no
`secretmanager.versions.access`). Granting `secretAccessor` is a security
decision — do it on individual secrets, not project-wide.

## Day-to-day command patterns

These apply to any GCP project. Substitute `<project>`, `<region>`,
`<service>`, etc.

### Authentication & context

```bash
# Use a SA key without polluting your default gcloud config:
export CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=/path/to/key.json
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json   # for SDKs

# Or use a named configuration:
gcloud config configurations create <name>
gcloud config configurations activate <name>
gcloud config set project <project>
gcloud config set core/account <sa-email>

# Verify which identity is active:
gcloud auth list
gcloud config list
```

### Reading logs without the console

```bash
# Cloud Run service errors, last 100 lines, JSON:
gcloud logging read \
  'resource.type="cloud_run_revision"
   AND resource.labels.service_name="<service>"
   AND severity>=ERROR' \
  --project=<project> --limit=100 --format=json

# Container startup probe failures (very common):
gcloud logging read \
  'resource.type="cloud_run_revision"
   AND textPayload:"STARTUP HTTP probe failed"' \
  --project=<project> --limit=20

# Tail logs (recent + follow-ish via --freshness):
gcloud logging read \
  'resource.type="cloud_run_revision"
   AND resource.labels.service_name="<service>"' \
  --project=<project> --freshness=5m --order=asc
```

### Cloud Run inspection & deploy

```bash
# Current revisions and traffic split:
gcloud run services describe <service> \
  --region=<region> --project=<project> \
  --format='table(status.traffic[].revisionName, status.traffic[].percent)'

# Image swap (the CI/CD pattern):
gcloud run deploy <service> \
  --image=<region>-docker.pkg.dev/<project>/<repo>/<image>:<tag> \
  --region=<region> --project=<project>

# Roll back to the previous revision:
gcloud run services update-traffic <service> \
  --to-revisions=<prev-revision>=100 \
  --region=<region> --project=<project>
```

### IAM inspection

```bash
# Who has what on this project:
gcloud projects get-iam-policy <project> --format=json \
  | jq -r '.bindings[] | "\(.role)\t\(.members | join(","))"'

# What roles does this principal have?
gcloud projects get-iam-policy <project> \
  --flatten="bindings[].members" \
  --filter="bindings.members:<principal>" \
  --format="table(bindings.role)"

# Service-level (Cloud Run, Pub/Sub topic, GCS bucket) — usually one
# of these depending on resource type:
gcloud run services get-iam-policy <service> --region=<region> --project=<project>
gcloud storage buckets get-iam-policy gs://<bucket>
```

### Terraform routine

```bash
# Always work from inside the env directory (no -chdir):
cd environments/<env>

terraform init       # first time or after provider changes
terraform plan       # preview — read it, don't blind-apply
terraform apply      # apply
terraform output     # show outputs
terraform state list # what's managed

# Drift check (no code-level diffs, just compare state to reality):
terraform plan -refresh-only
```

## Common debugging recipes

### Why is my Cloud Run service returning 5xx?

1. Check revisions + traffic (`gcloud run services describe`).
2. Read recent errors (`gcloud logging read ... severity>=ERROR`).
3. Look specifically for startup probe failures — most "503 service unavailable" is the container failing health checks.
4. Check the runtime SA's bindings — common cause: missing
   `secretmanager.secretAccessor` on a secret it tries to read at boot.
5. Check egress — if it calls another GCP API, does the runtime SA have
   the role on the target project?

### Did infrastructure drift?

```bash
terraform plan -refresh-only
```

Diff under `-refresh-only` = real drift (someone changed it outside TF).
Diff under regular `plan` = code diverged from state. Address each
differently.

### What's costing money?

```bash
# Top-line spend by service (needs billing export to BQ for detail):
gcloud billing accounts list
gcloud billing projects describe <project>

# For active workloads, check Cloud Run cold-start vs warm spend by
# examining `min_scale`. min_scale > 0 means continuous compute cost.
```

### Permission denied — IAM diagnostic flow

```bash
# 1. Confirm which identity gcloud is using:
gcloud config list account

# 2. Inspect that principal's bindings on the project:
gcloud projects get-iam-policy <project> \
  --flatten="bindings[].members" \
  --filter="bindings.members:$(gcloud config get-value account)"

# 3. Test the exact permission (not just the role):
gcloud iam roles describe <role-id> --format='value(includedPermissions)' \
  | grep <permission>

# 4. If it's a service-level resource, check resource-level IAM too —
#    project bindings may not be inherited (e.g. GCS bucket-level IAM).
```

## SA design heuristics

Detailed material in `references/cloud-run-sa-scoping.md` and
`references/gcp-iam-security.md`. Quick rules:

1. **One runtime SA per service or service group**, never reuse across
   blast radii.
2. **Grant roles at the lowest scope.** Project < folder < org. Most
   bindings should be project-scoped.
3. **Never grant `roles/owner` or `roles/editor`** to runtime/deployer SAs.
4. **`roles/iam.serviceAccountTokenCreator` is an escape from any
   boundary** — only grant when impersonation is the intended pattern,
   never to a general-purpose SA.
5. **`roles/secretmanager.secretAccessor` is granted on individual
   secrets**, not project-wide. Project-wide makes every secret readable
   by the principal.
6. **Deployer SA roles (for CI/CD via WIF):** `roles/run.admin`,
   `roles/artifactregistry.writer`, `roles/iam.serviceAccountUser` (on
   the runtime SA only, not project-wide).

## When to escalate

Stop and confirm with the user before:

- **Modifying IAM bindings beyond the established pattern** — especially
  on production projects, DNS projects, or any project where a mistake
  affects external users.
- **Creating new SA keys** — long-lived credentials; should be rare and
  justified.
- **Changing WIF pool/provider configuration** — affects every CI/CD
  pipeline that authenticates through it.
- **Editing `lifecycle { ignore_changes }` blocks** — can cause CI/CD
  reverts or unintended drift.
- **Touching Terraform state buckets directly** — bucket deletion = state
  loss; bucket misconfiguration can break every plan/apply.
- **Anything in a project that owns production DNS** — record changes
  affect production traffic directly.

## What this skill is NOT

- Not a replacement for reading `cloud.google.com/docs` when the task is
  novel or the API surface is unfamiliar.
- Not a substitute for `terraform plan` review — always read the plan
  output, never blind-apply.
- Not a credential broker — the user sets up SAs and keys; the skill knows
  the conventions and the operational moves.
- Not a security auditor — for that, load `gcp-iam-security.md` and run
  through it deliberately, or invoke the `security-review` skill.

---

**Reference layout:**
- `references/gcp-*.md` — six general GCP topic files (cc-polymath).
- `references/cloud-run-sa-scoping.md`, `references/project-isolation.md`,
  `references/terraform-multi-env.md`, `references/mariadb-cloudsql-migration.md`,
  `references/claudecode-ro-debug-setup.md` — focused operational guides.
- **Project conventions are NOT in the skill** — they live as
  `gcp-conventions.md` at the root of each project that has them. Always
  walk up from `$PWD` to find one before starting GCP work.
