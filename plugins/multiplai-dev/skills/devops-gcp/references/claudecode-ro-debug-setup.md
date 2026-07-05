# Claude Code + gcloud — Locked-Down Read-Only Debug Setup

Two layers — lock it down on both the gcloud side (IAM) and the Claude Code side (permissions). Belt and suspenders: IAM is the hard wall, Claude Code permissions are the soft wall.

## 1. gcloud side — read-only service account

The strongest lock is at the IAM layer. Create a dedicated SA so even if Claude runs `gcloud secrets versions access`, GCP refuses.

```bash
PROJECT=your-project-id
gcloud iam service-accounts create claude-debug \
  --display-name="Claude Code debug (read-only)" --project=$PROJECT

SA=claude-debug@$PROJECT.iam.gserviceaccount.com

# Read-only roles — pick what you actually need
for ROLE in \
  roles/viewer \
  roles/logging.viewer \
  roles/monitoring.viewer \
  roles/iam.securityReviewer \
  roles/cloudsql.viewer; do
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:$SA" --role="$ROLE"
done

# Key file, kept out of repo
gcloud iam service-accounts keys create ~/.config/gcloud/claude-debug.json \
  --iam-account=$SA
```

### Notes on roles
- `roles/viewer` excludes Secret Manager payload access (`secretmanager.versions.access`) by design — it can list secrets metadata but not read values.
- `roles/iam.securityReviewer` lets it check permissions without modifying them.
- Do NOT add `roles/editor`, `roles/secretmanager.*`, or `roles/iam.serviceAccountTokenCreator` (the last one lets you impersonate other SAs and escape the box).

### Isolate as its own gcloud configuration

Activate it in a separate gcloud config so it doesn't clobber your normal login:

```bash
gcloud config configurations create claude-debug
gcloud auth activate-service-account --key-file=~/.config/gcloud/claude-debug.json
gcloud config set project $PROJECT
gcloud config configurations activate default  # switch back
```

Then Claude uses it via `CLOUDSDK_ACTIVE_CONFIG_NAME=claude-debug gcloud ...` or `GOOGLE_APPLICATION_CREDENTIALS=~/.config/gcloud/claude-debug.json`.

## 2. Claude Code side — permission allowlist

Belt-and-suspenders: even with read-only IAM, restrict which `gcloud` subcommands Claude can run without prompting. In `.claude/settings.json` (project) or `~/.claude/settings.json` (global):

```json
{
  "env": {
    "CLOUDSDK_ACTIVE_CONFIG_NAME": "claude-debug"
  },
  "permissions": {
    "allow": [
      "Bash(gcloud logging read:*)",
      "Bash(gcloud logging logs list:*)",
      "Bash(gcloud projects describe:*)",
      "Bash(gcloud projects get-iam-policy:*)",
      "Bash(gcloud iam roles describe:*)",
      "Bash(gcloud iam service-accounts list:*)",
      "Bash(gcloud iam service-accounts get-iam-policy:*)",
      "Bash(gcloud run services list:*)",
      "Bash(gcloud run services describe:*)",
      "Bash(gcloud compute instances list:*)",
      "Bash(gcloud compute instances describe:*)",
      "Bash(gcloud sql instances list:*)",
      "Bash(gcloud sql instances describe:*)",
      "Bash(gcloud monitoring:*)",
      "Bash(gcloud config list:*)",
      "Bash(gcloud config configurations list:*)"
    ],
    "deny": [
      "Bash(gcloud secrets:*)",
      "Bash(gcloud kms:*)",
      "Bash(gcloud auth:*)",
      "Bash(gcloud iam service-accounts keys:*)",
      "Bash(gcloud * delete*)",
      "Bash(gcloud * create*)",
      "Bash(gcloud * update*)",
      "Bash(gcloud * set-iam-policy*)",
      "Bash(gcloud * add-iam-policy-binding*)"
    ]
  }
}
```

Deny rules win over allow. Anything not on the allow list still prompts you, so it fails closed.

## 3. Optional — extra paranoia

- Add a `PreToolUse` hook that hard-blocks any Bash command matching `gcloud (secrets|kms|auth)` regardless of settings.
- Set `gcloud config set auth/disable_credentials true` on the claude-debug config to prevent it from quietly using your user creds as fallback.
- Run Claude in your container (claude-multiplai:local) so the SA key is only mounted there, not on the host.

## Threat model recap

| Layer | What it stops | What it doesn't stop |
|-------|---------------|----------------------|
| IAM (read-only SA) | Any write/delete; secret payload reads; SA impersonation | Reading whatever the read-only roles permit (logs may contain sensitive data) |
| Claude Code allowlist | Unintended subcommands running without prompt | A user manually approving a prompted command |
| PreToolUse hook | Specific dangerous patterns even if allowlisted | Patterns you didn't anticipate |

The IAM layer is the only one that's actually a security boundary. The Claude Code layers reduce the chance of accidental damage and surface what's happening — they're not adversarial defenses.

## Caveat — logs can contain secrets

`roles/logging.viewer` lets Claude read Cloud Logging. If your apps log request bodies, JWTs, or stack traces with credentials, Claude sees them. Either scrub at the log source or accept that "read-only" here still means "can read anything logged."
