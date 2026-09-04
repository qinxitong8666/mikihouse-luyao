---
name: issue-to-verified-push
description: Complete one GitHub Issue end-to-end in mikihouse-luyao with supplier-sync safety, repository verification, remote SHA proof, final CI gating, and durable PR evidence. Never auto-merge and never trigger Shijiu live writes unless the task explicitly authorizes them.
---

# Issue → Verified Push

Use this Skill only when explicitly invoked.

Primary purpose:

- resolve the requested GitHub Issue;
- preserve supplier/catalog invariants;
- run the repository-required verification;
- commit only task changes;
- push safely;
- prove local HEAD equals the remote feature branch;
- create or update the matching PR;
- persist truthful acceptance evidence;
- wait for final CI state before marking COMPLETED;
- never auto-merge.

## 1. Resolve scope before editing

1. Read repo-root `AGENTS.md` completely.
2. Read the referenced Issue completely.
3. Inspect the current branch, status, remotes and relevant implementation.
4. Identify explicit allowed files / forbidden files / side effects.
5. Determine the required validation level from `AGENTS.md`:
   - Level A: default offline verification;
   - Level B: real Storefront read-only smoke;
   - Level C: Shijiu planning/dry-run;
   - Level D: Shijiu live/recovery/browser-exact.
6. If the Issue does not explicitly authorize production Shijiu writes, treat all Shijiu live write operations as forbidden.

Do not infer authorization for CREATE/UPDATE/image upload/delete/deactivate/reactivate from phrases such as “验证”“修复”“跑通”“测试” or “完成 Issue”.

## 2. Preserve dirty state

Before making changes:

```bash
git status -sb
git branch --show-current
git remote -v
```

Rules:

- preserve pre-existing local edits;
- do not reset, clean, checkout-overwrite or stash user work without explicit permission;
- do not mix unrelated changes into the task commit;
- do not force push;
- do not create empty commits.

For Issue work, prefer a dedicated feature branch from the current intended base (`main` unless the Issue says otherwise).

## 3. Protect supplier and catalog invariants

When the task touches catalog/scraper/sync behavior, audit these invariants before editing:

- product stable key remains `product_number`;
- variant stable key remains `product_number::variant SKU`;
- variant price/color/size/stock semantics remain per variant;
- the 351 PDF special SKUs remain a permanent Shijiu exclusion pool;
- Storefront product and variant pagination remain complete;
- retries/re-runs do not duplicate products or variants;
- network failure or partial crawl cannot silently become deactivation;
- master catalog state commits only after a complete successful crawl and validation;
- removed products/variants preserve historical identity and can restore without new IDs;
- supplier-specific response fields do not become unstable core identifiers.

If an Issue proposes changing one of these invariants, make the migration explicit in code/tests/evidence instead of silently rewriting behavior.

## 4. Protect Shijiu state and live writes

### Default mode

Unless explicitly authorized, the task must remain offline/read-only with respect to Shijiu production.

Do not execute:

- CREATE;
- UPDATE;
- image upload;
- delete;
- deactivate;
- reactivate;
- any request that mutates Shijiu goods.

Do not use production credentials merely because they are present in the environment.

### If live write is explicitly authorized

Before any live write:

1. confirm the Issue/command explicitly authorizes the mutation;
2. verify the explicit write confirmation gate;
3. perform read-only discovery/duplicate-risk/contract checks;
4. scope the write to the smallest required item/batch;
5. fail closed on any contract or readback uncertainty;
6. capture redacted evidence;
7. never persist token/secret/cookie/authorization.

A failed/uncertain live write must never be reported as completed based only on a local test passing.

## 5. Protect persistent state and deliverables

Before verification or task-specific commands that might write files, note the status of:

- `state/**`;
- `deliverables/**`;
- formal Storefront master catalog / production output if present.

For normal Issue work these must not be unexpectedly modified.

If the task intentionally changes them, document the old/new state and why.

Do not commit temporary crawl dumps, credentials, browser session artifacts, debug screenshots, or large generated files unless the Issue explicitly requires them as deliverables.

## 6. Implement the smallest correct change

- Reuse existing helpers and contracts before adding new parallel logic.
- Keep supplier-specific assumptions isolated.
- Add/update tests for behavior changes.
- For documentation-only tasks, do not “improve” code opportunistically.
- For PDF/UI/visual changes, remember unit tests do not replace real visual QA.

## 7. Run repository verification

Always run the repo-root unified entrypoint when it exists:

```bash
python scripts/verify_local.py
```

Capture the actual result.

Do not reconstruct PASS later from memory or from a clean working tree.

Then apply additional validation required by the task:

### Level B — Storefront read-only online

For scraper/catalog/Storefront contract changes, run the Issue-specified real read-only smoke when appropriate.

Record:

- command;
- PASS/FAIL/NOT CAPTURED;
- network/source scope;
- relevant item counts or samples;
- whether any persistent master state was committed.

Never infer online correctness from offline pytest.

### Level C — Shijiu planning/dry-run

For planning/contract/payload changes, run only dry-run/contract checks unless the Issue explicitly authorizes live writes.

Record:

- dry-run/contract result;
- exclusion and duplicate-risk checks;
- whether protected state changed.

### Level D — Shijiu live/recovery/browser-exact

Offline verification is mandatory first.

Actual production Shijiu verification is `NOT APPLICABLE` unless explicitly authorized. If authorized, record the real write/readback result and redacted forensics.

### PDF / visual

If the task changes PDF rendering/layout/images, record render/manual visual QA separately. Do not mark visual QA PASS without actual inspection.

## 8. Inspect the final diff

Before commit:

```bash
git status -sb
git diff --check
git diff --cached --check
```

Review the diff for:

- secrets or auth material;
- accidental state/deliverable changes;
- generated crawl/debug artifacts;
- unrelated source changes;
- unexpected large files;
- temporary browser/session files;
- special SKU exclusion or price-guard drift not requested by the Issue.

## 9. Commit task-only changes

Commit only files required by the Issue.

Do not create an empty commit merely because the user said “已推送” or because the remote is already current.

If the requested behavior already exists and no code change is necessary, verify it and report that truthfully instead of manufacturing a commit.

## 10. Push and prove remote SHA

Push safely:

```bash
git rev-parse HEAD
git push <remote> HEAD:<feature-branch>
git ls-remote <remote> refs/heads/<feature-branch>
```

Store:

- `LOCAL_SHA` = full local HEAD;
- `REMOTE_SHA` = full SHA returned for the remote feature branch.

`Remote verified: YES` only when:

```text
LOCAL_SHA == REMOTE_SHA
```

`Everything up-to-date` alone is not proof.

## 11. Persist PR evidence for GitHub Issue work

For Issue-driven work delivered on a feature branch:

1. look for an existing open PR with this head branch → base branch;
2. reuse/update it if present;
3. otherwise create one;
4. include `Closes #<issue>` unless the Issue should intentionally remain open after merge;
5. never auto-merge from this Skill.

Use this evidence structure:

```text
Issue: #<number>
Status: AWAITING_CI / READY FOR REVIEW / COMPLETED / PARTIAL / BLOCKED
Branch: <branch>
Commit: <short SHA> <message>
Local SHA: <full SHA>
Remote SHA: <full SHA>
Remote verified: YES / NO

Verification:
- unified offline verification: PASS / FAIL / NOT CAPTURED
- pytest: PASS / FAIL / NOT CAPTURED
- config / Node syntax: PASS / FAIL / NOT APPLICABLE / NOT CAPTURED
- Storefront read-only smoke: PASS / FAIL / NOT APPLICABLE / NOT CAPTURED
- Shijiu planning/dry-run: PASS / FAIL / NOT APPLICABLE / NOT CAPTURED
- Shijiu live write: PASS / FAIL / NOT APPLICABLE / NOT AUTHORIZED / NOT CAPTURED
- PDF/manual visual QA: PASS / FAIL / NOT APPLICABLE / NOT CAPTURED
- protected state/deliverables unchanged: YES / NO / NOT CAPTURED
- CI: PASS / FAIL / AWAITING / NOT APPLICABLE
- git diff --check: PASS / FAIL / NOT CAPTURED

Secrets / production safety:
- credentials persisted: NO / YES / UNKNOWN
- unintended production writes: NO / YES / UNKNOWN

Working tree:
- clean / remaining pre-existing files / remaining task files / NOT CAPTURED

Follow-ups or risks:
- none, or explicit items
```

Evidence rules:

- record only directly observed facts;
- missing output is `NOT CAPTURED`, never inferred PASS;
- production write without explicit authorization is a blocker, not a successful validation;
- unexpected protected state/deliverable mutation blocks `COMPLETED` until understood and resolved.

## 12. Final CI gating

If GitHub Actions/CI applies to the PR:

- queued/in_progress means Status is `AWAITING_CI` or `READY FOR REVIEW`, never `COMPLETED`;
- CI failure means `PARTIAL` or `BLOCKED` depending on the failure;
- only after final CI success may the PR evidence be upgraded to `COMPLETED`, assuming all other applicable checks are PASS/N/A and remote SHA is verified.

After final CI result, update both:

1. PR evidence;
2. Issue index comment.

The Issue index should not remain stuck at “awaiting CI” after CI has completed.

## 13. Issue index comment

When possible, add or update one concise Issue comment:

```text
PR: #<pr>
Branch: <branch>
Verified remote SHA: <full SHA>
Status: <COMPLETED / AWAITING_CI / PARTIAL / BLOCKED>
```

For completed CI, mention the final CI state concisely.

## 14. Final report to the user

Report:

- Status;
- Issue;
- branch;
- commit;
- local SHA;
- remote SHA;
- Remote verified YES/NO;
- PR number;
- evidence persisted YES/NO;
- unified verification result;
- applicable Storefront/Shijiu/PDF validation result;
- protected state/deliverable result;
- CI result;
- any residual risk.

Never claim COMPLETED when:

- applicable verification failed;
- remote SHA is not proven;
- mandatory PR evidence is missing;
- CI is still pending;
- protected state changed unexpectedly;
- production Shijiu behavior was asserted without actual authorized evidence.
