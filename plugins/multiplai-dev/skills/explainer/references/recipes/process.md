# Recipe: a process

A deployment, a release, an on-call routine, an onboarding: something people and machines do in order.
The question is "What happens when …" or "How do we …".

## Gather

1. The runbook, if there is one, and every script, CI workflow and config file it names. Read the
   scripts: a runbook can be out of date, the script is what runs.
2. Who does each step: a person (by role), a CI job, a scheduled job, an agent on a machine.
3. Where the process can stop or fail, and what happens then: retries, rollbacks, who gets told.
4. Ask the user only for what no file says: who owns a step, what is planned but not built.

When a runbook and a script disagree, the page follows the script and a `.callout` says where the
runbook differs.

## Shape

- **Answer**: what the process produces and who sets it off ("Tagging `s23.1` builds the image in CI and
  the VM's release agent deploys it to staging").
- **Picture**: one lane per actor (`text.lane`: developer, CI, registry, server) when more than two
  actors take part; otherwise boxes in a row.
- **Worked example**: one run of the process, with its real names and values where they may be shown (a
  tag, a version, an image name), followed card by card.
- **Each card's where line**: the script or job and where it runs (`deploy/release.py · CI · on tag push`).
- **Deeper sections**: "What happens when it fails"; "What you need before you start" (access,
  secrets by name only, never values); "What's not built yet" (`.road`).
- **Status words**: automated / by hand / not built yet.

Never put a secret's value, a token or a password on the page, even when a file you read holds one.
Name the secret and where it lives.
