"""Bound documentation and keep its local references usable."""

from pathlib import Path
import copy
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_documentation import check_page, check_repository, parse_markdown, word_count


class MarkdownContractTests(unittest.TestCase):
    def test_chemical_comparators_arrows_and_quotes_cannot_hide_words(self):
        prose = "δ < 0.5 ppm\n" + "detail " * 200 + "\na -> b\n> quoted text"
        self.assertEqual(word_count(prose), 207)
        self.assertEqual(word_count("<em>Visible words</em> <img src='logo.png'>"), 2)

    def test_colliding_generated_anchors_preserve_every_section(self):
        page = parse_markdown("# Guide\n" + "".join(
            f"## {heading}\nContent {index}.\n" for index, heading in enumerate(
                ["Run", "Run", "Run 1", "Run-1", "Run 1", "Run", "Run-2"])))
        anchors = ["run", "run-1", "run-1-1", "run-1-2", "run-1-3", "run-2", "run-2-1"]
        self.assertEqual(list(page.sections), ["__intro__", *anchors])
        for index, anchor in enumerate(anchors):
            self.assertIn(f"Content {index}.", page.sections[anchor])

    def test_all_supported_link_forms_and_images_are_checked_and_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "README.md"
            path.write_text("# Guide\n## Links\n[![ci](logo.png)](outer.md)\n"
                            "[Full][guide] [Collapsed][] [Shortcut]\n"
                            "![ref image][picture] <a href='html.md'>HTML</a> <img src='html.png'>\n"
                            "[guide]: full.md\n[Collapsed]: collapsed.md\n[Shortcut]: shortcut.md\n"
                            "[picture]: reference.png\n", encoding="utf-8")
            limits = {"max_words": 100, "max_fenced_lines": 0, "max_images": 2,
                      "sections": {"__intro__": 10, "links": 40},
                      "section_constraints": {"links": {"max_links": 4}}}
            errors, page = check_page(path, limits, root, tracked_paths=["README.md"])
            self.assertEqual(page.images, 3)
            self.assertEqual(set(page.links), {"logo.png", "outer.md", "full.md", "collapsed.md",
                                                "shortcut.md", "reference.png", "html.md", "html.png"})
            for target in page.links:
                self.assertTrue(any("missing link" in error and target in error for error in errors), errors)
            self.assertTrue(any("3 exceeds 2" in error for error in errors), errors)
            self.assertTrue(any("5 links exceeds 4" in error for error in errors), errors)

    def test_reference_destinations_and_inline_code_do_not_count_as_prose_or_links(self):
        page = parse_markdown("# Guide\n[Two words][guide] and ![hidden][pic].\n"
                              "`[literal](missing.md)`\n[guide]: https://example.org/long/path\n"
                              "[pic]: image.png\n")
        self.assertEqual(word_count(page.sections["__intro__"]), 5)
        self.assertEqual(page.links, ["https://example.org/long/path", "image.png"])
        self.assertEqual(page.images, 1)

    def test_multiline_link_labels_are_checked_and_multiline_html_fails_explicitly(self):
        page = parse_markdown("# Guide\n[Two\nwords](missing.md)\n<a\nhref='other.md'>Label</a>\n")
        self.assertIn("missing.md", page.links)
        self.assertTrue(any("HTML" in error for error in page.syntax_errors), page.syntax_errors)

    def test_inline_code_keeps_literal_comments_and_image_only_sections_have_content(self):
        self.assertEqual(word_count("`<!-- two visible words -->`"), 3)
        page = parse_markdown("# Guide\n## Image\n![diagram]\n[diagram]: diagram.png\n")
        self.assertNotIn("image", page.empty_headings)
        self.assertEqual(page.images, 1)
        self.assertEqual(word_count(page.sections["image"]), 1)

    def test_empty_tables_have_valid_zero_row_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "README.md"
            path.write_text("# Guide\n| Name | Result |\n|---|---|\n", encoding="utf-8")
            limits = {"max_words": 20, "max_fenced_lines": 0, "sections": {"__intro__": 20},
                      "table_row_limits": {"__intro__": 0}}
            errors, _ = check_page(path, limits, root, ["README.md"])
            self.assertEqual(errors, [])

    def test_links_use_exact_tracked_paths_including_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "docs/Other.MD").write_text("# Other\n## Present\nText.\n", encoding="utf-8")
            (root / "untracked.md").write_text("# Local\n", encoding="utf-8")
            path = root / "README.md"
            path.write_text("# Guide\n[Case](docs/other.md) [Local](untracked.md) "
                            "[Directory](docs/) [Good](docs/Other.MD#present) "
                            "[Bad](docs/Other.MD#absent)\n", encoding="utf-8")
            errors, _ = check_page(path, {"max_words": 100, "max_fenced_lines": 0,
                                           "sections": {"__intro__": 100}}, root,
                                   tracked_paths=["README.md", "docs/Other.MD"])
            self.assertEqual(len(errors), 3, errors)
            for target in ("docs/other.md", "untracked.md", "docs/Other.MD#absent"):
                self.assertTrue(any(target in error for error in errors), errors)

    def test_only_current_hosted_repository_guides_are_local(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "README.md"
            path.write_text("# Guide\n[Current](https://github.com/aelashkin/CheckSI/blob/main/missing.md) "
                            "[Other](https://github.com/example/other/blob/main/other.md)\n", encoding="utf-8")
            errors, _ = check_page(path, {"max_words": 100, "max_fenced_lines": 0,
                                           "sections": {"__intro__": 100}}, root,
                                   tracked_paths=["README.md"])
            self.assertEqual(len(errors), 1, errors)
            self.assertIn("missing.md", errors[0])

    def test_git_inventory_is_one_mockable_external_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("# Guide\nText. [Local](ignored.md)\n", encoding="utf-8")
            (root / "ignored.md").write_text("# Ignored\n", encoding="utf-8")
            with patch("scripts.check_documentation.subprocess.run") as git:
                git.return_value.stdout = b"README.md\0"
                errors, _ = check_repository(root, self.budget())
            self.assertEqual(git.call_count, 1)
            self.assertTrue(any("missing link: ignored.md" in error for error in errors), errors)

    @staticmethod
    def budget():
        return {"version": 1, "totals": {"entry_pages": ["README.md"], "max_words": 100},
                "pages": {"README.md": {"max_words": 100, "max_fenced_lines": 0,
                                         "sections": {"__intro__": 100}}}}

    def test_invalid_budget_shapes_types_and_unknown_keys_are_errors(self):
        cases = [((), "typo", 1), (("totals",), "max_word", 1),
                 (("pages", "README.md"), "max_word", 1),
                 (("pages", "README.md"), "max_words", True),
                 (("pages", "README.md", "sections"), "__intro__", -1),
                 (("pages", "README.md"), "section_constraints", {"__intro__": {"max_link": 1}}),
                 (("pages", "README.md"), "table_row_limits", {"__intro__": False}),
                 ((), "pages", []), ((), "version", True),
                 (("totals",), "entry_pages", ["README.md", "README.md"]),
                 ((), "legacy_references", [{"path": "README.md", "section": "__intro__", "reason": 2}])]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("# Guide\nText.\n", encoding="utf-8")
            for nesting, key, value in cases:
                with self.subTest(nesting=nesting, key=key, value=value):
                    budget = copy.deepcopy(self.budget())
                    container = budget
                    for part in nesting:
                        container = container[part]
                    container[key] = value
                    errors, _ = check_repository(root, budget, ["README.md"])
                    self.assertTrue(errors)
                    self.assertTrue(any("budget" in error for error in errors), errors)
            for nesting, key in [((), "totals"), (("totals",), "max_words"),
                                 (("pages", "README.md"), "max_words"),
                                 (("pages", "README.md"), "max_fenced_lines"),
                                 (("pages", "README.md"), "sections")]:
                with self.subTest(missing=key):
                    budget = copy.deepcopy(self.budget())
                    container = budget
                    for part in nesting:
                        container = container[part]
                    del container[key]
                    errors, _ = check_repository(root, budget, ["README.md"])
                    self.assertTrue(errors)

    def test_stale_and_duplicate_budget_entries_are_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("# Guide\nText.\n", encoding="utf-8")
            budget = self.budget()
            budget["pages"]["README.md"]["table_row_limits"] = {"removed": 2, "__intro__": 2}
            item = {"path": "README.md", "section": "removed", "reason": "Historical name."}
            budget["legacy_references"] = [item, dict(item),
                                            {"path": "removed.md", "section": "__intro__", "reason": "Gone."}]
            errors, _ = check_repository(root, budget, ["README.md"])
            self.assertTrue(any("duplicate" in error for error in errors), errors)
            del budget["legacy_references"][1]
            errors, _ = check_repository(root, budget, ["README.md"])
            for expected in ("table", "removed", "removed.md"):
                self.assertTrue(any(expected in error for error in errors), errors)

    def test_markdown_suffixes_require_budgets_case_insensitively(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ["NOTES.MD", "guide.markdown", "mixed.MarkDown"]
            for name in names:
                (root / name).write_text("# Guide\nText.\n", encoding="utf-8")
            errors, _ = check_repository(root, {"version": 1, "pages": {},
                                                "totals": {"entry_pages": [], "max_words": 0}}, names)
            for name in names:
                self.assertTrue(any(name in error and "missing page budget" in error for error in errors), errors)

    def test_words_count_labels_and_inline_code_without_destinations(self):
        text = "# Guide\n[Two words](https://example.org/very/long/path) and `one_key` value.\n"
        self.assertEqual(word_count(text), 6)
        self.assertEqual(word_count("![badge](https://img.shields.io/example) <!-- private note -->"), 0)

    def test_inline_code_placeholders_are_visible_words(self):
        self.assertEqual(word_count("`tool --source <pinned-directory> --receipt <download-receipt.json>`"), 5)
        self.assertEqual(word_count("`<one two three four five six seven eight nine ten>`"), 10)

    def test_empty_nested_heading_fails_even_when_its_parent_has_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "README.md"
            path.write_text("# Guide\n## Run\nUseful text.\n### Empty\n", encoding="utf-8")
            errors, _ = check_page(path, {"max_words": 100, "max_fenced_lines": 0,
                                          "sections": {"__intro__": 10, "run": 20}}, root, ["README.md"])
            self.assertTrue(any("empty" in error and "heading" in error for error in errors), errors)

    def test_fences_keep_code_out_of_words_sections_and_links(self):
        page = parse_markdown(
            "# Guide\nIntro.\n## Run\nUse this.\n```sh\n"
            "# not a heading\nuv run app \\\n  --flag\n\n"
            "[not a link](missing.md)\n```\n## Run\nAgain.\n"
        )
        self.assertEqual(set(page.sections), {"__intro__", "run", "run-1"})
        self.assertEqual(page.fenced_lines, 4)
        self.assertEqual(page.commands, 2)
        self.assertEqual(page.links, [])
        self.assertEqual(word_count(page.sections["run"]), 3)

    def test_nested_headings_belong_to_their_parent_section(self):
        page = parse_markdown("# Guide\n## One\nText.\n### Details\nMore.\n## Two\nDone.\n")
        self.assertIn("Details", page.sections["one"])
        self.assertEqual(page.anchors, {"guide", "one", "details", "two"})

    def test_table_caps_count_body_rows_without_separators_or_headers(self):
        page = parse_markdown("# Guide\n## Checks\n| Area | Result |\n|---|---|\n| NMR | Counts |\n")
        self.assertEqual(page.table_rows["checks"], ["| NMR | Counts |"])

    def test_section_limits_and_local_anchors_fail_at_the_readers_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "README.md"
            path.write_text("# Guide\n## Run\nToo many words. [Missing](other.md#absent)\n", encoding="utf-8")
            (root / "other.md").write_text("# Other\n## Present\n", encoding="utf-8")
            errors, _ = check_page(path, {"max_words": 100, "max_fenced_lines": 0,
                                          "sections": {"__intro__": 10, "run": 2}}, root, ["README.md", "other.md"])
            self.assertTrue(any("run" in error and "words" in error for error in errors), errors)
            self.assertTrue(any("absent" in error for error in errors), errors)

    def test_missing_sections_and_fenced_limits_cannot_hide_extra_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "README.md"
            path.write_text("# Guide\n## Extra\n```text\nHidden detail\n```\n", encoding="utf-8")
            errors, _ = check_page(path, {"max_words": 100, "max_fenced_lines": 0,
                                          "sections": {"__intro__": 10, "run": 10}}, root, ["README.md"])
            for expected in ("extra", "run", "fenced"):
                self.assertTrue(any(expected in error for error in errors), errors)

    def test_section_structure_rejects_extra_paragraphs_and_link_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "README.md"
            path.write_text("# Guide\n## Settings\nFirst paragraph.\n\nSecond paragraph.\n"
                            "## Reading\n[One](README.md) · [Two](README.md)\n", encoding="utf-8")
            limits = {"max_words": 100, "max_fenced_lines": 0,
                      "sections": {"__intro__": 10, "settings": 20, "reading": 20},
                      "section_constraints": {"settings": {"max_paragraphs": 1},
                                              "reading": {"max_links": 1}}}
            errors, _ = check_page(path, limits, root, ["README.md"])
            self.assertTrue(any("settings" in error and "paragraphs" in error for error in errors), errors)
            self.assertTrue(any("reading" in error and "links" in error for error in errors), errors)

    def test_legacy_reference_exception_is_confined_to_its_named_section(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "README.md"
            old = "data" + "_checker"
            path.write_text(f"# CheckSI\n## Migration\nPreviously `{old}`.\n## Current\nUse `{old}`.\n", encoding="utf-8")
            budget = {"version": 1, "totals": {"entry_pages": ["README.md"], "max_words": 100},
                      "pages": {"README.md": {"max_words": 100, "max_fenced_lines": 0,
                                              "sections": {"__intro__": 10, "migration": 20, "current": 20}}},
                      "legacy_references": [{"path": "README.md", "section": "migration",
                                             "reason": "Identify the replaced import for migration."}]}
            errors, _ = check_repository(root, budget, ["README.md"])
            self.assertEqual(sum("legacy" in error for error in errors), 1, errors)
            self.assertTrue(any("current" in error for error in errors), errors)

    def test_new_markdown_requires_a_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "new.md").write_text("# New\n", encoding="utf-8")
            errors, _ = check_repository(root, {"version": 1, "pages": {},
                                                "totals": {"entry_pages": [], "max_words": 0}}, ["new.md"])
            self.assertTrue(any("new.md" in error and "budget" in error for error in errors), errors)


class RepositoryDocumentationTests(unittest.TestCase):
    def test_documentation_contract(self):
        errors, _ = check_repository(Path(__file__).resolve().parents[1])
        self.assertEqual(errors, [], "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
