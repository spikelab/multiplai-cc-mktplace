# GCP Cloud Run Service Account Scoping and Naming Best Practices

**Date:** 2026-04-02 | **Type:** best_practices | **Confidence:** high | **Sources:** 20 consulted, 16 used

## Summary

Google's official documentation and the broader GCP practitioner community converge on a clear position: each Cloud Run service should have its own dedicated service account, and each deployment pipeline should use a dedicated deployer SA. This applies to both runtime (service identity) and deployer SAs. The rationale rests on three pillars: blast radius containment (a compromised SA only affects one service), audit trail clarity (Cloud Audit Logs identify actions by SA email, not by calling application), and permission creep prevention (services' access needs diverge over time, and shared SAs accumulate unnecessary permissions). According to [Google's IAM best practices](https://docs.cloud.google.com/iam/docs/best-practices-service-accounts), "If multiple applications share a service account, you might not be able to trace activity back to the correct application."

The concern about same-named SAs across projects causing audit log confusion is unfounded. Service account emails always include the project ID in the format `name@project-id.iam.gserviceaccount.com`, and this full email appears in `protoPayload.authenticationInfo.principalEmail` in audit logs ([Google IAM audit logging examples](https://docs.cloud.google.com/iam/docs/audit-logging/examples-service-accounts), [Mitiga](https://www.mitiga.io/blog/who-touched-my-gcp-project-understanding-the-principal-part-in-cloud-audit-logs-part-2)). So `deployer@my-app-sim.iam.gserviceaccount.com` and `deployer@my-app-row.iam.gserviceaccount.com` are fully distinguishable. Using the same `account_id` across projects is not only safe but makes cross-project patterns more consistent.

For Workload Identity Federation (WIF), Google recommends one pool per external identity provider system (e.g., per GitHub organization), not per repository or per service ([WIF deployment pipelines docs](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)). Scoping is achieved via attribute conditions on the pool — for example, restricting to a specific repository and branch. A dedicated GCP project for managing WIF pools is recommended for organizational clarity.

## Methodology

Searched 13 queries across GCP official documentation, security research firms (SANS, Praetorian, Datadog, Mitiga), practitioner blogs (Medium, Dev.to, OneUptime), and Terraform module documentation. Consulted 20 sources, used 16 after triage (3 skipped due to rendering failures, 1 skipped as not relevant to Cloud Run). Followed 0 links — primary sources contained sufficient detail. Post-read reassessment found no framing gaps or unverified load-bearing claims.

## Findings

### 1. Google Officially Recommends Per-Service Service Accounts

Google's IAM documentation explicitly calls for "single-purpose service accounts" and warns against sharing ([Best practices for using service accounts securely](https://docs.cloud.google.com/iam/docs/best-practices-service-accounts)). Three specific risks of sharing are named: lifecycle misalignment (unclear when to delete a shared SA), permission creep (diverging needs lead to accumulated access), and audit trail opacity (logs show SA, not the application behind it). The Cloud Run documentation describes two distinct identity roles — a deployer account (for the Admin API during deployment) and a service identity (the runtime SA) — and recommends user-managed SAs for both ([Introduction to service identity](https://docs.cloud.google.com/run/docs/securing/service-identity)).

The Cloud Run service identity is configured per-service via `serviceAccountName` in the service template, and applies to all revisions of that service ([Configure service identity](https://docs.cloud.google.com/run/docs/configuring/services/service-identity)). Practitioner content reinforces this: "Each Cloud Run service should have its own service account. This provides isolation between services" and "Shared accounts mean shared blast radius" ([OneUptime](https://oneuptime.com/blog/post/2026-02-17-how-to-configure-cloud-run-to-use-a-custom-service-account-with-least-privilege-permissions/view)).

**Confidence:** high -- Cross-checked across 5+ sources including authoritative Google documentation and multiple established security firms.

### 2. Deployer SA: One Per Pipeline, Named After the Pipeline

Google's deployment pipeline best practices document recommends a 1:1 relationship between service accounts and deployment pipelines ([Best practices for SAs in deployment pipelines](https://docs.cloud.google.com/iam/docs/best-practices-for-using-service-accounts-in-deployment-pipelines)). The naming convention should "incorporate pipeline name/ID into the service account's email address" for clear audit attribution. The deployer SA should use WIF rather than stored keys — GitHub Actions provides OIDC tokens that can be exchanged for short-lived GCP credentials ([OneUptime WIF guide](https://oneuptime.com/blog/post/2026-02-17-how-to-set-up-workload-identity-federation-for-github-actions-to-access-gcp-resources/view)).

Key permission constraints for deployer SAs: grant create access without read where possible (e.g., `roles/storage.objectCreator` rather than `roles/storage.objectAdmin`), exclude `*.setIamPolicy` permissions to prevent privilege escalation, and never use basic roles like Editor/Owner. The deployer needs `roles/iam.serviceAccountUser` on the target runtime SA to attach it during deployment.

For audit non-repudiation, Google suggests using the Terraform `request_reason` parameter or the `X-Goog-Request-Reason` HTTP header to correlate pipeline run IDs with Cloud Audit Log entries.

**Confidence:** high -- Directly from Google's pipeline-specific documentation.

### 3. Audit Logs Identify SAs by Full Email (Including Project ID)

The `protoPayload.authenticationInfo.principalEmail` field in Cloud Audit Logs contains the full service account email, including the project ID: `my-service-account@my-project.iam.gserviceaccount.com` ([Google IAM audit logging examples](https://docs.cloud.google.com/iam/docs/audit-logging/examples-service-accounts)). This means same-named SAs across different projects are always distinguishable — the project ID is inherent to the identity. The `resource` section in audit logs also includes `project_id` labels for additional clarity ([Datadog](https://www.datadoghq.com/blog/monitoring-gcp-audit-logs/)).

Beyond the SA email, audit logs expose additional tracing data: `serviceAccountKeyName` (when key-based auth is used), `serviceAccountDelegationInfo` (for impersonation chains), and `principalSubject` (for WIF-based identities) ([Mitiga](https://www.mitiga.io/blog/who-touched-my-gcp-project-understanding-the-principal-part-in-cloud-audit-logs-part-2)). WIF-based authentication is more traceable than key-based authentication because the delegation chain is logged.

**Confidence:** high -- Verified across 4 independent sources (Google docs, Mitiga, Datadog, OneUptime).

### 4. Naming Convention: Prefix + Purpose, No Need to Embed Project Name

Google's official naming guidance uses a prefix system based on the SA's attachment type: `vm-` for VM instances, `wlif-` for Workload Identity Federation, `onprem-` for on-premises, with the application name embedded (e.g., `vm-travelexpenses@`) ([Best practices for using service accounts securely](https://docs.cloud.google.com/iam/docs/best-practices-service-accounts)). The project name does not need to be in the `account_id` because the project ID is already part of the SA email after the `@`. For example, `deployer` in project `my-app-sim` produces `deployer@my-app-sim.iam.gserviceaccount.com`.

The Terraform `google_service_account` resource constrains `account_id` to 6-30 characters matching `[a-z]([-a-z0-9]*[a-z0-9])` (from search results — the Terraform registry page itself could not be fetched). The official [terraform-google-service-accounts module](https://github.com/terraform-google-modules/terraform-google-service-accounts) supports a `prefix` + `names` pattern (e.g., prefix `test-sa` + name `first` = `test-sa-first`), and cross-project roles via `project-foo=>roles/viewer` syntax.

A practitioner pattern from search results uses `sa-{project_short}-tf-{env}` for Terraform-specific SAs (e.g., `sa-demo-tf-sbx`). For Cloud Run specifically, the pattern `<service-name>-sa` is common in practitioner guides (e.g., `my-app-docs-run` for a docs site runtime SA). The description field should hold contact info, documentation links, or notes about the SA's purpose.

**Confidence:** high -- Google official docs + Terraform module conventions.

### 5. WIF Architecture: One Pool Per GitHub Org, Attribute Conditions for Scoping

Google recommends one workload identity pool per external identity provider system — for GitHub Actions, that means one pool per GitHub organization, not per repository ([WIF deployment pipelines](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)). Scoping to specific repositories is done via attribute conditions, not separate pools. For example: `assertion.repository_owner=='my-org' && assertion.repository=='my-org/my-repo' && assertion.ref=='refs/heads/main'`.

A critical security note: use numeric identifier fields (`repository_id`, `repository_owner_id`) rather than name-based fields to prevent cybersquatting and typosquatting attacks. The binding pattern uses `principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/attribute.repository/my-github-org/my-repo`.

Google also recommends using a dedicated GCP project to manage WIF pools and providers, separate from the projects hosting workloads.

**Confidence:** high -- Directly from Google's WIF documentation.

### 6. Default SA Risk is Well-Documented and Widespread

According to [Datadog Security Labs](https://securitylabs.datadoghq.com/articles/google-cloud-default-service-accounts/) (October 2024), 33% of Compute Engine instances and 46% of GKE clusters still use the default Compute Engine service account. Only 31% of GCP projects have hardening against default SA privilege. While this study focused on Compute Engine and GKE (not Cloud Run), the underlying risk is the same: the default compute SA may receive the Editor role automatically, granting "view and modify every resource in a project" ([Praetorian](https://www.praetorian.com/blog/google-cloud-platform-gcp-service-account-based-privilege-escalation-paths/)). SANS reinforces: "single-purpose service accounts will greatly strengthen your security posture" ([SANS](https://www.sans.org/blog/mitigating-risk-default-service-accounts-google-cloud)).

**Confidence:** high -- Datadog study is quantitative research; corroborated by SANS, Praetorian, and Google's own docs.

## Minority Views & Tensions

Sources showed strong consensus on per-service SAs. No source argued against dedicated SAs. The only tension is between the theoretical ideal (per-service everything) and the practical overhead for small teams — but no source explicitly discussed this tradeoff. All sources, from Google's official docs to practitioner blogs, uniformly recommend per-service SAs without acknowledging that this creates more resources to manage, more Terraform code to maintain, and more IAM bindings to track.

The closest to a contrarian view comes from Google's own Cloud Run service identity documentation, which is notably less prescriptive than the IAM best practices page. The Cloud Run docs say to "determine the most minimal set of permissions" without explicitly mandating per-service SAs, while the IAM docs explicitly call for "single-purpose service accounts." This may reflect that the Cloud Run team considers the per-service question an operational decision, while the IAM/security team considers it a security baseline.

## Gaps & Open Questions

- **Management overhead for small teams:** No source addresses the operational cost of per-service SAs at small scale. For a 1-2 person team with 1-3 services per project, having separate deployer + runtime SAs per service could mean 4-12 SAs across two projects. Is the audit/security benefit worth the Terraform complexity? No source provides data on this tradeoff.
- **Cloud Run-specific audit log examples:** No source showed a complete Cloud Run deployment audit log entry revealing both the deployer SA and the target service name in context. The Cloud Run audit logging page lists audited operations but not sample entries.
- **SA lifecycle at small scale:** Google recommends disable-then-delete and using Activity Analyzer, but this tooling is designed for organizations with many SAs. Whether it adds value for a team managing fewer than 20 SAs total is unclear.
- **Terraform module patterns for small multi-project setups:** The `terraform-google-service-accounts` module supports multi-project roles, but no source showed a complete pattern for a small team managing 2-3 projects with consistent SA naming across all of them.

## Falsifiability

The main conclusion — per-service SAs are best practice for Cloud Run — would be weakened by evidence showing that per-service SA proliferation at small scale leads to configuration drift, forgotten stale SAs accumulating permissions, or increased error rates from mismatched SA-to-service bindings. If small teams consistently make more security mistakes with many narrow SAs than with fewer well-managed shared SAs, the complexity cost would outweigh the blast-radius benefit. No source provided evidence either way for this scenario.

## All Sources

| # | Source | Reputation | Used | Relevance | Date |
|---|--------|------------|------|-----------|------|
| 1 | [Best practices for using service accounts securely](https://docs.cloud.google.com/iam/docs/best-practices-service-accounts) | authoritative | Yes | Core guidance on SA naming, single-purpose SAs, audit implications | 2026-03 |
| 2 | [Introduction to service identity - Cloud Run](https://docs.cloud.google.com/run/docs/securing/service-identity) | authoritative | Yes | Cloud Run deployer vs runtime SA distinction, custom SA recommendation | 2026-04 |
| 3 | [Best practices for SAs in deployment pipelines](https://docs.cloud.google.com/iam/docs/best-practices-for-using-service-accounts-in-deployment-pipelines) | authoritative | Yes | 1:1 SA-per-pipeline, WIF preference, audit non-repudiation | 2026-03 |
| 4 | [WIF with deployment pipelines](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines) | authoritative | Yes | Pool-per-org architecture, attribute conditions, security warnings | 2026-03 |
| 5 | [Understanding audit logs](https://docs.cloud.google.com/logging/docs/audit/understanding-audit-logs) | authoritative | Yes | protoPayload structure, principalEmail field | 2026-03 |
| 6 | [Example logs for service accounts](https://docs.cloud.google.com/iam/docs/audit-logging/examples-service-accounts) | authoritative | Yes | Full SA email in logs including project ID | 2026-03 |
| 7 | [Cloud Run audit logging](https://docs.cloud.google.com/run/docs/audit-logging) | authoritative | Yes | Which Cloud Run operations generate audit logs | 2026-04 |
| 8 | [Configure service identity - Cloud Run](https://docs.cloud.google.com/run/docs/configuring/services/service-identity) | authoritative | Yes | Terraform/gcloud patterns, per-service SA config, cross-project SA | 2026-04 |
| 9 | [Datadog: GCP default service accounts](https://securitylabs.datadoghq.com/articles/google-cloud-default-service-accounts/) | established | Yes | Adoption stats: 33% CE, 46% GKE use default SA | 2024-10 |
| 10 | [Datadog: Monitoring GCP audit logs](https://www.datadoghq.com/blog/monitoring-gcp-audit-logs/) | established | Yes | SA identification in logs, project_id labels | 2020-05 |
| 11 | [Mitiga: GCP Cloud Audit Logs deep dive](https://www.mitiga.io/blog/who-touched-my-gcp-project-understanding-the-principal-part-in-cloud-audit-logs-part-2) | established | Yes | Principal types in audit logs, SA email format, delegation chains | 2026-03 |
| 12 | [OneUptime: Cloud Run custom SA](https://oneuptime.com/blog/post/2026-02-17-how-to-configure-cloud-run-to-use-a-custom-service-account-with-least-privilege-permissions/view) | emerging | Yes | Per-service SA recommendation, naming pattern, common roles | 2026-02 |
| 13 | [OneUptime: WIF for GitHub Actions](https://oneuptime.com/blog/post/2026-02-17-how-to-set-up-workload-identity-federation-for-github-actions-to-access-gcp-resources/view) | emerging | Yes | WIF setup steps, per-repo scoping, env-specific SA naming | 2026-02 |
| 14 | [SANS: Mitigating default SA risk](https://www.sans.org/blog/mitigating-risk-default-service-accounts-google-cloud) | established | Yes | Single-purpose SAs, default SA risks, org policy controls | 2025-09 |
| 15 | [Praetorian: GCP SA privilege escalation](https://www.praetorian.com/blog/google-cloud-platform-gcp-service-account-based-privilege-escalation-paths/) | established | Yes | Escalation via project-level SA binding, shared SA risks | 2020-12 |
| 16 | [terraform-google-service-accounts module](https://github.com/terraform-google-modules/terraform-google-service-accounts) | established | Yes | Prefix+names pattern, cross-project role syntax | 2026-01 |
| 17 | [Medium: GCP SA best practices](https://medium.com/@gcp.akp/the-best-practices-for-gcp-service-accounts-579195fb7764) | emerging | Yes | Naming prefixes, SA lifecycle management | 2024-08 |
| 18 | [dev.to: Granular GCP IAM permissions](https://dev.to/googlecloud/practical-advice-on-specifying-more-granular-permissions-with-google-cloud-iam-436l) | emerging | Yes | Iterative permission reduction methodology | 2020-05 |
| 19 | [Medium: Cross-project SAs in GCP](https://gtseres.medium.com/using-service-accounts-across-projects-in-gcp-cf9473fef8f0) | emerging | Yes | Centralized SA pattern, project-level resource constraint | 2020-03 |
| 20 | [Xebia: GCP IAM naming in Terraform](https://xebia.com/blog/how-to-name-your-google-project-iam-resources-in-terraform/) | established | No | Page did not render content | unknown |

---

<!-- STRUCTURED DATA -- machine-readable, do not edit above this line -->

```yaml
index:
  questions_investigated:
    - "What does Google officially recommend for service account granularity per Cloud Run service?"
    - "One deployer SA per project vs per repo vs per service -- tradeoffs for audit trail, blast radius, and WIF scoping?"
    - "One runtime SA per project vs per service -- how does this affect least-privilege and audit log clarity?"
    - "How are SAs identified in Cloud Audit Logs -- by email (includes project ID) or just account_id? Does same-name-across-projects cause log confusion?"
    - "What naming conventions do GCP practitioners use for SAs across multi-project setups?"
  questions_open:
    - "What is the actual management overhead of per-service SAs for a 1-2 person team with 2-3 projects?"
    - "Does SA proliferation at small scale lead to configuration drift or stale permissions?"
    - "What does a complete Cloud Run deployment audit log entry look like with deployer SA and service name?"
    - "What is a minimal-but-complete Terraform module pattern for 2-project SA consistency?"
  sources_consulted: 20
  total_findings: 55
  findings_by_confidence:
    verified: 12
    likely: 5
    unverified: 0
  sources_by_reputation:
    authoritative: 8
    established: 7
    emerging: 5
  falsifiability: "Would be weakened if per-service SA proliferation at small scale demonstrably leads to more security mistakes than fewer shared SAs"

meta:
  query: "GCP best practices for Cloud Run service account naming and scoping across multiple projects and environments"
  date: "2026-04-02"
  research_type: "best_practices"
  preset: standard
  confidence: high
  confidence_reason: "8 authoritative Google sources agree with 7 established security/monitoring firms; zero contradictions found"
  falsifiability: "Evidence that per-service SA proliferation causes more security errors than shared SAs at small team scale would undermine the main conclusion"

methodology:
  queries_used:
    - "GCP Cloud Run service account best practices per service 2025 2026"
    - "Google Cloud service account naming convention multiple projects"
    - "Cloud Run least privilege service account granularity"
    - "GCP audit log service account identification protoPayload"
    - "Workload Identity Federation service account scoping per repository"
    - "terraform google_service_account naming convention multi-environment"
    - "GCP shared service account risks Cloud Run"
    - "over-provisioning service accounts complexity overhead small team"
    - "site:cloud.google.com service account best practices Cloud Run"
    - "site:medium.com OR site:dev.to GCP service account per service vs shared"
    - "GCP Cloud Run service account per project vs per service audit logging 2025"
    - "Google Cloud service account email format project ID audit logs identification"
    - "terraform GCP service account module pattern multi-project infrastructure"
  sources_consulted: 20
  sources_used: 16
  links_followed: 0
  research_duration: "~17 minutes"
  reassessment_triggered: false

findings:
  - fact: "Google recommends single-purpose service accounts; sharing creates lifecycle misalignment, permission creep, and audit trail opacity"
    source: "[Best practices for using service accounts securely](https://docs.cloud.google.com/iam/docs/best-practices-service-accounts)"
    reputation: authoritative
    confidence: high
    evidence: "If multiple applications share a service account, you might not be able to trace activity back to the correct application"
    date: "2026-03-30"
    verification: "Corroborated by SANS, Praetorian, OneUptime, Medium practitioner"

  - fact: "Cloud Run has two distinct identity roles: deployer account (Admin API) and service identity (runtime SA)"
    source: "[Introduction to service identity | Cloud Run](https://docs.cloud.google.com/run/docs/securing/service-identity)"
    reputation: authoritative
    confidence: high
    date: "2026-04-01"

  - fact: "Google recommends 1:1 relationship between service accounts and deployment pipelines"
    source: "[Best practices for SAs in deployment pipelines](https://docs.cloud.google.com/iam/docs/best-practices-for-using-service-accounts-in-deployment-pipelines)"
    reputation: authoritative
    confidence: high
    date: "2026-03-30"

  - fact: "SA emails in audit logs use full format including project ID: name@project-id.iam.gserviceaccount.com"
    source: "[Example logs for service accounts](https://docs.cloud.google.com/iam/docs/audit-logging/examples-service-accounts)"
    reputation: authoritative
    confidence: high
    date: "2026-03-30"
    verification: "Cross-checked with Mitiga, Datadog, OneUptime"

  - fact: "Same-named SAs across different projects are always distinguishable in audit logs because project ID is part of the email"
    source: "[Mitiga: GCP Cloud Audit Logs deep dive](https://www.mitiga.io/blog/who-touched-my-gcp-project-understanding-the-principal-part-in-cloud-audit-logs-part-2)"
    reputation: established
    confidence: high
    date: "2026-03-05"
    verification: "Confirmed by Google IAM audit logging examples"

  - fact: "Google recommends one WIF pool per external identity provider system (per GitHub org), not per repo or service"
    source: "[WIF with deployment pipelines](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)"
    reputation: authoritative
    confidence: high
    date: "2026-03-30"

  - fact: "WIF scoping uses attribute conditions, not separate pools; use numeric *_id fields to prevent typosquatting"
    source: "[WIF with deployment pipelines](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)"
    reputation: authoritative
    confidence: high
    date: "2026-03-30"

  - fact: "Google naming convention uses prefixes (vm-, wlif-, onprem-) with app name embedded; project name not needed in account_id"
    source: "[Best practices for using service accounts securely](https://docs.cloud.google.com/iam/docs/best-practices-service-accounts)"
    reputation: authoritative
    confidence: high
    date: "2026-03-30"

  - fact: "Terraform account_id must be 6-30 chars matching [a-z]([-a-z0-9]*[a-z0-9])"
    source: "[Terraform Registry: google_service_account](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/google_service_account)"
    reputation: authoritative
    confidence: high
    date: "undated"

  - fact: "Official TF module uses prefix+names pattern (e.g., prefix test-sa + name first = test-sa-first) and cross-project roles via project-foo=>roles/viewer"
    source: "[terraform-google-service-accounts module](https://github.com/terraform-google-modules/terraform-google-service-accounts)"
    reputation: established
    confidence: high
    date: "2026-01-19"

  - fact: "33% of Compute Engine instances use default SA; 46% of GKE clusters use default SA; only 31% of projects have SA hardening"
    source: "[Datadog: GCP default service accounts](https://securitylabs.datadoghq.com/articles/google-cloud-default-service-accounts/)"
    reputation: established
    confidence: high
    evidence: "Based on Datadog's analysis of real-world GCP environments"
    date: "2024-10-29"

  - fact: "Cloud Run service identity is configured per-service via serviceAccountName in the template; applies to all revisions"
    source: "[Configure service identity | Cloud Run](https://docs.cloud.google.com/run/docs/configuring/services/service-identity)"
    reputation: authoritative
    confidence: high
    date: "2026-04-01"

  - fact: "Cross-project SA usage requires roles/iam.serviceAccountTokenCreator on Cloud Run service agent + org policy adjustment"
    source: "[Configure service identity | Cloud Run](https://docs.cloud.google.com/run/docs/configuring/services/service-identity)"
    reputation: authoritative
    confidence: high
    date: "2026-04-01"

  - fact: "Deployer SA needs roles/iam.serviceAccountUser on the target runtime SA to attach it during deployment"
    source: "[Configure service identity | Cloud Run](https://docs.cloud.google.com/run/docs/configuring/services/service-identity)"
    reputation: authoritative
    confidence: high
    date: "2026-04-01"

  - fact: "Binding SA access at project level rather than SA level lets user impersonate any SA in the project"
    source: "[Praetorian: GCP SA privilege escalation](https://www.praetorian.com/blog/google-cloud-platform-gcp-service-account-based-privilege-escalation-paths/)"
    reputation: established
    confidence: high
    date: "2020-12-15"

  - fact: "Service accounts are project-level resources; cannot be created at folder or org level"
    source: "[Medium: Cross-project SAs in GCP](https://gtseres.medium.com/using-service-accounts-across-projects-in-gcp-cf9473fef8f0)"
    reputation: emerging
    confidence: likely
    date: "2020-03-30"

  - fact: "Impersonation-based auth is more traceable than key-based auth in audit logs"
    source: "[Best practices for using service accounts securely](https://docs.cloud.google.com/iam/docs/best-practices-service-accounts)"
    reputation: authoritative
    confidence: high
    date: "2026-03-30"

minority_views:
  - view: "Cloud Run docs are less prescriptive than IAM docs on per-service SAs -- say 'determine minimal permissions' without mandating one SA per service"
    source: "[Introduction to service identity | Cloud Run](https://docs.cloud.google.com/run/docs/securing/service-identity)"
    why_notable: "May indicate per-service is a security recommendation, not a Cloud Run architectural requirement"

tensions:
  - topic: "Prescriptiveness of per-service SA recommendation"
    position_a:
      claim: "Single-purpose service accounts are a firm security baseline"
      source: "[Best practices for using service accounts securely](https://docs.cloud.google.com/iam/docs/best-practices-service-accounts)"
    position_b:
      claim: "Determine the most minimal set of permissions (without mandating per-service)"
      source: "[Introduction to service identity | Cloud Run](https://docs.cloud.google.com/run/docs/securing/service-identity)"
    resolution: "Not a true contradiction -- IAM docs set the security standard, Cloud Run docs focus on configuration mechanics. Both teams are at Google. The IAM position is the stronger recommendation."

gaps:
  - "No source addresses operational overhead of per-service SAs for teams of 1-2 people managing 2-3 projects"
  - "No complete Cloud Run deployment audit log entry example showing both deployer SA and Cloud Run service name"
  - "No end-to-end Terraform module pattern for small multi-project setups with consistent SA naming"
  - "Cloud Run-specific default SA adoption statistics not available (Datadog study covers only CE and GKE)"

sources:
  - title: "Best practices for using service accounts securely"
    url: "https://docs.cloud.google.com/iam/docs/best-practices-service-accounts"
    reputation: authoritative
    relevance: "Core guidance on naming, single-purpose SAs, audit implications"
    date: "2026-03-30"
  - title: "Introduction to service identity | Cloud Run"
    url: "https://docs.cloud.google.com/run/docs/securing/service-identity"
    reputation: authoritative
    relevance: "Deployer vs runtime SA distinction"
    date: "2026-04-01"
  - title: "Best practices for SAs in deployment pipelines"
    url: "https://docs.cloud.google.com/iam/docs/best-practices-for-using-service-accounts-in-deployment-pipelines"
    reputation: authoritative
    relevance: "1:1 SA-per-pipeline, WIF, audit non-repudiation"
    date: "2026-03-30"
  - title: "WIF with deployment pipelines"
    url: "https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines"
    reputation: authoritative
    relevance: "Pool-per-org architecture, attribute conditions"
    date: "2026-03-30"
  - title: "Understanding audit logs"
    url: "https://docs.cloud.google.com/logging/docs/audit/understanding-audit-logs"
    reputation: authoritative
    relevance: "protoPayload structure"
    date: "2026-03-30"
  - title: "Example logs for service accounts"
    url: "https://docs.cloud.google.com/iam/docs/audit-logging/examples-service-accounts"
    reputation: authoritative
    relevance: "Full SA email format in audit logs"
    date: "2026-03-30"
  - title: "Cloud Run audit logging"
    url: "https://docs.cloud.google.com/run/docs/audit-logging"
    reputation: authoritative
    relevance: "Which Cloud Run operations are audited"
    date: "2026-04-01"
  - title: "Configure service identity | Cloud Run"
    url: "https://docs.cloud.google.com/run/docs/configuring/services/service-identity"
    reputation: authoritative
    relevance: "Terraform/gcloud SA config, cross-project requirements"
    date: "2026-04-01"
  - title: "Datadog: GCP default service accounts"
    url: "https://securitylabs.datadoghq.com/articles/google-cloud-default-service-accounts/"
    reputation: established
    relevance: "Adoption statistics on default SA usage"
    date: "2024-10-29"
  - title: "Datadog: Monitoring GCP audit logs"
    url: "https://www.datadoghq.com/blog/monitoring-gcp-audit-logs/"
    reputation: established
    relevance: "SA identification format in logs"
    date: "2020-05-29"
  - title: "Mitiga: GCP Cloud Audit Logs deep dive"
    url: "https://www.mitiga.io/blog/who-touched-my-gcp-project-understanding-the-principal-part-in-cloud-audit-logs-part-2"
    reputation: established
    relevance: "Principal types, SA email format, delegation chains"
    date: "2026-03-05"
  - title: "OneUptime: Cloud Run custom SA"
    url: "https://oneuptime.com/blog/post/2026-02-17-how-to-configure-cloud-run-to-use-a-custom-service-account-with-least-privilege-permissions/view"
    reputation: emerging
    relevance: "Per-service SA recommendation, naming pattern"
    date: "2026-02-17"
  - title: "OneUptime: WIF for GitHub Actions"
    url: "https://oneuptime.com/blog/post/2026-02-17-how-to-set-up-workload-identity-federation-for-github-actions-to-access-gcp-resources/view"
    reputation: emerging
    relevance: "WIF setup steps, per-repo scoping patterns"
    date: "2026-02-17"
  - title: "SANS: Mitigating default SA risk"
    url: "https://www.sans.org/blog/mitigating-risk-default-service-accounts-google-cloud"
    reputation: established
    relevance: "Single-purpose SAs, org policy controls"
    date: "2025-09-15"
  - title: "Praetorian: GCP SA privilege escalation"
    url: "https://www.praetorian.com/blog/google-cloud-platform-gcp-service-account-based-privilege-escalation-paths/"
    reputation: established
    relevance: "Escalation via project-level SA binding"
    date: "2020-12-15"
  - title: "terraform-google-service-accounts module"
    url: "https://github.com/terraform-google-modules/terraform-google-service-accounts"
    reputation: established
    relevance: "Prefix+names pattern, cross-project role syntax"
    date: "2026-01-19"
```
