#!/usr/bin/env python3
"""Package checks for the published skill bundles.

These checks are about the *package*, not about behaviour: they verify that the two
language variants are structurally equivalent, that each SKILL.md carries valid YAML
frontmatter whose ``name`` matches its directory, that every relative reference in the
docs resolves to a file that exists, that the JSON metadata parses and uses the shape
the eval tooling expects, and that the published tree carries no machine-local or
personal data.

The leak scan is deliberately both *narrow* and *explained*: it looks for the shapes
that actually appeared in the private original (home-directory paths, a personal user
name, a specific legacy database filename, a private repository name, and credential
variable names), and every pattern that can have a benign match is paired with a
documented allowlist. A random UUID used as a temporary-file token in the inbox script
is exactly such a benign match and must NOT be reported as a credential.
"""

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_NAMES = ("orchestra-auto", "orchestra-auto-zh")
SKILL_DIRS = tuple(REPO_ROOT / "skills" / name for name in SKILL_NAMES)

#: Every script or static asset that ships in a bundle. The byte-parity and compile checks
#: iterate this list, and a completeness test fails when a shipped file is missing from it, so
#: a new script cannot land with a green suite and no coverage.
SCRIPT_FILES = (
    "acceptance.py",
    "health.py",
    "inbox-loop.py",
    "inbox-loop.sh",
    "ledger.py",
    "preflight.py",
    "progress-demo.py",
    "progress.html",
    "progress.py",
)
PYTHON_SCRIPTS = (
    "acceptance.py",
    "health.py", "inbox-loop.py", "ledger.py", "preflight.py", "progress-demo.py",
    "progress.py",
)

REFERENCE_FILES = (
    "dispatch-loop.md",
    "dispatch-preflight.md",
    "failed-dispatch-recovery.md",
    "failure-and-reporting.md",
    "gate-acceptance.md",
    "gates-docs-planning.md",
    "inbox-and-waiting.md",
    "ledger.md",
    "long-horizon-tickets.md",
    "reformat-noise-recovery.md",
    "reply-protocol.md",
    "progress-dashboard.md",
    "run-health.md",
    "spec-contract.md",
    "worker-start-and-recovery.md",
)

# The safety invariants the spec calls out, with the term each language uses for it. Reordering and
# rewording are allowed; dropping an invariant is not.
EN_INVARIANTS = (
    "worktree",           # isolated top-level worktree per ticket
    "turnStart",          # formal evidence that a worker's turn began
    "disabled TUI agent",  # live preference and disabled-state check
    "deliveryId",         # durable, deduplicated message handling
    "escalation",         # decisions go to a human
    "G-merge",            # acceptance before merge
    "implement-spec",     # long-horizon map goes through one worker
    "integration branch", # one integration PR, not per-ticket PRs
    "one-way",            # a one-way door needs a human merge
    "retro",              # process errors produce candidates, not auto-edits
    "reclaimable",        # worker lifecycle close-out
)
ZH_INVARIANTS = (
    "worktree",
    "turnStart",
    "禁用 TUI agent",
    "deliveryId",
    "escalation",
    "G-merge",
    "implement-spec",
    "integration branch",
    "单向门",
    "retro",
    "reclaimable",
)

# Text files that make up the published surface. Binary-ish and generated paths are
# excluded by suffix rather than by directory so a stray cache directory cannot hide.
TEXT_SUFFIXES = {".md", ".py", ".sh", ".json", ".jsonc", ".txt", ".yml", ".yaml", ".html", ""}
IGNORED_DIRECTORIES = {
    ".git", "__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache",
    "node_modules", ".venv", "venv",
}


def tracked_text_files():
    """Every published text file, from git's index plus untracked-not-ignored files.

    Using git keeps the scan aligned with what would actually be published, and also
    lets a caller run the scan before and after staging.
    """
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    for relative in sorted(listing):
        path = REPO_ROOT / relative
        if not path.is_file():
            continue
        if any(part in IGNORED_DIRECTORIES for part in path.parts):
            continue
        if path.suffix not in TEXT_SUFFIXES:
            continue
        yield relative, path


def read_frontmatter(text):
    """Return the raw YAML frontmatter block of a Markdown document, or ``None``.

    This intentionally does not pull in a YAML library: the package only needs the two
    required scalar fields, plus a syntax sanity check, and a standard-library-only
    dependency set keeps the checks runnable on a bare Python install.
    """
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    if end == -1:
        return None
    return text[4:end]


def frontmatter_scalar(block, key):
    """Read a scalar frontmatter value, supporting plain and block (``>-``) forms."""
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(key + ":"):
            continue
        remainder = line[len(key) + 1:].strip()
        if remainder in (">-", ">", "|", "|-"):
            collected = []
            for following in lines[index + 1:]:
                if following.strip() and not following.startswith((" ", "\t")):
                    break
                collected.append(following.strip())
            return " ".join(part for part in collected if part).strip()
        return remainder.strip("'\"")
    return None


def relative_markdown_links(text):
    """Yield relative Markdown link targets, ignoring absolute URLs and anchors."""
    for target in re.findall(r"\]\(([^)]+)\)", text):
        target = target.split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        yield target


def referenced_paths(text):
    """Every repo-relative path a document mentions, linked or merely written.

    A routing table in SKILL.md names a reference in backticks rather than as a link, and
    that is a legitimate way to point at a conditionally-read file: the table's third
    column carries the trigger, which a bare link would not. So reachability is judged
    over both forms; a path that appears in neither is genuinely orphaned.
    """
    yield from relative_markdown_links(text)
    yield from re.findall(r"`([^`\s]+\.md)`", text)


class StructureTests(unittest.TestCase):
    def test_both_language_variants_exist(self):
        for directory in SKILL_DIRS:
            self.assertTrue(directory.is_dir(), f"missing skill directory {directory}")

    def test_reference_files_match_across_variants(self):
        for name in SKILL_NAMES:
            directory = REPO_ROOT / "skills" / name / "references"
            present = sorted(p.name for p in directory.glob("*.md"))
            self.assertEqual(present, sorted(REFERENCE_FILES), f"{name} references")

    def test_scripts_match_across_variants(self):
        for filename in SCRIPT_FILES:
            english = (SKILL_DIRS[0] / "scripts" / filename).read_bytes()
            chinese = (SKILL_DIRS[1] / "scripts" / filename).read_bytes()
            self.assertEqual(english, chinese, f"{filename} differs between variants")

    def test_skill_directories_hold_only_expected_content(self):
        expected = {"SKILL.md", "references", "scripts", "evals", "eval.yaml"}
        for directory in SKILL_DIRS:
            present = {p.name for p in directory.iterdir() if p.name != ".DS_Store"}
            self.assertEqual(present, expected, f"unexpected content in {directory}")


def required_frontmatter(directory, name):
    """Return a SKILL.md's frontmatter block, failing the test when it is absent.

    Returning ``str`` (rather than ``str | None``) keeps every caller from having to
    re-check for absence: a missing frontmatter is already a test failure here.
    """
    block = read_frontmatter((directory / "SKILL.md").read_text())
    if block is None:
        raise AssertionError(f"{name}: SKILL.md has no YAML frontmatter")
    return block


