# Contributing

Thanks for looking. This repository publishes two installable Agent Skill bundles plus their checks
and documentation.

## What lives here

| Path | What it is |
| --- | --- |
| `skills/orchestra-auto/` | The English bundle: `SKILL.md`, `references/`, `scripts/`, `evals/` |
| `skills/orchestra-auto-zh/` | The Chinese bundle, the same procedure in 简体中文 |
| `tests/` | The `unittest` suites that gate a change |
| `launch-kit/` | Draft launch material — see the note at the bottom |
| `README.md`, `README.zh-CN.md` | Repository documentation, one per language |

## The one rule that matters most

**The two language variants must stay semantically identical.** They are separate installable
bundles, not a translation fallback, so a change to one is a change to both unless it is genuinely
language-specific (a quoted idle banner, a CLI flag name). Concretely:

- The same reference files exist in both, with the same trigger conditions.
- The same eval scenarios exist in both, in the same order, with the same assertion counts.
- All shipped scripts and static assets (`acceptance.py`, `chain.py`, `closeout.py`, `flow.py`,
  `health.py`, `inbox-loop.py`, `inbox-loop.sh`, `init-check.py`, `ledger.py`, `preflight.py`,
  `progress.py`, `progress-demo.py`, `progress.html`, `visual-evidence.py`) are **byte-identical**
  between the variants. The tests assert
  this; a divergence is a test failure, not a style preference. `SCRIPT_FILES` and `PYTHON_SCRIPTS` in
  `tests/test_package_content.py` are the lists those checks iterate: a new script that is not
  added to both fails the suite on purpose, because an unregistered script would otherwise be
  green and untested.

When you edit one variant, edit the other in the same commit. If you cannot translate a phrase
faithfully, rewrite both sides into something both can carry rather than letting the two drift.

## Before you open a pull request

Run the full suite. It needs no network, no credentials and no particular machine:

```bash
python3 -m unittest discover -s tests -v
```

It covers both the package's shape and the inbox script's behaviour. If you only want a quick loop
while you work, the individual commands are:

```bash
python3 -m py_compile skills/*/scripts/*.py     # Python compiles
bash -n skills/*/scripts/inbox-loop.sh                    # shell wrapper parses
python3 -m unittest tests.test_package_content -v         # package + disclosure scan
python3 -m unittest tests.test_ledger -v               # ledger storage + reconcile
python3 -m unittest tests.test_preflight -v            # preflight receipt
python3 -m unittest tests.test_health -v               # pure health state machine
python3 -m unittest tests.test_acceptance -v           # acceptance records + four levels
python3 -m unittest tests.test_closeout -v             # closeout checklist + release gate
python3 -m unittest tests.test_progress_dashboard -v   # progress CLI + HTTP
python3 -m unittest tests.test_inbox_loop -v              # script behaviour
python3 -m unittest tests.test_chain -v                   # chain resume + exit checks
python3 -m unittest tests.test_flow -v                    # explicit flow record + plan
```

GitHub Actions runs that same suite on Python 3.10 and 3.14: `ubuntu-latest` runs all modules,
`macos-latest` runs every module except the socket-bound progress one, whose five-second readiness
budget does not survive a shared runner (the workflow says so where it excludes it). The `tests`
check is required to merge into `main`. A red check is not a separate CI-only gate: if it passes
locally and fails there, the difference is the platform, and that is worth a line in the issue.

### If you change messaging

Orca 1.4.248 handles consuming `check` batches immediately and delivers native wakes to the agent. The retired `inbox-loop` entrypoints must fail before calling Orca or changing historical state. Tests model peek, consuming checks, retry, duplicate delivery and inbox recovery; the model is not live end-to-end evidence. Preserve message IDs and historical files for audit, reconcile real effects before replay, and use the selected Orca version's guide and help. No automatic replay of historical ack is supported without verified older-runtime evidence.

### If you change a SKILL.md or a reference

- Keep the frontmatter to the documented Agent Skills keys. `name` must equal the directory name.
  The description is the only text the agent sees when deciding whether to use the skill, so a
  change there is a behaviour change; make it clear and keep the trigger signals a user would
  actually type.
- Every reference file must be linked from the `SKILL.md` that ships it and must state its trigger
  near the top. The tests enforce both.
- Keep the safety invariants listed in the README intact. You may reorder and reword them; you may
  not weaken them.
- Do not hardcode a version-specific detail. If a file name, table name or receipt field is really
  version-dependent, point at the current CLI help and the current guide, and tell the reader to ask
  with evidence when their command does not match the public documentation.
- The six clauses in `references/prose-contract.md` are load-bearing. Keep the wording recognisable
  in both bundles; the anchor test looks for the phrases, and the review reads the paragraph around
  them, because the anchor test cannot check that the surrounding prose still means the same thing.

### If you touch the eval scenarios

Keep them **machine-independent**. A scenario must not contain a home-directory path, a private
repository or ticket name, a specific host's settings snapshot, or a claim that a test was run when
it was not. A scenario may test the *reasoning* (read the live setting; do not guess) without
baking in a value. When a scenario needs a filesystem path — a pre-skill's absolute path, say —
supply a self-contained fixture (`SKILLS_ROOT` / `PRE_SKILL`) and assert against that fixture, so
the model never has to invent a real machine path and a genuine path requirement still gets tested.
The `_note` field states that these are prompt-level checks, not end-to-end proof — keep that
accurate.

New scenarios are appended after the existing set, in the same order in both bundles, with the same
assertion counts. `tests/test_package_content.py` asserts that order, that the count matches the
files, and that no scenario claims a test ran. The count is never restated by hand in the README or
the launch kit: those documents are checked against the files, so a scenario added in one place and
not the other fails the suite.

## Disclosure discipline

Everything on a public branch is readable, including before it merges. Before you push:

1. Run the disclosure scan (`tests/test_package_content.py`) and read its output, not just its exit
   code.
2. Read your own diff for anything that identifies a machine, a person, a private repository or a
   private ticket.
3. If you add a new forbidden shape to the scan, add the reason alongside the pattern and check the
   allowlist still names every file that may legitimately contain it.

The scan is written to stay trustworthy: its UUID-token test asserts that the inbox script's random
temporary-file tokens are not mistaken for credentials, so a genuine finding is never buried under
false positives.

## Style

- Match the surrounding document: the Chinese bundle is written in Chinese, the English bundle in
  English, and each keeps the repository's own terminology rather than translating it word for
  word into something else.
- Prefer explaining *why* a rule exists over stacking `MUST`s. The reader is an agent that follows
  reasoning better than it follows volume.
- Long reference files get a short table of contents. `SKILL.md` stays a routing document; detail
  goes into `references/` with a stated trigger.

## Launch material is a draft, never an action

`launch-kit/` contains positioning, a demo script, FAQs and channel drafts. They are maintained so a
maintainer can publish them deliberately. Contributing to them means **improving the text**. It does
not mean posting it: do not sign in to any account, contact a community, register a listing or
submit an account binding on this repository's behalf. If you want something announced, open a PR
with the improved draft and let a maintainer do the posting.

## Licence

By contributing you agree your contribution is licensed under the repository's [MIT licence](LICENSE).
