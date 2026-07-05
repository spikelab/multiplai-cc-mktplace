# Terraform Multi-Environment GCP Best Practices

**Date:** 2026-04-03
**Sources:** 15 (5 authoritative, 4 established, 6 emerging)
**Query:** Best practices for managing multiple GCP environments with shared and per-environment infrastructure

---

## The Core Problem

When environments share some infrastructure (project, IAM, WIF, Artifact Registry) but need different sets of services (Cloud Run instances, databases, etc.), the common starting point — a single set of .tf files with per-environment tfvars — breaks down. A `for_each` over `var.services` forces every environment to declare every service, making environments identical in shape even when they shouldn't be.

## The Consensus: Directory-Per-Environment + Shared Modules

Across Google Cloud docs, HashiCorp guides, Gruntwork, and practitioner reports, one pattern dominates for teams managing 2-5 environments: **reusable modules composed differently per environment directory.**

### Recommended Structure

```
terraform/
├── modules/
│   ├── project-base/          # Project, APIs, budget
│   ├── iam/                   # SAs, IAM bindings, WIF
│   ├── artifact-registry/     # AR repo
│   └── cloud-run-service/     # Single Cloud Run service (reusable)
│       ├── main.tf
│       ├── variables.tf
│       └── outputs.tf
├── environments/
│   ├── my-app-row/
│   │   ├── main.tf            # Composes modules — decides which services exist
│   │   ├── variables.tf
│   │   ├── terraform.tfvars
│   │   └── backend.tf         # Own state
│   └── my-app-sim/
│       ├── main.tf            # Different composition — different services
│       ├── variables.tf
│       ├── terraform.tfvars
│       └── backend.tf         # Own state
```

### Key Principles

- Each environment has its own `main.tf` that **composes** modules — it decides which services exist
- Modules are building blocks, not full environments
- A `cloud-run-service` module is called N times per environment, once per service
- Shared infra (project, IAM, WIF) lives in modules called by every environment
- Each environment has **independent state** — applying my-app-sim can never break my-app-row
- No workspaces needed — `cd environments/my-app-row && terraform apply`

### Module Design

Structure modules around **logical service boundaries**, not resource types (source: Pixel Guild, GCP docs). A Cloud Run service module bundles: the service itself + IAM (public access) + domain mapping. It accepts dependencies as input vars (dependency inversion), doesn't create them internally.

HashiCorp advocates **flat module trees** — one level of child modules assembled in root config, not deeply nested modules calling other modules. "We call this flat style of module usage _module composition_, because it takes multiple composable building-block modules and assembles them together to produce a larger system." (HashiCorp docs)

---

## Workspaces vs Directory-Per-Env vs Terragrunt

### Workspaces: Strong Consensus Against (for this use case)

| Source | Verdict |
|--------|---------|
| Google Cloud (2026) | "Use only the default workspace" per environment directory. Multiple CLI workspaces NOT recommended. |
| HashiCorp (2025) | Workspaces valid within "composition layers" but "not a suitable tool for system decomposition" alone. |
| DevOps Directive (2025) | Single-instance root modules (directory-per-env) "easier to reason about." Workspaces force lock-step upgrades. |
| Rui Duarte (2025) | "Workspaces optimize for a superficial form of convenience while sacrificing critical architectural principles." |

**Failure modes of workspaces:**
1. No state isolation — shared backend is a single point of failure
2. Conditional logic mess — `terraform.workspace == "prod" ? ... : ...` proliferates
3. No repo visibility — can't see what's deployed where by reading the directory
4. Lock-step releases — can't test a module upgrade in staging before prod
5. Blast radius — a bad apply can cascade across environments

**One dissenting voice** (JP, DEV.to 2025): Workspaces work well WITHIN already-isolated composition layers. The community misreads HashiCorp's warning as "never use workspaces" when they mean "don't use workspaces alone." Valid narrow use case: ephemeral feature-branch environments.

### Directory-Per-Environment: The Recommended Default

**Advantages:**
- True state isolation per environment
- Each environment's composition is visible in its `main.tf`
- Progressive delivery — upgrade my-app-sim first, my-app-row later
- Simple CI/CD — each directory is an independent terraform apply target
- Environments can have completely different resource sets

**Tradeoff:** Some duplication across environment `main.tf` files. Mitigated by keeping logic in modules — environment `main.tf` is mostly module calls with different arguments.

DevOps Directive (2025): "The drift/duplication argument is mostly a straw man — config logic should live in child modules regardless."

### Terragrunt: Not Yet (for this scale)

Terragrunt solves real problems: DRY backend config, dependency orchestration (DAG), version pinning per env, hierarchical variable inheritance. v0.80 brought 42% speed improvement and Stacks feature for "pattern-level" reuse.

**But:** It adds a learning curve, extra tooling, and debugging complexity. Gruntwork rates it 0/5 for "no extra tooling needed." For 2-5 environments managed solo, plain Terraform with directory-per-env is the right complexity level.

**Revisit Terragrunt when:** 5+ environments, team members who need orchestrated deploys, or complex inter-module dependencies.

---

## App Repo vs Infra Repo: Where Do Service Definitions Live?

The research supports a clean split:

| Concern | Owner | Repo |
|---------|-------|------|
| Dockerfile, build args, health check path | App team | App repo (my-app-docs) |
| Cloud Run service definition (scaling, memory, SA, domain) | Infra / Terraform | Infra repo (terraform/) |
| Image tag (which version is deployed) | CI/CD pipeline | GitHub Actions |

**The pattern:** Terraform creates and manages the service shape. CI/CD builds the image, pushes to AR, and runs `gcloud run deploy --image=NEW_TAG` to swap the image. The `lifecycle { ignore_changes = [image] }` block in Terraform prevents Terraform from reverting CI/CD deploys.

**For canary deploys:** `gcloud run deploy --no-traffic --tag=canary` creates a new revision with zero traffic and a testable URL. Promote with `gcloud run services update-traffic --to-latest`.

---

## The `for_each` Trap

The current pattern (`for_each = var.services`) works when every environment has the same services. It breaks when:
- my-app-sim needs services A + B + C but my-app-row only needs A
- A new service should exist only in one environment
- Services have fundamentally different configurations per environment

**The fix is structural, not syntactic.** Don't try to make `for_each` conditional with `count` or `for` expressions. Move to directory-per-env where each environment's `main.tf` explicitly calls the modules it needs. Composition replaces iteration.

---

## GCP-Specific Patterns

**Google's Example Foundation** (terraform-google-modules) uses a layered approach:
1. Bootstrap (org, CI/CD)
2. Org policies
3. Environments (folders per env)
4. Networking (per env)
5. Projects (per env)
6. App infra (per env)

Each layer has its own state. Environments share a baseline module but each gets its own directory. Cross-environment communication uses `terraform_remote_state` data source.

**GCP recommends:** Max 100 resources per state, ideally "a few dozen." Tag resources with `managed_by = terraform` for drift detection. Use `terraform.tfvars` (not CLI flags) because "command-line options are ephemeral and easy to forget."

---

## Migration Path (from current setup)

1. Extract current `.tf` resources into modules (`modules/project-base/`, `modules/iam/`, `modules/cloud-run-service/`, etc.)
2. Create `environments/my-app-row/main.tf` that calls those modules
3. Import existing state into the new environment directory's state
4. Repeat for my-app-sim
5. Delete the old flat config and workspaces
6. Remove `cloudrun.yaml` from app repos — Terraform owns service definitions

---

## Open Questions

- **State backend:** Local state works solo but doesn't survive laptop loss. GCS backend per environment directory is the next step.
- **CI/CD integration:** How should `gcloud run deploy --image` be triggered? Directly from GitHub Actions, or through a Terraform apply that updates the image variable?
- **Module versioning:** For a solo operator, local module paths (`../modules/cloud-run-service`) are fine. Versioned module registry becomes relevant with team growth.

---

## Sources

| Source | Type | Date |
|--------|------|------|
| [GCP Best Practices: Root Modules](https://cloud.google.com/docs/terraform/best-practices/root-modules) | Authoritative | 2026-04-01 |
| [GCP Best Practices: General Style](https://docs.cloud.google.com/docs/terraform/best-practices/general-style-structure) | Authoritative | 2026-04-01 |
| [HashiCorp: Module Composition](https://developer.hashicorp.com/terraform/language/modules/develop/composition) | Authoritative | 2025-11-19 |
| [HashiCorp: Refactor Monolithic Config](https://developer.hashicorp.com/terraform/tutorials/modules/organize-configuration) | Authoritative | undated |
| [HashiCorp: Recommended Practices Part 1](https://developer.hashicorp.com/terraform/cloud-docs/recommended-practices/part1) | Authoritative | 2025-05-27 |
| [Google: terraform-example-foundation](https://github.com/terraform-google-modules/terraform-example-foundation) | Authoritative | ongoing |
| [Gruntwork: Multiple Environments](https://blog.gruntwork.io/how-to-manage-multiple-environments-with-terraform-32c7bc5d692) | Established | 2025-01-28 |
| [DevOps Directive: Organizing Configs](https://devopsdirective.com/posts/2025/07/organizing-terraform-configurations/) | Established | 2025-07 |
| [Spacelift: Terraform Environments](https://spacelift.io/blog/terraform-environments) | Established | 2025 |
| [Pixel Guild: Terraform at Scale GCP](https://pixelguild.com/articles/terraform-at-scale-multi-environment-gcp) | Emerging | 2026-02-18 |
| [Axel Mendoza: Terragrunt vs Terraform](https://www.axelmendoza.com/posts/terraform-vs-terragrunt/) | Emerging | 2025-08-04 |
| [Rui Duarte: Workspaces Are a Trap](https://medium.com/@ruipmduartept) | Emerging | 2025-07-24 |
| [JP: Curious Case of Workspaces](https://dev.to/aws-builders/the-curious-case-of-terraform-workspaces-3llo) | Emerging | 2025-11-12 |
| [Oussema Besbes: Multi-Env GCP](https://medium.com/@oussema.besbes_73236/) | Emerging | 2025 |
| [Terragrunt Multi-Env GCP (GitHub)](https://github.com/johnbedeir/Terragrunt-Multi-Env-GCP) | Emerging | 2025 |