class FrontmatterTests(unittest.TestCase):
    def test_frontmatter_is_present_and_names_match_directories(self):
        for name, directory in zip(SKILL_NAMES, SKILL_DIRS, strict=True):
            block = required_frontmatter(directory, name)
            self.assertEqual(
                frontmatter_scalar(block, "name"), name,
                f"{name}: frontmatter name must equal the directory name",
            )

    def test_description_is_present_and_substantial(self):
        for name, directory in zip(SKILL_NAMES, SKILL_DIRS, strict=True):
            description = frontmatter_scalar(required_frontmatter(directory, name), "description")
            self.assertTrue(description, f"{name}: description must be non-empty")
            self.assertGreater(
                len(description or ""), 120,
                f"{name}: description is too thin to trigger the skill reliably",
            )

    def test_frontmatter_keys_are_the_agent_skills_ones(self):
        """Only the documented Agent Skills keys may appear in the frontmatter."""
        allowed = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
        for name, directory in zip(SKILL_NAMES, SKILL_DIRS, strict=True):
            block = required_frontmatter(directory, name)
            keys = {
                line.split(":", 1)[0].strip()
                for line in block.splitlines()
                if line and not line.startswith((" ", "\t", "#")) and ":" in line
            }
            self.assertTrue(keys <= allowed, f"{name}: unexpected frontmatter keys {keys - allowed}")

    def test_frontmatter_values_contain_no_tabs(self):
        """A literal tab breaks YAML block scalars; guard against editors adding one."""
        for name, directory in zip(SKILL_NAMES, SKILL_DIRS, strict=True):
            block = required_frontmatter(directory, name)
            self.assertNotIn("\t", block, f"{name}: frontmatter contains a literal tab")


class ReferenceIntegrityTests(unittest.TestCase):
    def test_every_relative_link_in_docs_resolves(self):
        failures = []
        for path in REPO_ROOT.rglob("*.md"):
            if any(part in IGNORED_DIRECTORIES for part in path.parts):
                continue
            text = path.read_text()
            for target in relative_markdown_links(text):
                candidate = (path.parent / target).resolve()
                if not candidate.exists():
                    failures.append(f"{path.relative_to(REPO_ROOT)} -> {target}")
        self.assertEqual(failures, [], f"broken relative links: {failures}")

    def test_skill_entrypoint_stays_a_routing_document(self):
        """SKILL.md must stay an entrypoint, not become the procedure.

        CONTRIBUTING.md says the main skill stays a routing document and detail goes into
        references, and the Agent Skills guidance recommends staying under 500 lines; this repo targets 300 to keep activation context lean. An entrypoint
        that grows past that stops being readable as an index, which is what makes the references
        hard to find.
        """
        for name, directory in zip(SKILL_NAMES, SKILL_DIRS, strict=True):
            lines = (directory / "SKILL.md").read_text().splitlines()
            self.assertLess(
                len(lines), 300,
                f"{name}: SKILL.md is {len(lines)} lines; move detail into references",
            )

    def test_both_variants_keep_the_safety_invariants(self):
        """Every safety invariant the spec calls out must survive in both languages.

        Reordering and rewording are allowed; dropping one is not. This is the regression guard for
        the constraint that the published packages must not weaken these protections. Each language
        is checked against its own wording for the two invariants that differ.
        """
        expectations = {
            "orchestra-auto": EN_INVARIANTS,
            "orchestra-auto-zh": ZH_INVARIANTS,
        }
        for name, directory in zip(SKILL_NAMES, SKILL_DIRS, strict=True):
            entrypoint = (directory / "SKILL.md").read_text()
            references = "\n".join(
                p.read_text() for p in (directory / "references").glob("*.md")
            )
            corpus = entrypoint + "\n" + references
            for invariant in expectations[name]:
                self.assertIn(
                    invariant, corpus,
                    f"{name}: the safety invariant {invariant!r} is missing",
                )

    def test_dispatch_guardrails_stay_in_both_entrypoints(self):
        """Keep dispatch prerequisites in the always-loaded entrypoints."""
        expectations = {
            "orchestra-auto": (
                "--worktree new-top-level",
                "--agent",
                "agent_unconfigured",
                "`state: ready` is **not** `turnStart`",
                "Help examples are not an exhaustive",
            ),
            "orchestra-auto-zh": (
                "--worktree new-top-level",
                "--agent",
                "agent_unconfigured",
                "`state: ready` **不是** `turnStart`",
                "帮助中的示例不是完整支持列表",
            ),
        }
        for name, required_phrases in expectations.items():
            entrypoint = (REPO_ROOT / "skills" / name / "SKILL.md").read_text()
            for phrase in required_phrases:
                self.assertIn(
                    phrase, entrypoint,
                    f"{name}: SKILL.md is missing dispatch guardrail {phrase!r}",
                )

    def test_agent_help_examples_are_not_an_allowlist(self):
        cases = {
            "orchestra-auto": ("example list is not exhaustive", "let Orca validate it", "do not substitute"),
            "orchestra-auto-zh": ("示例不是穷举", "由 Orca 校验", "不替换 agent"),
        }
        for name, phrases in cases.items():
            preflight = (REPO_ROOT / "skills" / name / "references" / "dispatch-preflight.md").read_text()
            for phrase in phrases:
                self.assertIn(phrase, preflight, f"{name}: missing {phrase!r}")

    def test_behavior_rules_carry_all_five_escalation_triggers(self):
        """The spec template's escalation rule must list all five triggers in both languages.

        A partial list is worse than none: a translator dropping "paid service / manual approval"
        silently narrows what a worker is allowed to escalate, and the two variants then disagree
        about the safety rule they both claim to share.
        """
        pairs = (
            ("orchestra-auto", ("credentials", "third party", "paid service", "manual approval",
                                "irreversible live-data")),
            ("orchestra-auto-zh", ("凭证", "第三方", "付费", "人工审批", "不可逆")),
        )
        for name, triggers in pairs:
            body = (REPO_ROOT / "skills" / name / "references" / "spec-contract.md").read_text()
            for trigger in triggers:
                self.assertIn(
                    trigger, body,
                    f"{name}: the escalation rule is missing the trigger {trigger!r}",
                )

    def test_spec_contract_requires_a_test_tier_line(self):
        """Narrow/wide is a coordinator decision the worker can only see through the spec.

        G-impl rule 1 and the pre-dispatch tick sheet both make the coordinator choose a test
        tier, but a tier that never reaches the spec is invisible to the worker: it falls back to
        the full suite or picks a few tests itself. The template line is what turns that judgement
        into a deliverable, and the preflight must demand it mechanically rather than from memory.
        """
        pairs = (
            ("orchestra-auto", ("Test tier:", "narrow", "wide", "blast radius is bounded")),
            ("orchestra-auto-zh", ("测试档：", "窄", "宽", "影响面可圈定")),
        )
        for name, phrases in pairs:
            references = REPO_ROOT / "skills" / name / "references"
            contract = (references / "spec-contract.md").read_text()
            preflight = (references / "dispatch-preflight.md").read_text()
            for phrase in phrases:
                self.assertIn(
                    phrase, contract,
                    f"{name}: the spec template is missing the test-tier wording {phrase!r}",
                )
            self.assertIn(
                phrases[0], preflight,
                f"{name}: the preflight does not require the spec's {phrases[0]!r} line",
            )

    def test_no_reference_link_uses_a_fragment(self):
        """Every #fragment must resolve to a real heading in the target document.

        The file-existence test cannot see anchors, so a stale fragment silently rots into a link
        that lands on the wrong part of a page — or nowhere. Validating the slug is what makes the
        README's claim that every relative link resolves actually true.
        """
        def heading_slugs(path):
            # GitHub-style anchor for a heading: lowercase, punctuation dropped, spaces hyphens.
            slugs = set()
            for line in path.read_text().splitlines():
                if not line.startswith("#"):
                    continue
                heading = re.sub(r"^#+\s*", "", line).strip().lower()
                heading = re.sub(r"[^\w\s-]", "", heading, flags=re.UNICODE)
                slugs.add(re.sub(r"\s+", "-", heading))
            return slugs

        offenders = []
        for path in REPO_ROOT.rglob("*.md"):
            if any(part in IGNORED_DIRECTORIES for part in path.parts):
                continue
            for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                if "#" not in target:
                    continue
                file_part, fragment = target.split("#", 1)
                target_doc = (path.parent / file_part).resolve()
                if not target_doc.is_file():
                    continue  # the file-existence test reports this case
                if fragment not in heading_slugs(target_doc):
                    offenders.append(f"{path.relative_to(REPO_ROOT)} -> {target}")
        self.assertEqual(
            offenders, [],
            "these links point at an anchor that does not exist in the target: "
            + "; ".join(offenders),
        )

    def test_no_test_or_doc_names_a_tool_that_does_not_exist(self):
        """Referencing a nonexistent script misleads readers about how verification works.

        An earlier draft of the scanner pointed at a hypothetical pre-push helper; the mechanism
        actually used is the documented ``PRIVATE_IDENTIFIERS`` environment variable. Only docs and
        skills are checked: this test necessarily contains the name to look for, and letting it
        check itself would make the assertion a no-op.
        """
        for path in list(REPO_ROOT.rglob("*.md")):
            if any(part in IGNORED_DIRECTORIES for part in path.parts):
                continue
            self.assertNotIn(
                "prepush_scan", path.read_text(errors="replace"),
                f"{path.relative_to(REPO_ROOT)} names a script that is not in the repository",
            )
        for directory in SKILL_DIRS:
            for path in list(directory.rglob("*.md")) + list(directory.rglob("*.json")):
                self.assertNotIn(
                    "prepush_scan", path.read_text(errors="replace"),
                    f"{path.relative_to(REPO_ROOT)} names a script that is not in the repository",
                )

    def test_each_reference_is_reachable_from_its_skill(self):
        """Every reference file must be named from the SKILL.md that ships it.

        Naming it in a routing table counts: the table's third column states the trigger,
        which is what makes the file conditional in the first place.
        """
        for name, directory in zip(SKILL_NAMES, SKILL_DIRS, strict=True):
            mentioned = set(referenced_paths((directory / "SKILL.md").read_text()))
            for filename in REFERENCE_FILES:
                relative = f"references/{filename}"
                self.assertTrue(
                    relative in mentioned or filename in mentioned,
                    f"{name}: {filename} is shipped but never referenced",
                )

    def test_reference_files_declare_their_trigger(self):
        """A conditionally-read reference must say when to read it, in its first lines."""
        markers = ("When to read", "什么时候读", "何时读取", "何时读", "When to read this")
        for name, directory in zip(SKILL_NAMES, SKILL_DIRS, strict=True):
            for filename in REFERENCE_FILES:
                head = "\n".join(
                    (directory / "references" / filename).read_text().splitlines()[:12]
                )
                self.assertTrue(
                    any(marker in head for marker in markers),
                    f"{name}/{filename}: no trigger statement near the top",
                )


