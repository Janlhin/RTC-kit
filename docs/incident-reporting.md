# Incident reporting

[简体中文](incident-reporting.zh-CN.md)

This guide closes the documentation loop from a local error to an approved issue (#68, #74 layer B).
It does not detect outages or repair the scheduler.
External outage detection remains #25.
A failed scheduler cannot reliably report itself through the same broken path.

### Record what is known

Keep the existing append-only, one-sentence scene -> consequence -> next-action entry in errors.md.
Separate observed facts from hypotheses.
Record only evidence actually obtained; mark proposed verification as not run.
Unexpected disagreement between system files still means stop, record and tell the user; do not repair those files as part of reporting.

Synthetic local example (not a reproduction of the reported incident):

> Scene: due exited 1 in a disposable workspace (observed) -> consequence: that invocation produced no reminder output (observed; delivery not checked) -> next action: inspect the captured error; a missing module is only a hypothesis, not yet verified.

One failed invocation does not prove an outage duration, missed delivery or root cause.
Never invent module names.
A first possible system defect can justify proposing an issue; only a second similar occurrence triggers the separate proposal to create a standing rule.
Both require human approval before their respective external/policy changes.

### Search, redact and prepare

Search the chosen repository for existing reports first.
Keep a separate local draft outside runtime state files, even if authentication or network access fails.
An existing report may need a reply rather than a duplicate issue; approval is required for either action.

```powershell
gh issue list --repo <OWNER/REPO> --state all --search "<minimal symptom>"
```

Prepare a new, separate draft file, not a copy of errors.md. Include only:

- Affected version/commit and a minimal, sanitized setup.
- Observed behavior and expected behavior.
- Exact verification performed and its result; identify unrun steps.
- Hypotheses explicitly marked unverified, and the next proposed check.
- Relevant existing issue links and any remaining uncertainty.

Exclude personal schedules, usernames/addresses, absolute local paths, credentials, conversation text and unrelated logs.
Redact every proposed excerpt before requesting approval.
Do not attach errors.md or whole state files.
If necessary evidence cannot be safely redacted, ask the user which private channel to use instead.

### Obtain approval, then submit

Show the exact destination repository, title, body and each attachment to the user.
Approval for one draft is not permission for different text, another repository or additional attachments.
Urgent human notification is separate from permission to publish an issue.

The command below is an example for use only after that approval.
Never run it during documentation verification.
Replace placeholders with reviewed values; the body file must contain only the approved sanitized draft.
If submission fails, retain the draft and report the failure; do not retry indefinitely or block scheduling.
Search before retrying if the outcome is uncertain, to avoid duplicates.

```powershell
gh issue create --repo <OWNER/REPO> --title "<approved title>" --body-file "<approved-draft.md>"
```

The following is entirely fictional, including the version and search result; it is not a completed search or submission.

- Destination: `<OWNER/REPO>`
- Title: `due exits without an actionable diagnostic (synthetic example)`
- Version/commit: `<tested-commit>` (placeholder, not verified evidence)
- Setup: disposable workspace with synthetic reminders; no personal data.
- Existing issue search: no matching report (fictional result).

Proposed body:

> Observed: one due invocation exited 1 in a disposable workspace and produced no reminder output.
Expected: successful evaluation or an actionable diagnostic.
Cause: unverified; missing module is a hypothesis only.
Delivery and outage duration: not checked.
Next proposed step: inspect a redacted error excerpt.
No raw workspace files attached.

### Track without rewriting history

After a confirmed submission, append a short follow-up with the issue URL and status to errors.md; preserve earlier entries.
An issue is a tracking item, not proof of a fixed defect.
Only record a fix as verified after checking the relevant revision and result.
Reporting failure alone must not stop scheduling.
This does not override the existing stop-and-report rule for inconsistent system files.

Fictional follow-up entry (append only after a real submission has been confirmed):

> Scene: approved report submitted at `<confirmed-issue-URL>` -> consequence: tracked, not fixed -> next action: await triage and verify any proposed correction.

Approved excerpts may be included in the body. The create command above does not upload attachments; any separately approved attachment must be shared manually through the reviewed channel. A reply to an existing issue also requires approval of its exact destination and text.
