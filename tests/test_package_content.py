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

REFERENCE_FILES = (
    "dispatch-loop.md",
    "dispatch-preflight.md",
    "failed-dispatch-recovery.md",
    "failure-and-reporting.md",
    "gate-acceptance.md",
    "gates-docs-planning.md",
    "inbox-and-waiting.md",
    "long-horizon-tickets.md",
    "reformat-noise-recovery.md",
    "reply-protocol.md",
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
TEXT_SUFFIXES = {".md", ".py", ".sh", ".json", ".jsonc", ".txt", ".yml", ".yaml", ""}
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
        for filename in ("inbox-loop.py", "inbox-loop.sh"):
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
                "`--agent` must be recognized by `worker-start --help`",
            ),
            "orchestra-auto-zh": (
                "--worktree new-top-level",
                "--agent",
                "agent_unconfigured",
                "`state: ready` **不是** `turnStart`",
                "所选 `--agent` 必须被 `worker-start --help` 识别",
            ),
        }
        for name, required_phrases in expectations.items():
            entrypoint = (REPO_ROOT / "skills" / name / "SKILL.md").read_text()
            for phrase in required_phrases:
                self.assertIn(
                    phrase, entrypoint,
                    f"{name}: SKILL.md is missing dispatch guardrail {phrase!r}",
                )

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


class ScriptTests(unittest.TestCase):
    def test_python_scripts_compile(self):
        for path in REPO_ROOT.rglob("inbox-loop.py"):
            if any(part in IGNORED_DIRECTORIES for part in path.parts):
                continue
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", str(path)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, f"{path}: {result.stderr}")

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