def squash(text):
    """Collapse whitespace so an assertion survives re-wrapping."""
    return " ".join(text.split())


def compact(text):
    """Drop whitespace entirely for phrase checks that must survive CJK line breaks.

    ``squash`` keeps one space between words, which is right for an English phrase. A Chinese
    sentence has no word boundary to wrap on, so a template that reflows mid-phrase breaks a
    phrase check that kept the space; dropping whitespace on both sides of the comparison makes
    the assertion depend on the wording and nothing else.
    """
    return "".join(text.split())


def markdown_sections(text):
    """Split Markdown into ``(heading, body)`` pairs, ignoring '#' inside fenced code."""
    sections, current, fenced = [], None, False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("#"):
            current = (line, [])
            sections.append(current)
        elif current is not None:
            current[1].append(line)
    return [(heading, "\n".join(body)) for heading, body in sections]


def find_section(sections, prefix, where):
    """Return the heading and body of the section whose title starts with ``prefix``."""
    for heading, body in sections:
        if heading.lstrip("# ").startswith(prefix):
            return heading + "\n" + body
    raise AssertionError(f"{where}: no section starts with {prefix!r}")


class ContractRegressionTests(unittest.TestCase):
    """Regressions that live in the prose, pinned where the reader meets them.

    Every case here guards a *decision* the documents used to get wrong: which section owns a
    fact, how many states a frontier has, which level of completion a merge may claim, who is
    allowed to re-run setup. Asserting that a sentence exists would be the weak version of
    this; where the defect was structural (a fact in the wrong section, a rule duplicated so
    the copies could drift), the check is structural too.
    """

    def corpus(self, name):
        """Return the entrypoint and the reference files of one language variant."""
        directory = REPO_ROOT / "skills" / name
        entrypoint = (directory / "SKILL.md").read_text()
        references = {path.name: path.read_text() for path in (directory / "references").glob("*.md")}
        return entrypoint, references

    def test_entrypoints_accept_an_observed_start_and_drop_the_duplicate(self):
        """Both entrypoints must state the same start rule, not one rule each.

        The review caught the Chinese entrypoint keeping the duplicated
        "`state: ready` ≠ `turnStart`" line after the English one had replaced it with the
        `observed` rule: two texts carrying two rules is exactly the drift this repository
        forbids, and it is the same class of defect as the pre-skill mapping item 5 removed.
        """
        expectations = {
            "orchestra-auto": "start evidence",
            "orchestra-auto-zh": "启动证据",
        }
        for name, evidence in expectations.items():
            entrypoint, references = self.corpus(name)
            self.assertIn(evidence, squash(entrypoint),
                          f"{name}: the entrypoint must accept an observed start as evidence")
            self.assertIn(evidence, squash(references["dispatch-loop.md"]),
                          f"{name}: the dispatch loop lost the observed-start rule")
            self.assertNotIn("`state: ready` ≠ `turnStart`", entrypoint,
                             f"{name}: the duplicated ready rule came back")

    def test_spec_contract_forbids_a_post_pr_backfill_commit(self):
        """The PR number cannot ride the code commit, so bookkeeping leaves the repository.

        A template that asks the worker to "write the PR number back into the Answer" invites
        one push per reading: the number exists only after the PR, and each CI observation then
        becomes another documentation-only commit and another full CI run. An earlier fix
        bounded that to a single post-PR commit; this rule removes it — the number, the body path
        and the reading live in GitHub and the out-of-repo ledger, and the final PR HEAD's CI
        stays the coordinator's check. An explicit user or repository rule that needs the facts
        in the tree still wins, so the template must say so.

        The scenario that teaches the rule is checked alongside the template: a worker reads the
        spec, but the next coordinator learns the rule from the scenario, so reverting either
        one must fail here.
        """
        contract = {
            "orchestra-auto": {
                "required": ("no project commit is added", "out-of-repo ledger",
                             "coordinator's check before the merge", "follow that rule instead",
                             "in-repo Answer therefore reports what was delivered",
                             "explicitly requires those facts in the tree"),
                "forbidden": ("post-PR backfill commit", "single post-PR commit"),
            },
            "orchestra-auto-zh": {
                "required": ("不为携带这些事实追加项目提交", "仓库外台账",
                             "coordinator 在合并前验收", "以该规则为准",
                             "仓库内 Answer 只记录交付了什么与所在分支",
                             "明确要求把该事实写进仓库"),
                "forbidden": ("唯一一笔回填提交", "PR 创建后一笔提交"),
            },
        }
        scenario = "no-post-pr-backfill-commit"
        retired = "single-post-pr-backfill"
        for name, expected in contract.items():
            _entrypoint, references = self.corpus(name)
            text = compact(references["spec-contract.md"])
            for phrase in expected["required"]:
                self.assertIn(compact(phrase), text,
                              f"{name}: the no-backfill rule lost {phrase!r}")
            for phrase in expected["forbidden"]:
                self.assertNotIn(compact(phrase), text,
                                 f"{name}: the post-PR backfill instruction came back as {phrase!r}")

            payload = json.loads((REPO_ROOT / "skills" / name / "evals" / "evals.json").read_text())
            names = [entry["name"] for entry in payload["evals"]]
            self.assertIn(scenario, names, f"{name}: the eval teaching the no-backfill rule is gone")
            self.assertNotIn(retired, names,
                             f"{name}: the retired single-commit scenario came back")
            teaching = next(entry for entry in payload["evals"] if entry["name"] == scenario)
            for field in ("prompt", "expected_output"):
                self.assertNotIn("backfill commit", teaching[field].lower(),
                                 f"{name}: {scenario}'s {field} asks for a backfill commit")
            self.assertEqual(
                [assertion["name"] for assertion in teaching["assertions"]],
                ["no_post_pr_commit", "facts_recorded_out_of_repo",
                 "coordinator_owns_final_ci", "explicit_rule_overrides"],
                f"{name}: {scenario} lost an assertion of the rule",
            )

    def test_dispatch_preflight_separates_the_three_checkpoints(self):
        """A pre-dispatch fact and a post-dispatch fact must not share one gate.

        The defect: one tick sheet listed "the turn really started" and "the worker's first
        report proves isolation" next to "the spec has a test tier". Neither of the first two
        can be observed before the Dispatch exists, so "not every line is green" read as "do
        not dispatch".
        """
        expectations = {
            "orchestra-auto": {
                "verdict": "Only §4.0.1 can block a dispatch",
                "isolation": "isolated worktree",
                "missing_worker": "do not exist before the Dispatch does",
                "teardown": "formatter",
                "exemption": ("bounded blast radius", "spec and the Answer"),
            },
            "orchestra-auto-zh": {
                "verdict": "只有 §4.0.1 能阻止派活",
                "isolation": "隔离 worktree",
                "missing_worker": "Dispatch 存在之前",
                "teardown": "重排",
                "exemption": ("影响面可圈定", "spec 与 Answer"),
            },
        }
        for name, expected in expectations.items():
            _entrypoint, references = self.corpus(name)
            text = references["dispatch-preflight.md"]
            flat = squash(text)
            self.assertIn(expected["verdict"], flat,
                          f"{name}: the 'no dispatch' rule must name the checkpoint it scopes")
            sections = markdown_sections(text)
            before = find_section(sections, "4.0.1", name)
            after = find_section(sections, "4.0.2", name)
            teardown = find_section(sections, "4.0.3", name)
            numbers = [heading.lstrip("# ").split()[0]
                       for heading, _body in sections
                       if heading.lstrip("# ").startswith("4.0.")]
            self.assertEqual(numbers, ["4.0.1", "4.0.2", "4.0.3"],
                             f"{name}: expected exactly the three ordered checkpoints, got {numbers}")
            self.assertNotIn("turnStart", before,
                             f"{name}: the turn-start check cannot be a pre-dispatch line")
            self.assertIn("turnStart", after, f"{name}: the start check is missing after dispatch")
            self.assertIn(expected["isolation"], after,
                          f"{name}: the isolation proof belongs to the post-dispatch section")
            self.assertIn(expected["missing_worker"], squash(after),
                          f"{name}: the post-dispatch section must say why it is not a tick line")
            self.assertIn(expected["teardown"], squash(teardown),
                          f"{name}: the teardown section lost its formatter-noise check")
            for phrase in expected["exemption"]:
                self.assertIn(phrase, squash(before),
                              f"{name}: the review exemption lost its condition {phrase!r}")

    def test_dispatch_preflight_documents_the_receipt_contract(self):
        """A receipt item the reference never describes can drift from the sheet.

        The machine anchors below are the same tokens ``preflight.py`` emits: the twelve
        tick-line ids and classes, the independent ``live-settings`` item, the receipt
        fields, and the three post-receipt observations. The post-receipt observations
        must sit in §4.0.2, not §4.0.1: they are ledger observations, never pre-dispatch
        gates.
        """
        tokens = (
            "kind: preflight",
            "`live-settings`",
            "`guide-freshness`",
            "`cli-app-up`",
            "`cli-command-surface`",
            "`baseline-source-commit`",
            "mechanical",
            "judgment",
            "blocking",
            "overrides",
            "`by`",
            "`reason`",
            "post_dispatch",
            "version-scoped",
            "1.4.244",
            "2026-10-29",
        )
        post_dispatch_tokens = (
            "`review-exemption-recheck`",
            "`dashboard-backfill`",
            "`inbox-health`",
        )
        for name in SKILL_NAMES:
            _entrypoint, references = self.corpus(name)
            text = references["dispatch-preflight.md"]
            flat = squash(text)
            for token in tokens:
                self.assertIn(token, flat, f"{name}: preflight receipt lost {token!r}")
            sections = markdown_sections(text)
            before = find_section(sections, "4.0.1", name)
            after = find_section(sections, "4.0.2", name)
            for token in post_dispatch_tokens:
                self.assertIn(token, after,
                              f"{name}: §4.0.2 lost the post-receipt observation {token!r}")
                self.assertNotIn(token, before,
                                  f"{name}: {token!r} must not be a pre-dispatch gate")
            self.assertEqual(
                len([line for line in before.splitlines() if line.startswith("- [ ]")]),
                12,
                f"{name}: the tick sheet must keep exactly twelve lines",
            )
            self.assertNotIn(
                "`live-settings`", before,
                f"{name}: live-settings is an independent blocking item, not a tick line",
            )

    def test_wayfinder_frontier_is_not_completion(self):
        """An empty frontier has three causes; only one of them finishes the stage."""
        expectations = {
            "orchestra-auto": {
                "states": ("takeable", "in progress", "blocked", "done"),
                "causes": "every ticket assigned, every remaining ticket blocked, or the",
                "not_done": "the frontier is empty but tickets are in progress or blocked",
                "forbidden": "An empty frontier is this stage's endpoint",
            },
            "orchestra-auto-zh": {
                "states": ("可领取", "处理中", "阻塞", "完成"),
                "causes": "全部票已分配",
                "not_done": "frontier 为空但有票在「处理中」或「阻塞」",
                "forbidden": "frontier 为空才是这一阶段的终点",
            },
        }
        for name, expected in expectations.items():
            _entrypoint, references = self.corpus(name)
            text = squash(references["gates-docs-planning.md"])
            for state in expected["states"]:
                self.assertIn(state, text, f"{name}: the frontier states are incomplete: {state!r}")
            self.assertIn(expected["causes"], text,
                          f"{name}: the doc must name the other causes of an empty frontier")
            self.assertIn(expected["not_done"], text,
                          f"{name}: an empty frontier with unfinished tickets must not pass")
            self.assertNotIn(expected["forbidden"], text,
                             f"{name}: an empty frontier must not be the stage's endpoint")

    def test_long_horizon_acceptance_separates_the_four_levels(self):
        """Implemented, integrated, merged and tracker-closed are four different claims."""
        expectations = {
            "orchestra-auto": {
                "levels": ("**Ticket implementation complete**", "**Integration accepted**",
                           "**PR merged**", "**Tickets closed per tracker**"),
                "no_premerge_closure": "requiring every ticket to be closed before the merge",
                "no_false_close": "reporting a ticket as closed while the PR is only open",
                "reconcile": "Reconcile the tickets against the tracker after the merge",
            },
            "orchestra-auto-zh": {
                "levels": ("**票实施完成**", "**集成已验收**", "**PR 已合并**", "**票据按 tracker 关闭**"),
                "no_premerge_closure": "合并前所有票都必须关闭",
                "no_false_close": "PR 还开着就报告票已关闭",
                "reconcile": "合并后按 tracker 对账",
            },
        }
        for name, expected in expectations.items():
            _entrypoint, references = self.corpus(name)
            long_horizon = squash(references["long-horizon-tickets.md"])
            for level in expected["levels"]:
                self.assertIn(level, long_horizon, f"{name}: missing acceptance level {level!r}")
            self.assertIn(expected["no_premerge_closure"], long_horizon,
                          f"{name}: closing every ticket before the merge must be rejected")
            self.assertIn(expected["no_false_close"], long_horizon,
                          f"{name}: a false close-out must be named as forbidden")
            self.assertIn(expected["reconcile"], squash(references["gate-acceptance.md"]),
                          f"{name}: the post-merge tracker reconciliation is missing")

    def test_acceptance_records_document_the_same_rules_in_both_bundles(self):
        """The record, its evidence rules and the four-level extension are one contract.

        The acceptance record is a local schema; if the two language bundles describe it
        differently, a coordinator reading the Chinese bundle would accept what the English
        one refuses. Each side is checked for the same decisions: the record fields, the
        minimum-evidence rules (including "a grep is not a test run" and "worker_done proves
        settlement only"), the multi-PR all-linked-PRs rule, the never-merge terminal and
        the reopen-appends rule, and the pending/provisional treatment of issue #20.
        """
        expectations = {
            "orchestra-auto": {
                "section": "§6.6",
                "fields": ("`accepted_sha`", "`answer_location`", "`decided_by`",
                           "`checks [", "`review {", "`levels`"),
                "no_true_without_evidence": "without an `evidence_ref`",
                "merge": "PR host `MERGED`",
                "tracker": "read-back",
                "start": "`turnStart: observed`",
                "grep": "A `grep` only proves a string exists",
                "worker_done": "`worker_done` proves settlement only",
                "waiver": "the spec **and** the Answer",
                "multi_pr": "**every** linked PR record supports",
                "divergent": "`divergent`",
                "never_merge": "`never_merge` is a terminal state",
                "revert": "**appends** an observation",
                "pending": "keep the affected semantics `provisional`",
                "script": "scripts/acceptance.py",
            },
            "orchestra-auto-zh": {
                "section": "§6.6",
                "fields": ("`accepted_sha`", "`answer_location`", "`decided_by`",
                           "`checks [", "`review {", "`levels`"),
                "no_true_without_evidence": "却没有 `evidence_ref`",
                "merge": "PR host 的 `MERGED`",
                "tracker": "读回",
                "start": "`turnStart: observed`",
                "grep": "只证明字符串存在",
                "worker_done": "`worker_done` 只证明结算",
                "waiver": "spec **和** Answer",
                "multi_pr": "**每个**关联 PR 记录都支持",
                "divergent": "`divergent`",
                "never_merge": "`never_merge` 是 merged 级的终态",
                "revert": "**追加** observation",
                "pending": "`provisional`",
                "script": "scripts/acceptance.py",
            },
        }
        for name, expected in expectations.items():
            _entrypoint, references = self.corpus(name)
            text = squash(references["gate-acceptance.md"])
            self.assertIn(expected["section"], text,
                          f"{name}: gate-acceptance.md has no acceptance-record section")
            for field in expected["fields"]:
                self.assertIn(field, text,
                              f"{name}: the acceptance record loses the field {field!r}")
            for key in ("no_true_without_evidence", "merge", "tracker", "start", "grep",
                        "worker_done", "waiver", "multi_pr", "divergent", "never_merge",
                        "revert", "pending", "script"):
                self.assertIn(expected[key], text,
                              f"{name}: the acceptance rule {key!r} is missing from "
                              "gate-acceptance.md")

    def test_pre_skill_is_chosen_by_stage_not_by_channel(self):
        """A stage chooses its pre-skill; the old channel-to-skill mapping must be gone."""
        expectations = {
            "orchestra-auto": {
                "old_mapping": ("channel B is `implement`, channel C is `grill-with-docs`"),
                "stages": ("`research`", "`to-tickets`", "`implement-spec`"),
                "by_stage": "not by channel",
                "missing": "Missing → report per §9",
            },
            "orchestra-auto-zh": {
                "old_mapping": ("B 通道是 `implement`，C 通道是 `grill-with-docs`"),
                "stages": ("`research`", "`to-tickets`", "`implement-spec`"),
                "by_stage": "按阶段与票型选",
                "missing": "按 §9 带证据报告",
            },
        }
        for name, expected in expectations.items():
            entrypoint, references = self.corpus(name)
            everything = squash("\n".join([entrypoint, *references.values()]))
            self.assertNotIn(expected["old_mapping"], everything,
                             f"{name}: the fixed channel-to-skill mapping is still published")
            loop = squash(references["dispatch-loop.md"])
            self.assertIn(expected["by_stage"], loop,
                          f"{name}: the stage-based rule is missing from the dispatch loop")
            for stage in expected["stages"]:
                self.assertIn(stage, loop, f"{name}: the stage table is missing {stage!r}")
            self.assertIn(expected["missing"], loop,
                          f"{name}: a missing skill must be reported, not invented or installed")

    def test_failed_dispatch_is_classified_before_acting(self):
        """Zero-output setup evidence is a signal to investigate, not proof of a transient fault."""
        expectations = {
            "orchestra-auto": {
                "classes": ("Transient infrastructure", "Setup fault",
                            "Isolation / placement fault", "Artifact already present",
                            "Authority / configuration fault"),
                "not_proof": "does **not** by itself prove",
                "setup": ("**Idempotent**", "**Authorized**", "**Side-effect aware**"),
                "executor": "**Who executes it**",
                "protection": "Reconcile any uncommitted work",
                "old_unconditional": "It passes ⇒ **transient**",
            },
            "orchestra-auto-zh": {
                "classes": ("瞬态基础设施", "setup 故障", "隔离/落点故障", "产物已存在",
                            "权限/配置故障"),
                "not_proof": "它本身**不**证明",
                "setup": ("**幂等**", "**授权**", "**副作用已知**"),
                "executor": "**谁执行**",
                "protection": "任何未提交工作先对账",
                "old_unconditional": "不是配置问题，可以安全重派",
            },
        }
        for name, expected in expectations.items():
            _entrypoint, references = self.corpus(name)
            text = squash(references["failed-dispatch-recovery.md"])
            for label in expected["classes"]:
                self.assertIn(label, text, f"{name}: missing failure class {label!r}")
            self.assertIn(expected["not_proof"], text,
                          f"{name}: the zero-output combination must stop claiming to be proof")
            for condition in expected["setup"]:
                self.assertIn(condition, text, f"{name}: the setup re-run condition {condition!r} is missing")
            self.assertIn(expected["executor"], text,
                          f"{name}: the doc must say who runs setup again")
            self.assertIn(expected["protection"], text,
                          f"{name}: existing artifacts must be protected before release")
            self.assertNotIn(expected["old_unconditional"], text,
                             f"{name}: the old unconditional 'safe to re-dispatch' claim came back")

    def test_merge_danger_has_one_authoritative_definition(self):
        """Two copies of the trigger list drift apart; one authority plus pointers does not."""
        expectations = {
            "orchestra-auto": {
                "trigger": "major dependency upgrade",
                "authority": "single authoritative definition",
            },
            "orchestra-auto-zh": {
                "trigger": "主版本依赖升级",
                "authority": "唯一权威定义",
            },
        }
        for name, expected in expectations.items():
            _entrypoint, references = self.corpus(name)
            holders = [filename for filename, text in references.items()
                       if expected["trigger"] in squash(text)]
            self.assertEqual(holders, ["gate-acceptance.md"],
                             f"{name}: the one-way trigger list must live only in gate-acceptance.md, "
                             f"found in {holders}")
            self.assertIn(expected["authority"], squash(references["gate-acceptance.md"]),
                          f"{name}: the authoritative copy is not labelled as such")
            self.assertIn("gate-acceptance.md", references["long-horizon-tickets.md"],
                          f"{name}: the second copy was removed without leaving a pointer")

    def test_readme_states_the_real_eval_count(self):
        """A stale scenario count is a claim about coverage the package does not have."""
        counts = {}
        for name in SKILL_NAMES:
            payload = json.loads((REPO_ROOT / "skills" / name / "evals" / "evals.json").read_text())
            counts[name] = len(payload["evals"])
        english = (REPO_ROOT / "README.md").read_text()
        chinese = (REPO_ROOT / "README.zh-CN.md").read_text()
        self.assertIn(f"{counts['orchestra-auto']} prompt-level scenarios", english,
                      "README.md must state the number of scenarios that actually ship")
        self.assertIn(f"{counts['orchestra-auto-zh']} 个提示词层面的场景", chinese,
                      "README.zh-CN.md must state the number of scenarios that actually ship")

    def test_start_verification_scenarios_match_the_body(self):
        """The scenarios must test the current rule, conditions included.

        The old case demanded an idle-banner comparison as *the* recovery and read an unchanged
        title as "the preamble is parked": that makes a blind Enter correct. The current rule
        accepts `turnStart: observed`, needs real evidence for a receipt that cannot carry it,
        reads a no-terminal worker through `worker-read`, and allows the Enter only on a
        confirmed parked preamble with no turn running.
        """
        expectations = {
            "orchestra-auto": {
                "scenario": "unverified-start",
                "assertions": ("start_enter_requires_preamble", "start_no_terminal_uses_worker_read",
                               "start_idle_title_not_enough"),
                "observed_scenario": "observed-start-is-evidence",
                "body": ("complete preamble is parked in the composer", "no turn is currently"),
                "fixture_paths": ("worker-named-by-user", "channel-routing",
                                   "worker-slot-follows-setting-not-habit", "read-setting-before-declare"),
            },
            "orchestra-auto-zh": {
                "scenario": "unverified-start",
                "assertions": ("start_enter_requires_preamble", "start_no_terminal_uses_worker_read",
                               "start_idle_title_not_enough"),
                "observed_scenario": "observed-start-is-evidence",
                "body": ("完整 preamble 停在输入框", "当前未运行 turn"),
                "fixture_paths": ("worker-named-by-user", "channel-routing",
                                   "worker-slot-follows-setting-not-habit", "read-setting-before-declare"),
            },
        }
        for name, expected in expectations.items():
            payload = json.loads((REPO_ROOT / "skills" / name / "evals" / "evals.json").read_text())
            scenarios = {entry["name"]: entry for entry in payload["evals"]}
            assertions = {assertion["name"] for assertion in scenarios[expected["scenario"]]["assertions"]}
            for assertion in expected["assertions"]:
                self.assertIn(assertion, assertions,
                              f"{name}: {expected['scenario']} lost the assertion {assertion!r}")
            self.assertIn(expected["observed_scenario"], scenarios,
                          f"{name}: a scenario accepting `turnStart: observed` is missing")
            _entrypoint, references = self.corpus(name)
            body = squash(references["worker-start-and-recovery.md"])
            for phrase in expected["body"]:
                self.assertIn(phrase, body, f"{name}: the start rule lost {phrase!r}")
            for scenario_name in expected["fixture_paths"]:
                prompt = scenarios[scenario_name]["prompt"]
                self.assertIn("PRE_SKILL=", prompt,
                              f"{name}: {scenario_name} demands a path without supplying a fixture")
                self.assertIn("<skills-root>/implement/SKILL.md", prompt,
                              f"{name}: {scenario_name} has no self-contained pre-skill fixture")


