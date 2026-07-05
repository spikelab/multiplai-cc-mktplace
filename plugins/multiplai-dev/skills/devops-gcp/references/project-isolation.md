# GCP Project Isolation Best Practices for Staging/Test Environments

**Date:** 2026-03-13
**Query:** GCP best practices for creating isolated test/staging environments when migrating existing infrastructure
**Context:** my-app (real estate SaaS) — GCP VMs, MySQL, Redis, Django Channels, Celery. Moving to Cloud Run + Cloud SQL + Redis. Zero tolerance for production impact (payments, police reporting, smart locks).

---

## 1. Organization and Project Hierarchy

### The Right Structure for my-app

GCP's resource hierarchy is **Organization > Folders > Projects > Resources**. Each resource has exactly one parent, and IAM policies flow downward through inheritance ([GCP Resource Hierarchy docs](https://docs.cloud.google.com/resource-manager/docs/cloud-platform-resource-hierarchy), updated 2026-03-12).

**For my-app, the answer is straightforward: create a second project within the existing "My App" organization.** GCP projects are the primary isolation boundary — "completely isolated from each other unless permissions are connectivity are explicitly granted" ([Priocept](https://priocept.com/2016/12/02/cloud-project-isolation-aws-vs-google-cloud-platform/)). This is fundamentally different from AWS, where you'd need separate accounts. [VERIFIED — 3+ authoritative sources]

**Recommended hierarchy:**

```
My App (Organization)
├── Production (Folder — optional at this scale)
│   └── my-app-prod (Project) ← existing infrastructure
└── Non-Production (Folder — optional at this scale)
    └── my-app-staging (Project) ← new project for Cloud Run experiments
```

At my-app's scale (CTO + 2 junior devs), folders are optional. The project-level isolation is sufficient. GCP recommends folders when you have "different policy and administration requirements per environment" ([GCP Architecture Center](https://docs.cloud.google.com/architecture/landing-zones/decide-resource-hierarchy)). For now, two projects under one org is the right level of complexity. [VERIFIED]

### Key GCP Terminology Trap

GCP uses "staging organization" to mean testing **org-wide policy changes** (IAM, org policies, resource hierarchy) — NOT a staging deployment environment. "Don't use your staging organization for managing testing environments" ([GCP docs](https://docs.cloud.google.com/architecture/identity/best-practices-for-planning)). my-app does NOT need a separate organization. One org, two projects. [VERIFIED]

### IAM Inheritance Implications

- Allow policies are inherited downward and additive. Deny policies supersede allow policies.
- Any IAM role granted at the org level applies to ALL projects. Be careful with org-level grants.
- Moving a project between folders changes its inherited permissions automatically.

**Practical implication:** If someone has `roles/editor` at the org level, they have editor access to both production and staging. For my-app's small team this is probably fine initially — all three people need access to both. But as the team grows, move to folder-level or project-level grants. [LIKELY — inferred from hierarchy docs]

---

## 2. IAM Isolation Between Projects

### Service Account Strategy

The critical principle: **create separate service accounts per project, scoped to that project's resources.** "Use service accounts that reside in the same project as the resources you need to access — don't use cross-project SAs" ([GCP WIF Best Practices](https://cloud.google.com/iam/docs/best-practices-for-using-workload-identity-federation)). [VERIFIED]

**For my-app staging:**

| Service Account | Project | Roles | Purpose |
|----------------|---------|-------|---------|
| `cloudrun-staging@my-app-staging.iam` | my-app-staging | Cloud SQL Client, Secret Manager Secret Accessor | Cloud Run service in staging |
| `deploy-staging@my-app-staging.iam` | my-app-staging | Cloud Run Developer, Artifact Registry Writer | GitHub Actions deployment |
| `cloudrun-prod@my-app-prod.iam` | my-app-prod | Cloud SQL Client, Secret Manager Secret Accessor | Cloud Run service in prod (future) |
| `deploy-prod@my-app-prod.iam` | my-app-prod | Cloud Run Developer, Artifact Registry Writer | GitHub Actions deployment (future) |

### Critical Guardrails

1. **Disable default service account Editor grants.** Organizations created after May 2024 have the constraint "Disable Automatic IAM Grants for Default Service Accounts" enforced by default. Verify this is active on the My App org. If the org predates May 2024, enable it manually. ([GCP SA Best Practices](https://docs.cloud.google.com/iam/docs/best-practices-service-accounts)) [VERIFIED]

2. **Don't manage SA access at the project level.** "Don't manage access to service accounts at the Google Cloud project or folder level. Instead, individually manage each service account" — because project-level grants create blanket access. For example, granting Service Account Token Creator at the project level lets users impersonate ANY SA in that project. [VERIFIED]

3. **Prevent lateral movement.** A SA in staging with permission to impersonate a SA in production is the most dangerous pattern. Use GCP's Recommender lateral movement insights to detect these chains. For my-app: the staging project's SAs should have ZERO permissions in the production project. [VERIFIED]

4. **Disable SA key creation** for production accounts. Use Workload Identity Federation instead of SA keys for CI/CD. Apply org-level constraint "Disable service account key creation." [VERIFIED]

5. **Enable data access audit logs** for IAM API and Security Token Service API in both projects. This creates records of all SA impersonation events — important for compliance given my-app handles payments and police reporting. [VERIFIED]

---

## 3. Budget Controls and Cost Safety

### The Critical Fact: Budgets Don't Cap Spending

"Setting a budget does not automatically cap Google Cloud or Google Maps Platform usage or spending. Budgets trigger alerts to inform you of how your usage costs are trending over time." ([GCP Budget docs](https://docs.cloud.google.com/billing/docs/how-to/budgets), updated 2026-03-05) [VERIFIED]

This is the single most important thing to understand. A $50/month budget alert will send you emails, but it will NOT stop a runaway Cloud SQL instance from racking up charges.

### Recommended Budget Setup for my-app-staging

**Layer 1: Budget Alerts (monitoring)**
- Create a project-scoped budget for my-app-staging
- Set thresholds at 50%, 90%, 100% of your target spend (e.g., $100/month)
- Add forecasted cost threshold at 100% (alerts based on projected end-of-month spend)
- Route alerts to Cloud Monitoring notification channel targeting the user's email
- Project owners can create project-scoped budgets without billing account admin access [VERIFIED]

**Layer 2: Automated Billing Disable (enforcement)**

For a sandbox where you genuinely need hard cost caps, implement the automated billing disable pattern:

1. Connect budget to a Pub/Sub topic
2. Deploy a Cloud Function that listens to budget notifications
3. When threshold exceeded, the function removes the billing account from the project

**WARNING:** "When you remove Cloud Billing from your project, all resources are shut down. The resources may not shut down gracefully and be irretrievably deleted. There is no graceful recovery if you disable Cloud Billing." ([Cyclenerd/poweroff-google-cloud-cap-billing](https://github.com/Cyclenerd/poweroff-google-cloud-cap-billing)) [VERIFIED]

**Open-source implementations:**
- **[Cyclenerd/poweroff-google-cloud-cap-billing](https://github.com/Cyclenerd/poweroff-google-cloud-cap-billing)** — 76 stars, Terraform-based, ~10-15 min setup, creates custom IAM role with minimal permissions. Better maintained.
- **[Rumeister/gcp-budget-cap](https://github.com/Rumeister/gcp-budget-cap)** — Simpler but dormant (last commit June 2023, 0 stars).

**Key caveat:** Pub/Sub budget notifications average ~30 minutes delay. Set your automated threshold BELOW your actual spending limit to account for the lag. For example, if you want to cap at $100, trigger disable at $70. [VERIFIED]

**Layer 3: Quotas (prevention)**

For specific high-cost services, set API quotas:
- Cloud SQL: limit instance hours or maximum instance size
- Cloud Run: limit concurrent instances and memory allocation
- These prevent runaway scaling before budget alerts even fire

### Recommendation for my-app

For a staging/test environment, Layer 1 (budget alerts at $100/month) + Layer 3 (Cloud Run max instances = 2, Cloud SQL tier = db-f1-micro) is sufficient. Layer 2 (automated disable) is overkill for an actively managed staging environment — the nuclear option of billing disable destroys resources, which you'd then need to recreate. Quotas are a better prevention mechanism. [LIKELY — synthesized from multiple sources]

---

## 4. GitHub Actions CI/CD with Workload Identity Federation

### Architecture: Separate WIF Per Project

GCP recommends a dedicated project to manage WIF pools and providers ([GCP WIF docs](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)). But for my-app's scale, putting the WIF pool in each target project is simpler and still secure. [LIKELY]

**Recommended setup:**

```
my-app-staging project:
  └── WIF Pool: "github-pool"
      └── Provider: "github-actions"
          └── Attribute condition: assertion.repository_owner=='my-github-org'
      └── SA binding: deploy-staging@my-app-staging.iam

my-app-prod project (future):
  └── WIF Pool: "github-pool"
      └── Provider: "github-actions"
          └── Attribute condition: assertion.repository_owner=='my-github-org' && assertion.ref=='refs/heads/main'
      └── SA binding: deploy-prod@my-app-prod.iam
```

**Key security measures:**

1. **Use repository_id (numeric) instead of repository name** in attribute conditions to prevent cybersquatting attacks. [VERIFIED]
2. **Restrict production deploys to main branch** via attribute conditions. Staging can allow any branch. [VERIFIED]
3. **Separate GitHub Actions jobs per environment.** Each authentication call overwrites previous credentials, so don't try to deploy to staging and production in the same job. ([google-github-actions/auth](https://github.com/google-github-actions/auth)) [VERIFIED]
4. **Use a single provider per pool** to avoid subject collisions in audit logs. [VERIFIED]

**GitHub Actions workflow pattern:**

```yaml
jobs:
  deploy-staging:
    if: github.ref == 'refs/heads/develop'
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: projects/$STAGING_PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-actions
          service_account: deploy-staging@my-app-staging.iam.gserviceaccount.com

  deploy-production:
    if: github.ref == 'refs/heads/main'
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: projects/$PROD_PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-actions
          service_account: deploy-prod@my-app-prod.iam.gserviceaccount.com
```

---

## 5. VPC and Network Isolation

### Staging Should NOT Connect to Production

For my-app's use case (zero tolerance for production impact), the staging project should have **its own completely separate VPC with no peering or connectivity to production.** [VERIFIED]

"Create VPC networks in different projects for independent IAM controls" ([GCP VPC Best Practices](https://docs.cloud.google.com/architecture/best-practices-vpc-design), 2025-01-30). "If you require independent IAM controls per VPC network, create your VPC networks in different projects." [VERIFIED]

**Recommended approach:**

| Resource | my-app-staging | my-app-prod |
|----------|-----------------|---------------|
| VPC | `staging-vpc` (standalone) | `prod-vpc` (standalone) |
| Cloud SQL | Private IP in staging-vpc | Private IP in prod-vpc |
| Cloud Run | Connected to staging-vpc | Connected to prod-vpc |
| Redis (Memorystore) | In staging-vpc | In prod-vpc |

**No Shared VPC.** Shared VPC requires multiple IAM roles (Shared VPC Admin, Service Project Admin, Network Admin, Security Admin) — overhead inappropriate for a 3-person team. Use it later if you need centralized network governance across many projects. [VERIFIED]

### Data Seeding Without Production Access

Since staging has no network path to production:
1. **Export production data** (anonymized) to a Cloud Storage bucket
2. **Import into staging Cloud SQL** from the bucket
3. Automate with a script that:
   - Dumps production MySQL (excluding PII or with masking)
   - Uploads to a GCS bucket accessible by staging SA
   - Imports into staging Cloud SQL instance
4. **Never** give staging service accounts access to production Cloud SQL

For my-app specifically: given the police reporting and payment data sensitivity, consider synthetic test data rather than anonymized production data. A MySQL dump with faker-generated addresses, names, and fake PayPal transaction IDs eliminates the risk of PII leaking into staging. [LIKELY — synthesized recommendation based on data sensitivity context]

---

## 6. Cloud Run + Cloud SQL Setup for Staging

### Architecture Pattern

Based on the [Django on Cloud Run codelab](https://codelabs.developers.google.com/codelabs/cloud-run-django) and [Cloud Run to Cloud SQL docs](https://docs.cloud.google.com/sql/docs/mysql/connect-run):

```
my-app-staging project:
├── Cloud Run Service (Django app)
│   ├── --add-cloudsql-instances=my-app-staging:REGION:staging-db
│   ├── --set-secrets=APPLICATION_SETTINGS=app-settings:latest
│   └── Service account: cloudrun-staging@my-app-staging.iam
├── Cloud SQL (MySQL, db-f1-micro for staging)
│   ├── Private IP in staging-vpc
│   └── Connection: Unix socket via built-in connector
├── Secret Manager
│   ├── DATABASE_URL
│   ├── DJANGO_SECRET_KEY
│   ├── REDIS_URL
│   └── PAYPAL_SANDBOX_CREDENTIALS
├── Memorystore (Redis) — for Celery + Django Channels
├── Artifact Registry — container images
└── Cloud Build — CI/CD
```

**Key details:**
- Cloud Run's built-in `--add-cloudsql-instances` handles the Auth Proxy automatically for public IP connections. For private IP, you need Direct VPC egress or Serverless VPC Access connector. [VERIFIED]
- Service account needs `Cloud SQL Client` role. If SA and Cloud SQL are in the same project (they should be), no cross-project API enablement needed. [VERIFIED]
- 100 concurrent connections per Cloud Run instance to Cloud SQL — use connection pooling. [VERIFIED]
- Use Cloud Run Jobs for migrations: `gcloud run jobs execute django-migrate --region REGION`. [VERIFIED]
- Same container image can deploy to both staging and production — configuration differs only in Secret Manager entries and Cloud SQL instance connection name. [VERIFIED]

### Django Channels / WebSocket Consideration

Cloud Run supports WebSockets (since it handles HTTP/2 and gRPC), but with a caveat: Cloud Run instances can scale to zero and have a maximum request timeout (default 5 min, configurable up to 60 min). For Django Channels:
- The staging environment will validate whether Cloud Run's WebSocket support meets my-app's real-time requirements
- If long-lived WebSocket connections are needed, you may need Cloud Run with `--session-affinity` or consider GKE for production
- This is exactly why testing in staging before migrating production is valuable [UNVERIFIED — architectural inference]

---

## 7. Step-by-Step Setup Checklist

### Phase 1: Project Creation (30 minutes)

```bash
# Create the staging project under your existing org
gcloud projects create my-app-staging \
  --organization=$(gcloud organizations list --format="value(ID)")

# Link to existing billing account
gcloud billing projects link my-app-staging \
  --billing-account=$(gcloud billing accounts list --format="value(ACCOUNT_ID)" --limit=1)

# Enable required APIs
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  redis.googleapis.com \
  iam.googleapis.com \
  --project=my-app-staging
```

### Phase 2: Budget Alert (10 minutes)

```bash
# Create budget (via console is easier, but CLI works)
# Console: Billing > Budgets & alerts > Create Budget
# Scope: my-app-staging only
# Amount: $100/month
# Thresholds: 50%, 90%, 100% actual + 100% forecasted
# Notifications: your email via Cloud Monitoring channel
```

### Phase 3: Service Accounts & IAM (20 minutes)

```bash
# Create Cloud Run service account
gcloud iam service-accounts create cloudrun-staging \
  --display-name="Cloud Run Staging Service" \
  --project=my-app-staging

# Grant minimal roles
gcloud projects add-iam-policy-binding my-app-staging \
  --member="serviceAccount:cloudrun-staging@my-app-staging.iam.gserviceaccount.com" \
  --role="roles/cloudsql.client"

gcloud projects add-iam-policy-binding my-app-staging \
  --member="serviceAccount:cloudrun-staging@my-app-staging.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# Create deployment service account
gcloud iam service-accounts create deploy-staging \
  --display-name="GitHub Actions Deploy Staging" \
  --project=my-app-staging

gcloud projects add-iam-policy-binding my-app-staging \
  --member="serviceAccount:deploy-staging@my-app-staging.iam.gserviceaccount.com" \
  --role="roles/run.developer"

gcloud projects add-iam-policy-binding my-app-staging \
  --member="serviceAccount:deploy-staging@my-app-staging.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
```

### Phase 4: VPC & Database (30 minutes)

```bash
# Create VPC
gcloud compute networks create staging-vpc \
  --subnet-mode=auto \
  --project=my-app-staging

# Create Cloud SQL instance (small for staging)
gcloud sql instances create staging-db \
  --database-version=MYSQL_8_0 \
  --tier=db-f1-micro \
  --region=us-central1 \
  --network=staging-vpc \
  --project=my-app-staging

# Create database and user
gcloud sql databases create my-app \
  --instance=staging-db \
  --project=my-app-staging
```

### Phase 5: WIF for GitHub Actions (20 minutes)

```bash
# Create WIF pool
gcloud iam workload-identity-pools create github-pool \
  --location=global \
  --project=my-app-staging

# Create provider with attribute conditions
gcloud iam workload-identity-pools providers create-oidc github-actions \
  --location=global \
  --workload-identity-pool=github-pool \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner_id=assertion.repository_owner_id" \
  --attribute-condition="assertion.repository_owner_id=='YOUR_GITHUB_ORG_NUMERIC_ID'" \
  --project=my-app-staging

# Bind deploy SA to WIF pool
gcloud iam service-accounts add-iam-policy-binding \
  deploy-staging@my-app-staging.iam.gserviceaccount.com \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/YOUR_ORG/YOUR_REPO" \
  --project=my-app-staging
```

---

## Sources

| # | Source | Type | Date |
|---|--------|------|------|
| 1 | [GCP Resource Hierarchy](https://docs.cloud.google.com/resource-manager/docs/cloud-platform-resource-hierarchy) | Official docs | 2026-03-12 |
| 2 | [Decide Resource Hierarchy for Landing Zone](https://docs.cloud.google.com/architecture/landing-zones/decide-resource-hierarchy) | Architecture guide | Current |
| 3 | [Best Practices: Planning Accounts & Organizations](https://docs.cloud.google.com/architecture/identity/best-practices-for-planning) | Official docs | Current |
| 4 | [WIF with Deployment Pipelines](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines) | Official docs | Current |
| 5 | [WIF Best Practices](https://cloud.google.com/iam/docs/best-practices-for-using-workload-identity-federation) | Official docs | Current |
| 6 | [Service Account Best Practices](https://docs.cloud.google.com/iam/docs/best-practices-service-accounts) | Official docs | 2026-03-05 |
| 7 | [Budgets & Budget Alerts](https://docs.cloud.google.com/billing/docs/how-to/budgets) | Official docs | 2026-03-05 |
| 8 | [Programmatic Budget Notifications](https://docs.cloud.google.com/billing/docs/how-to/budgets-programmatic-notifications) | Official docs | 2026-03-05 |
| 9 | [VPC Design Best Practices](https://docs.cloud.google.com/architecture/best-practices-vpc-design) | Architecture guide | 2025-01-30 |
| 10 | [Shared VPC](https://docs.cloud.google.com/vpc/docs/shared-vpc) | Official docs | 2026-03-12 |
| 11 | [Cloud Run to Cloud SQL MySQL](https://docs.cloud.google.com/sql/docs/mysql/connect-run) | Official docs | 2026-03-12 |
| 12 | [Django on Cloud Run Codelab](https://codelabs.developers.google.com/codelabs/cloud-run-django) | Tutorial | Current |
| 13 | [google-github-actions/auth](https://github.com/google-github-actions/auth) | GitHub | Current |
| 14 | [Cyclenerd/poweroff-google-cloud-cap-billing](https://github.com/Cyclenerd/poweroff-google-cloud-cap-billing) | GitHub | Active |
| 15 | [Rumeister/gcp-budget-cap](https://github.com/Rumeister/gcp-budget-cap) | GitHub | 2023-06 |
| 16 | [AWS vs GCP Project Isolation (Priocept)](https://priocept.com/2016/12/02/cloud-project-isolation-aws-vs-google-cloud-platform/) | Blog | 2016-12 |

---

```yaml
# --- STRUCTURED DATA APPENDIX ---
research_metadata:
  query: "GCP best practices for creating isolated test/staging environments when migrating existing infrastructure"
  date: "2026-03-13"
  sources_read: 16
  sources_failed: 4
  total_fetches: 19
  fetch_budget: 40
  confidence: high

findings:
  - id: f1
    topic: hierarchy
    claim: "GCP projects are the primary isolation boundary — completely isolated by default unless permissions explicitly granted"
    confidence: VERIFIED
    sources: [1, 3, 16]

  - id: f2
    topic: hierarchy
    claim: "One organization with multiple projects is the right structure for my-app. Separate organizations are for testing org-wide policies, not deployment environments."
    confidence: VERIFIED
    sources: [2, 3]

  - id: f3
    topic: hierarchy
    claim: "IAM policies are inherited downward. Allow policies are additive; deny policies supersede allow policies."
    confidence: VERIFIED
    sources: [1, 2]

  - id: f4
    topic: iam
    claim: "Create separate service accounts per project. SAs should reside in the same project as resources they access."
    confidence: VERIFIED
    sources: [5, 6]

  - id: f5
    topic: iam
    claim: "Default SAs get Editor role — disable with org policy constraint. Orgs created after May 2024 have this enforced."
    confidence: VERIFIED
    sources: [6]

  - id: f6
    topic: iam
    claim: "Don't manage SA access at project level — manage individually per SA to avoid blanket access."
    confidence: VERIFIED
    sources: [6]

  - id: f7
    topic: iam
    claim: "Lateral movement (SA in one project impersonating SA in another) is the key cross-project risk to monitor."
    confidence: VERIFIED
    sources: [6]

  - id: f8
    topic: budget
    claim: "GCP budgets are monitoring-only — they do NOT cap spending."
    confidence: VERIFIED
    sources: [7]
    quote: "Setting a budget does not automatically cap Google Cloud or Google Maps Platform usage or spending."

  - id: f9
    topic: budget
    claim: "Automated billing disable via Pub/Sub + Cloud Function is the only way to hard-cap. But it destroys all resources non-gracefully."
    confidence: VERIFIED
    sources: [7, 8, 14, 15]
    quote: "There is no graceful recovery if you disable Cloud Billing."

  - id: f10
    topic: budget
    claim: "Budget notification delay averages ~30 minutes — set threshold below actual limit."
    confidence: VERIFIED
    sources: [14, 15]

  - id: f11
    topic: cicd
    claim: "Use separate GitHub Actions jobs per environment, each with its own WIF auth and service account."
    confidence: VERIFIED
    sources: [4, 5, 13]

  - id: f12
    topic: cicd
    claim: "Use repository_id (numeric) instead of repository name in WIF attribute conditions to prevent cybersquatting."
    confidence: VERIFIED
    sources: [4]

  - id: f13
    topic: cicd
    claim: "Restrict production WIF to main branch via attribute condition. Staging can allow any branch."
    confidence: VERIFIED
    sources: [4]

  - id: f14
    topic: network
    claim: "Staging should have its own VPC with no connectivity to production."
    confidence: VERIFIED
    sources: [9, 10]

  - id: f15
    topic: network
    claim: "Shared VPC is overkill for small teams — requires multiple admin IAM roles."
    confidence: VERIFIED
    sources: [10]

  - id: f16
    topic: cloudrun
    claim: "Cloud Run has built-in Cloud SQL connector via --add-cloudsql-instances. Handles Auth Proxy automatically."
    confidence: VERIFIED
    sources: [11]

  - id: f17
    topic: cloudrun
    claim: "Same container image can deploy to both staging and production. Config differs only in secrets and connection names."
    confidence: VERIFIED
    sources: [11, 12]

  - id: f18
    topic: cloudrun
    claim: "100 concurrent connections per Cloud Run instance to Cloud SQL. Use connection pooling."
    confidence: VERIFIED
    sources: [11]

  - id: f19
    topic: data
    claim: "For my-app's sensitive data (payments, police reports), use synthetic test data rather than anonymized production data."
    confidence: LIKELY
    sources: []
    note: "Synthesized recommendation based on data sensitivity context — no source explicitly covers this for my-app's case."

open_questions:
  - "Django Channels WebSocket behavior on Cloud Run: does --session-affinity + configurable timeout suffice, or does my-app need GKE?"
  - "Redis/Memorystore setup for Celery in staging: does Cloud Run cold start affect Celery worker reliability?"
  - "Exact GCP org creation date for My App — determines whether default SA Editor constraint is already enforced"
  - "Whether my-app's existing billing account can be shared across projects or if a separate billing account for staging is preferred"
```