class EvalMetadataTests(unittest.TestCase):
    def test_eval_files_parse_and_use_the_expected_shape(self):
        for name, directory in zip(SKILL_NAMES, SKILL_DIRS, strict=True):
            payload = json.loads((directory / "evals" / "evals.json").read_text())
            self.assertEqual(payload.get("skill_name"), name)
            self.assertTrue(payload.get("_note"), f"{name}: evals.json needs its _note caveat")
            evals = payload.get("evals")
            self.assertIsInstance(evals, list)
            self.assertGreaterEqual(len(evals), 10, f"{name}: too few scenarios")
            for entry in evals:
                self.assertIn("id", entry)
                self.assertIn("name", entry)
                self.assertIn("prompt", entry)
                self.assertIn("expected_output", entry)
                self.assertTrue(entry["prompt"].strip(), f"{name}: empty prompt in {entry['name']}")
                self.assertIsInstance(entry.get("files", []), list)
                for assertion in entry.get("assertions", []):
                    self.assertIn("name", assertion)
                    self.assertIn("text", assertion)

    def test_eval_ids_are_unique_and_sequential(self):
        for name, directory in zip(SKILL_NAMES, SKILL_DIRS, strict=True):
            payload = json.loads((directory / "evals" / "evals.json").read_text())
            ids = [entry["id"] for entry in payload["evals"]]
            self.assertEqual(ids, list(range(len(ids))), f"{name}: eval ids must be 0..n-1")

    def test_variants_cover_the_same_scenarios(self):
        english = json.loads((SKILL_DIRS[0] / "evals" / "evals.json").read_text())["evals"]
        chinese = json.loads((SKILL_DIRS[1] / "evals" / "evals.json").read_text())["evals"]
        self.assertEqual(
            [entry["name"] for entry in english],
            [entry["name"] for entry in chinese],
            "the two variants must cover the same scenario names in the same order",
        )
        self.assertEqual(
            [len(entry.get("assertions", [])) for entry in english],
            [len(entry.get("assertions", [])) for entry in chinese],
            "the two variants must carry the same number of assertions per scenario",
        )

    def test_variants_carry_the_same_fixture_values(self):
        """A scenario's fixture block is untranslatable, so it must survive translation.

        Case 12's Chinese prompt once dropped the ``LIVE_DEFAULT_TUI_AGENT`` block its English
        sibling carried while its assertions still demanded that value, which made the scenario
        unanswerable and produced a failure that looked like a skill defect. The prose may differ
        between variants; the fixture values and the upper-case tokens a scenario supplies may not.
        """
        english = json.loads((SKILL_DIRS[0] / "evals" / "evals.json").read_text())["evals"]
        chinese = json.loads((SKILL_DIRS[1] / "evals" / "evals.json").read_text())["evals"]
        fixture = re.compile(r"\b([A-Z][A-Z0-9_]{3,})=([A-Za-z0-9_.\-\[\]]+)")
        token = re.compile(r"\b[A-Z][A-Z0-9_]{4,}\b")
        for en_entry, zh_entry in zip(english, chinese, strict=True):
            self.assertEqual(
                set(fixture.findall(en_entry["prompt"])),
                set(fixture.findall(zh_entry["prompt"])),
                f"{en_entry['name']}: the fixture values differ between variants",
            )
            self.assertEqual(
                set(token.findall(en_entry["prompt"])),
                set(token.findall(zh_entry["prompt"])),
                f"{en_entry['name']}: the variants supply different upper-case tokens",
            )

    def test_eval_scenarios_are_machine_independent(self):
        """No scenario may bake in a home path or a private host detail."""
        forbidden = ("/Users/", "/home/", "profile-state.db", "orca-data.json")
        for name, directory in zip(SKILL_NAMES, SKILL_DIRS, strict=True):
            blob = (directory / "evals" / "evals.json").read_text()
            for needle in forbidden:
                self.assertNotIn(needle, blob, f"{name}: evals.json leaks {needle}")

    def test_eval_caveat_does_not_claim_unrun_tests(self):
        """The caveat must say these are prompt-level checks, not executed proofs."""
        for name, directory in zip(SKILL_NAMES, SKILL_DIRS, strict=True):
            payload = json.loads((directory / "evals" / "evals.json").read_text())
            note = payload["_note"].lower()
            denies_proof = (
                "not end-to-end" in note or "not proof" in note
                or "not a proof" in note or "不是端到端" in payload["_note"]
            )
            names_the_level = (
                "prompt-level" in note or "end-to-end" in note
                or "提示词层面" in payload["_note"]
            )
            self.assertTrue(
                denies_proof and names_the_level,
                f"{name}: the _note must state these are not end-to-end proof",
            )


class RunHealthReferenceTests(unittest.TestCase):
    """The health reference must match the module's state machine and signal table.

    A reference that drifts from ``health.py`` teaches the reader a state machine the code does
    not implement, which is worse than no reference. This checks the contract anchors — the five
    states, the precedence order, the eleven signals and the two authorization edges — not full
    semantic equivalence.
    """

    STATES = ("healthy", "degraded", "blocked", "lost", "unknown")
    # The eleven rows of the signal table, in each language's own wording. The module's own
    # keys are asserted in ``tests/test_health.py``; this file checks the documentation.
    SIGNALS = {
        "orchestra-auto": ("heartbeat", "inbox phase", "unacked batch",
                           "worker-list snapshot", "worker-show observation", "turnStart",
                           "progress freshness", "last effective progress",
                           "reclaimable terminal", "waiter-slot leak", "nested roster"),
        "orchestra-auto-zh": ("heartbeat", "inbox phase", "unacked 批次", "worker-list 快照",
                              "worker-show 观测", "turnStart", "progress freshness",
                              "last effective progress", "reclaimable 终端",
                              "waiter slot 泄漏", "nested roster"),
    }
    ANCHORS = {
        "orchestra-auto": (
            "lost > blocked > unknown > degraded > healthy",
            "never `healthy`",
            "not a health state",
            "health alone never authorizes it",
            "preamble cadence",
            "observation.agentWait",
            "watch_disabled",
        ),
        "orchestra-auto-zh": (
            "lost > blocked > unknown > degraded > healthy",
            "绝不 `healthy`",
            "不是健康状态",
            "从不授权 release",
            "preamble cadence",
            "observation.agentWait",
            "watch_disabled",
        ),
    }

    def test_the_state_machine_and_signal_table_are_documented(self):
        for name in SKILL_NAMES:
            text = (REPO_ROOT / "skills" / name / "references" / "run-health.md").read_text()
            flat = compact(text)
            for state in self.STATES:
                self.assertIn(state, text, f"{name}: run-health.md lost the {state} state")
            for signal in self.SIGNALS[name]:
                self.assertIn(compact(signal), flat,
                              f"{name}: run-health.md lost the {signal!r} signal row")
            for anchor in self.ANCHORS[name]:
                self.assertIn(compact(anchor), flat,
                              f"{name}: run-health.md lost the anchor {anchor!r}")

    def test_the_reference_trigger_is_on_the_first_lines(self):
        for name in SKILL_NAMES:
            head = "\n".join(
                (REPO_ROOT / "skills" / name / "references" / "run-health.md")
                .read_text().splitlines()[:12]
            )
            self.assertTrue(
                any(marker in head for marker in ("When to read", "什么时候读", "何时读取")),
                f"{name}: run-health.md must state its trigger near the top",
            )


class ScriptTests(unittest.TestCase):
    def test_python_scripts_compile(self):
        for name in SKILL_NAMES:
            for filename in PYTHON_SCRIPTS:
                path = REPO_ROOT / "skills" / name / "scripts" / filename
                result = subprocess.run(
                    [sys.executable, "-m", "py_compile", str(path)],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, f"{path}: {result.stderr}")

    def test_every_shipped_script_is_registered(self):
        """A script that is shipped but unregistered would pass the suite untested."""
        for directory in SKILL_DIRS:
            present = {
                path.name for path in (directory / "scripts").iterdir()
                if path.is_file() and path.name != ".DS_Store"
            }
            self.assertEqual(
                present, set(SCRIPT_FILES),
                f"{directory}: shipped scripts and SCRIPT_FILES disagree; register every "
                "script in SCRIPT_FILES and PYTHON_SCRIPTS",
            )

    def test_shell_scripts_pass_a_syntax_check(self):
        for path in REPO_ROOT.rglob("inbox-loop.sh"):
            if any(part in IGNORED_DIRECTORIES for part in path.parts):
                continue
            result = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, f"{path}: {result.stderr}")

    def test_shell_wrapper_forwards_to_the_python_implementation(self):
        for name, directory in zip(SKILL_NAMES, SKILL_DIRS, strict=True):
            wrapper = (directory / "scripts" / "inbox-loop.sh").read_text()
            self.assertIn("inbox-loop.py", wrapper, f"{name}: wrapper does not exec the loop")
            self.assertIn("exec", wrapper, f"{name}: wrapper must exec, not nest a shell")


class LeakScanTests(unittest.TestCase):
    """Patterns that must never appear on the published surface.

    Every pattern here is **generic**. The exact strings from the private original — the specific
    account name, the specific private repository name and the specific credential variable names —
    are deliberately NOT written down in this file, because writing them here would publish the very
    identifiers the scan exists to keep out. Those exact values are checked by
    an environment-supplied list (see ``PRIVATE_IDENTIFIERS`` in the README); this suite
    covers the shapes, and the local scan covers the literal values.

    An exception is granted per **(file, pattern)** pair, never per file: exempting a whole file
    would also excuse it from the path, private-repository, credential and token checks, which is how
    a disclosure scan quietly stops covering the files people edit most.
    """

    FORBIDDEN = (
        ("home-path-macos", re.compile(r"/Users/[A-Za-z0-9._-]+"),
         "absolute macOS home-directory path"),
        ("home-path-linux", re.compile(r"/home/[A-Za-z0-9._-]+"),
         "absolute Linux home-directory path"),
        ("profile-db", re.compile(r"[a-z-]*profile-state\.db"),
         "a specific Orca profile database filename"),
        # Generic credential shapes. Nothing here names a real variable or a real token. The
        # leading segment allows a two-character prefix like `MY`; requiring three or more
        # silently missed realistic names such as `MY_SERVICE_API_KEY`.
        ("credential-var", re.compile(r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)*_(?:TOKEN|SECRET|API_KEY)\b"),
         "a credential-style environment variable name"),
        ("token-literal", re.compile(r"\b(?:sk|ghp|gho|xox[baprs])_[A-Za-z0-9]{16,}\b"),
         "an API token literal"),
    )

    # The scanner defines the generic patterns it searches for, so the source necessarily contains
    # them literally. The exemption is per (file, pattern) pair and covers only those pattern
    # definitions. A test asserts each entry corresponds to a pattern that really is present there,
    # so a stale entry cannot silently no-op.
    ALLOWED = frozenset({
        ("tests/test_package_content.py", "profile-db"),
        ("tests/test_package_content.py", "credential-var"),
    })

    # The scanner's own source is the only file that gets a self-exemption, and it is named here
    # rather than inferred so that widening it is a deliberate edit.
    SCANNER_SOURCE = "tests/test_package_content.py"

    def test_scan_covers_the_published_surface(self):
        scanned = list(tracked_text_files())
        self.assertGreaterEqual(len(scanned), 20, "the scan found suspiciously few files")
        names = {relative for relative, _ in scanned}
        self.assertIn("skills/orchestra-auto/SKILL.md", names)
        self.assertIn("skills/orchestra-auto-zh/SKILL.md", names)

    def test_no_forbidden_pattern_appears_in_published_text(self):
        findings = []
        for relative, path in tracked_text_files():
            text = path.read_text(errors="replace")
            for name, pattern, reason in self.FORBIDDEN:
                if (relative, name) in self.ALLOWED:
                    continue
                for match in pattern.finditer(text):
                    line = text[: match.start()].count("\n") + 1
                    findings.append(f"{relative}:{line} [{reason}]")
        self.assertEqual(findings, [], "forbidden content found:\n" + "\n".join(findings))


    def test_every_allowlisted_file_is_still_scanned_for_other_patterns(self):
        """An exemption for one pattern must not exempt the file from the others."""
        for relative, _pattern in self.ALLOWED:
            text = (REPO_ROOT / relative).read_text(errors="replace")
            for name, pattern, reason in self.FORBIDDEN:
                if (relative, name) in self.ALLOWED:
                    continue
                self.assertIsNone(
                    pattern.search(text),
                    f"{relative}: {reason} is present in a file that is only "
                    f"exempted for another pattern",
                )

    def test_allowlist_names_only_files_that_exist_and_patterns_that_exist(self):
        """A stale entry would silently exempt something nobody looked at."""
        known = {name for name, _pattern, _reason in self.FORBIDDEN}
        for relative, pattern_name in sorted(self.ALLOWED):
            self.assertTrue(
                (REPO_ROOT / relative).is_file(),
                f"the disclosure allowlist names a missing file: {relative}",
            )
            self.assertIn(
                pattern_name, known,
                f"the disclosure allowlist names an unknown pattern: {pattern_name}",
            )

    def test_published_scanner_contains_no_private_identifiers(self):
        """The scanner must not name the private things it is looking for.

        Writing an exact private repository name, account name or credential variable name into a
        published scanner would publish the very identifiers the scan exists to keep out. That is
        why ``FORBIDDEN`` holds generic *shapes* only.

        The exact values cannot be asserted from inside the repository without writing them down, so
        they are supplied out of band: export them (comma separated) in ``PRIVATE_IDENTIFIERS`` and
        the local pre-push scan checks every published file for them. Without that variable the
        check reports a skip rather than a false pass, so it is never mistaken for coverage it does
        not have.
        """
        raw = os.environ.get("PRIVATE_IDENTIFIERS", "")
        identifiers = [part.strip() for part in raw.split(",") if part.strip()]
        if not identifiers:
            self.skipTest(
                "set PRIVATE_IDENTIFIERS=<comma-separated values> to run the exact-value "
                "disclosure check; the published suite can only check generic shapes"
            )
        findings = []
        for relative, path in tracked_text_files():
            text = path.read_text(errors="replace")
            for identifier in identifiers:
                if identifier in text:
                    line = text[: text.index(identifier)].count("\n") + 1
                    # Never echo the identifier itself in the failure message: that would put it
                    # into CI logs, which is the disclosure being checked for.
                    findings.append(f"{relative}:{line} contains a private identifier")
        self.assertEqual(findings, [], "private identifiers found:\n" + "\n".join(findings))

    def test_every_allowlist_entry_is_the_scanner_s_own_source(self):
        """The self-exemption must not spread to any published file.

        This is the invariant that keeps the exception from becoming a hole: a file the repository
        actually ships must be scanned for every pattern, with no exceptions at all. Only the file
        that defines the patterns may exempt itself, and only for those definitions.
        """
        offenders = {
            (relative, pattern_name) for relative, pattern_name in self.ALLOWED
            if relative != self.SCANNER_SOURCE
        }
        self.assertEqual(offenders, set(), (
            "only the scanner's own source may exempt itself; a published file must be scanned "
            "for every pattern"
        ))

    def test_allowlist_only_exempts_patterns_the_scanner_source_contains(self):
        """Each self-exemption must correspond to a pattern actually defined in that file.

        Otherwise the entry would be a silent no-op that later invites reuse for a real file.
        """
        source = (REPO_ROOT / self.SCANNER_SOURCE).read_text()
        for relative, pattern_name in sorted(self.ALLOWED):
            self.assertEqual(relative, self.SCANNER_SOURCE)
            pattern = next(
                pattern for name, pattern, _reason in self.FORBIDDEN if name == pattern_name
            )
            self.assertIsNotNone(
                pattern.search(source),
                f"{pattern_name} is exempted for the scanner source but does not appear there",
            )

    def test_random_uuid_tokens_are_not_reported_as_credentials(self):
        """The inbox script's temp-file UUIDs are tokens, not secrets.

        This is the documented anti-false-positive: a UUID literal in the script must
        match no credential pattern, so the scanner stays trustworthy.
        """
        script = (SKILL_DIRS[0] / "scripts" / "inbox-loop.py").read_text()
        self.assertIn("uuid.uuid4().hex", script, "the script is expected to mint temp tokens")
        for _name, pattern, reason in self.FORBIDDEN:
            self.assertIsNone(
                pattern.search(script),
                f"pattern for {reason} false-positives on the script's UUID tokens",
            )

    def test_generic_credential_pattern_catches_a_realistic_secret_name(self):
        """The generic shape must still be useful, not so vague it never fires.

        Without this, replacing the two literal names with a shape could quietly turn the
        credential check into a no-op — the failure mode where a scanner looks like it works.
        """
        pattern = next(p for n, p, _r in self.FORBIDDEN if n == "credential-var")
        for probe in ("MY_SERVICE_API_KEY", "EXAMPLE_HOOK_TOKEN", "DEPLOY_SECRET"):
            self.assertIsNotNone(pattern.search(probe), f"missed {probe}")
        for benign in ("uuid.uuid4().hex", "deliveries", "token = uuid.uuid4().hex"):
            self.assertIsNone(pattern.search(benign), f"false positive on {benign}")

    def test_evals_and_scripts_are_free_of_forbidden_patterns(self):
        for directory in SKILL_DIRS:
            for path in list(directory.rglob("*.json")) + list(directory.rglob("*.py")):
                text = path.read_text(errors="replace")
                for _name, pattern, reason in self.FORBIDDEN:
                    self.assertIsNone(
                        pattern.search(text),
                        f"{path.relative_to(REPO_ROOT)}: {reason}",
                    )


class RepositoryDocumentTests(unittest.TestCase):
    def test_license_is_mit_with_the_documented_copyright_holder(self):
        license_path = REPO_ROOT / "LICENSE"
        self.assertTrue(license_path.is_file(), "LICENSE is missing")
        text = license_path.read_text()
        self.assertIn("MIT License", text)
        self.assertIn("whutlichao", text)
        self.assertIn("2026", text)
        self.assertIn("Permission is hereby granted, free of charge", text)
        self.assertIn("WITHOUT WARRANTY OF ANY KIND", text)

    def test_readme_documents_both_install_commands_and_the_prerequisite(self):
        readme = (REPO_ROOT / "README.md").read_text()
        self.assertIn("npx skills add whutlichao/orchestra-auto --skill orchestra-auto", readme)
        self.assertIn("npx skills add whutlichao/orchestra-auto --skill orchestra-auto-zh", readme)
        self.assertIn("Orca", readme)

    def test_community_files_exist(self):
        for filename in ("CONTRIBUTING.md", "SECURITY.md"):
            self.assertTrue((REPO_ROOT / filename).is_file(), f"{filename} is missing")

    def test_launch_kit_is_present_in_both_languages(self):
        for filename in ("launch-kit/launch-kit.md", "launch-kit/launch-kit.zh-CN.md"):
            self.assertTrue((REPO_ROOT / filename).is_file(), f"{filename} is missing")

    def test_launch_kit_states_it_does_not_post_on_the_user_s_behalf(self):
        for filename in ("launch-kit/launch-kit.md", "launch-kit/launch-kit.zh-CN.md"):
            text = (REPO_ROOT / filename).read_text()
            lowered = text.lower()
            self.assertTrue(
                "draft" in lowered or "草稿" in text,
                f"{filename}: must state that the copy is a draft",
            )
            self.assertTrue(
                "not post" in lowered or "no posting" in lowered
                or "不代为发帖" in text or "不代发" in text,
                f"{filename}: must state that nothing is posted on the reader's behalf",
            )
            self.assertTrue(
                "sign-in" in lowered or "sign in" in lowered or "login" in lowered
                or "登录" in text,
                f"{filename}: must state that no account is signed in to",
            )


if __name__ == "__main__":
    unittest.main()
