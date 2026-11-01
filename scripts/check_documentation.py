"""Check the repository's bounded Markdown guides without third-party tooling."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import html
from html.parser import HTMLParser
from pathlib import Path
import posixpath
import re
import subprocess
import tomllib
from urllib.parse import unquote, urlsplit


LEGACY = re.compile(r"data(?:[-_ ]checker|checker)", re.IGNORECASE)
HOSTED_GUIDE = re.compile(r"https://github\.com/aelashkin/CheckSI/blob/main/(.+)")
HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:[ \t]+[^<>\n]*)?[ \t]*/?>")
HTML_START = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:[ \t\n]|$)")
CODE = re.compile(r"(`+)(.*?)\1", re.DOTALL)
REFERENCE = re.compile(r"^ {0,3}\[([^\]]+)\]:[ \t]*(.*)$")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")


def _destination(text: str) -> str | None:
    match = re.fullmatch(r'''(?:<([^<>\n]*)>|(\S+?))(?:[ \t]+(?:"[^"\n]*"|'[^'\n]*'|\([^()\n]*\)))?''', text.strip())
    return html.unescape(match[1] if match[1] is not None else match[2]) if match else None


def _label(text: str) -> str:
    return " ".join(text.split()).casefold()


def _without_comments(text: str) -> str:
    return re.sub(r"(`+).*?\1|<!--.*?-->", lambda match: match[0] if match[1] else "",
                  text, flags=re.DOTALL)


def _closing(text: str, start: int, opening: str, closing: str) -> int:
    depth = 1
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if not depth:
                return index
        index += 1
    return -1


class _HTMLTargets(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.targets: list[str] = []
        self.images = self.links = 0

    def handle_starttag(self, tag, attrs):
        self.images += tag == "img"
        for name, value in attrs:
            if name in {"href", "src"} and value is not None:
                self.targets.append(value)
                self.links += name == "href"


@dataclass
class _Inline:
    visible: str = ""
    targets: list[str] = field(default_factory=list)
    images: int = 0
    links: int = 0
    errors: list[str] = field(default_factory=list)


def _inline(text: str, references: dict[str, str] | None = None, depth: int = 0) -> _Inline:
    """Read bounded inline/reference links, nested image labels and single-line HTML."""
    result = _Inline()
    if depth > 12:
        result.errors.append("unsupported link nesting beyond 12 levels")
        result.visible = text
        return result
    validate_references = references is not None
    references = references or {}
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            result.visible += text[index + 1]
            index += 2
            continue
        code = CODE.match(text, index)
        if code:
            result.visible += code[2]
            index = code.end()
            continue
        tag = HTML_TAG.match(text, index)
        if tag:
            parser = _HTMLTargets()
            parser.feed(tag[0])
            result.targets.extend(parser.targets)
            result.images += parser.images
            result.links += parser.links
            index = tag.end()
            continue
        if HTML_START.match(text, index):
            result.errors.append("unsupported multiline or malformed HTML tag")
        image = text.startswith("![", index)
        start = index + image
        if text[start:start + 1] == "[":
            end = _closing(text, start, "[", "]")
            if end >= 0:
                label = text[start + 1:end]
                after = end + 1
                target = None
                explicit = False
                if text[after:after + 1] == "(":
                    close = _closing(text, after, "(", ")")
                    explicit = True
                    if close >= 0:
                        target = _destination(text[after + 1:close]) if close > after + 1 else ""
                        after = close + 1
                    if target is None:
                        result.errors.append("unsupported or malformed inline link destination")
                elif text[after:after + 1] == "[":
                    close = text.find("]", after + 1)
                    explicit = True
                    if close >= 0:
                        target = references.get(_label(text[after + 1:close] or label))
                        after = close + 1
                    if target is None and validate_references:
                        result.errors.append(f"undefined link reference: {label}")
                else:
                    target = references.get(_label(label))
                    if image:
                        explicit = True
                        if target is None and validate_references:
                            result.errors.append(f"undefined image reference: {label}")
                if target is not None or explicit:
                    child = _inline(label, references if validate_references else None, depth + 1)
                    result.targets.extend(child.targets)
                    result.errors.extend(child.errors)
                    result.images += child.images + image
                    result.links += child.links + (not image)
                    if not image:
                        result.visible += child.visible
                    if target is not None:
                        result.targets.append(target)
                    index = after
                    continue
        result.visible += text[index]
        index += 1
    return result


def visible_text(text: str) -> str:
    """Keep visible labels while dropping comments, images and markup."""
    return html.unescape(_inline(_without_comments(text)).visible).replace("`", "").replace("*", "")


def word_count(text: str) -> int:
    """Count whitespace-separated visible tokens containing letters or numbers."""
    return sum(bool(re.search(r"\w", token)) for token in visible_text(text).split())


def heading_anchor(text: str) -> str:
    """Match the GitHub heading syntax used in these guides."""
    text = visible_text(text).lower().strip().rstrip("#").strip()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


@dataclass
class MarkdownPage:
    sections: dict[str, str] = field(default_factory=lambda: {"__intro__": ""})
    raw_sections: dict[str, str] = field(default_factory=lambda: {"__intro__": ""})
    anchors: set[str] = field(default_factory=set)
    links: list[str] = field(default_factory=list)
    table_rows: dict[str, list[str]] = field(default_factory=dict)
    fenced_lines: int = 0
    fences: int = 0
    commands: int = 0
    images: int = 0
    unclosed_fence: bool = False
    empty_headings: list[str] = field(default_factory=list)
    section_links: dict[str, int] = field(default_factory=dict)
    syntax_errors: list[str] = field(default_factory=list)


def parse_markdown(text: str) -> MarkdownPage:
    """Read bounded Markdown headings, links, tables and backtick/tilde fences."""
    page = MarkdownPage()
    lines = _without_comments(text).splitlines()
    references = {}
    definitions = set()
    fence = ""
    for index, line in enumerate(lines):
        marker = FENCE.match(line)
        if marker and (not fence or (marker[1][0] == fence[0] and len(marker[1]) >= len(fence))):
            fence = "" if fence else marker[1]
        if not fence and (definition := REFERENCE.match(line)):
            definitions.add(index)
            target = _destination(definition[2])
            label = _label(definition[1])
            if target is None or label in references:
                page.syntax_errors.append(f"unsupported or duplicate link definition: {definition[1]}")
            else:
                references[label] = target
    section = "__intro__"
    fence = ""
    shell = continued = table = False
    outline: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if index in definitions:
            continue
        marker = FENCE.match(line)
        if marker and (not fence or (marker[1][0] == fence[0] and len(marker[1]) >= len(fence))):
            if fence:
                fence = ""
            else:
                fence = marker[1]
                shell = marker[2].strip() in {"sh", "shell", "bash", "zsh", "powershell", "console"}
                page.fences += 1
                continued = False
            table = False
            continue
        if fence:
            page.raw_sections[section] += line + "\n"
            if line.strip():
                page.fenced_lines += 1
                outline.append((0, "code"))
                if shell and not line.lstrip().startswith("#"):
                    page.commands += not continued
                    continued = line.rstrip().endswith("\\")
            continue
        heading = re.match(r"^\s{0,3}(#{1,6})(?:[ \t]+(.*))?$", line)
        if heading:
            base = heading_anchor(heading[2] or "")
            anchor = base
            count = 0
            while anchor in page.anchors or anchor == "__intro__":
                count += 1
                anchor = f"{base}-{count}"
            page.anchors.add(anchor)
            outline.append((len(heading[1]), anchor))
            if not base:
                page.empty_headings.append("<untitled>")
            if len(heading[1]) == 2:
                section = anchor
                page.sections[section] = ""
                page.raw_sections[section] = ""
        elif word_count(line) or _inline(line, references).images:
            outline.append((0, line))
        page.sections[section] += line + "\n"
        page.raw_sections[section] += line + "\n"
        if line.strip().startswith("|") and line.strip().endswith("|"):
            page.table_rows.setdefault(section, [])
            separator = re.fullmatch(r"[\s|:\-]+", line)
            if table and not separator:
                page.table_rows[section].append(line)
            table = True
        else:
            table = False
    page.unclosed_fence = bool(fence)
    for section, content in page.sections.items():
        inline = _inline(content, references)
        page.links.extend(inline.targets)
        page.images += inline.images
        page.section_links[section] = inline.links
        page.syntax_errors.extend(inline.errors)
    for index, (level, anchor) in enumerate(outline):
        if not level:
            continue
        end = next((i for i in range(index + 1, len(outline))
                    if 0 < outline[i][0] <= level), len(outline))
        if not any(item[0] == 0 for item in outline[index + 1:end]):
            page.empty_headings.append(anchor)
    return page


def _tracked_paths(root: Path) -> set[str]:
    return set(subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True,
                              capture_output=True, timeout=30).stdout.decode("utf-8").split("\0")) - {""}


def _markdown(name: str) -> bool:
    return Path(name).suffix.lower() in {".md", ".markdown"}


def _link_errors(target: str, path: Path, root: Path, tracked: set[str]) -> list[str]:
    hosted = HOSTED_GUIDE.fullmatch(target)
    try:
        parsed = urlsplit(hosted[1] if hosted else target)
    except ValueError:
        return [f"malformed link: {target}"]
    if parsed.scheme or parsed.netloc:
        return []
    base = "" if hosted else path.parent.relative_to(root).as_posix()
    decoded = unquote(parsed.path)
    name = posixpath.normpath(posixpath.join(base, decoded)) if decoded else path.relative_to(root).as_posix()
    if name == ".." or name.startswith(("../", "/")) or "\\" in name:
        return [f"link leaves repository: {target}"]
    destination = root / name
    directory = name == "." or any(item.startswith(name.rstrip("/") + "/") for item in tracked)
    if not destination.resolve().is_relative_to(root):
        return [f"link leaves repository: {target}"]
    if (name not in tracked and not directory) or not destination.exists():
        return [f"missing link: {target}"]
    if parsed.fragment and name in tracked and _markdown(name):
        anchors = parse_markdown(destination.read_text(encoding="utf-8")).anchors
        if unquote(parsed.fragment) not in anchors:
            return [f"missing anchor: {target}"]
    return []


def _mapping(value, location: str, allowed: set[str] | None, required: set[str], errors: list[str]) -> bool:
    if not isinstance(value, dict) or any(not isinstance(key, str) or not key for key in value):
        errors.append(f"budget {location}: expected a table with nonempty string keys")
        return False
    for key in sorted(required - value.keys()):
        errors.append(f"budget {location}: missing {key}")
    if allowed is not None:
        for key in sorted(value.keys() - allowed):
            errors.append(f"budget {location}: unknown key {key}")
    return True


def _ceilings(value: dict, location: str, keys: set[str], errors: list[str]) -> None:
    for key in keys & value.keys():
        if type(value[key]) is not int or value[key] < 0:
            errors.append(f"budget {location}.{key}: expected a nonnegative integer")


def _page_schema(limits, location: str) -> list[str]:
    errors = []
    ceilings = {"max_words", "max_fenced_lines", "max_fences", "max_commands", "max_images", "max_table_row_words"}
    tables = {"sections", "section_constraints", "table_row_limits"}
    if not _mapping(limits, location, ceilings | tables, {"max_words", "max_fenced_lines", "sections"}, errors):
        return errors
    _ceilings(limits, location, ceilings, errors)
    for table in tables & limits.keys():
        value = limits[table]
        if not _mapping(value, f"{location}.{table}", None, set(), errors):
            continue
        if table != "section_constraints":
            _ceilings(value, f"{location}.{table}", set(value), errors)
        else:
            for anchor, constraints in value.items():
                constraint_keys = {"max_paragraphs", "max_links"}
                if _mapping(constraints, f"{location}.{table}.{anchor}", constraint_keys, set(), errors):
                    if not constraints:
                        errors.append(f"budget {location}.{table}.{anchor}: missing ceiling")
                    _ceilings(constraints, f"{location}.{table}.{anchor}", constraint_keys, errors)
    return errors


def _budget_schema(budget) -> list[str]:
    errors = []
    if not _mapping(budget, "root", {"version", "totals", "pages", "legacy_references"},
                    {"version", "totals", "pages"}, errors):
        return errors
    if type(budget.get("version")) is not int or budget.get("version") != 1:
        errors.append("budget root: unsupported documentation budget version")
    totals = budget.get("totals")
    if _mapping(totals, "totals", {"entry_pages", "max_words"}, {"entry_pages", "max_words"}, errors):
        _ceilings(totals, "totals", {"max_words"}, errors)
        entries = totals.get("entry_pages")
        if not isinstance(entries, list) or any(not isinstance(item, str) or not item for item in entries):
            errors.append("budget totals.entry_pages: expected a list of paths")
        elif len(entries) != len(set(entries)):
            errors.append("budget totals.entry_pages: duplicate path")
    pages = budget.get("pages")
    if _mapping(pages, "pages", None, set(), errors):
        for name, limits in pages.items():
            if not _markdown(name) or name.startswith("/") or posixpath.normpath(name) != name or "\\" in name or name.startswith("../"):
                errors.append(f"budget pages.{name}: expected a repository Markdown path")
            errors.extend(_page_schema(limits, f"pages.{name}"))
    legacy = budget.get("legacy_references", [])
    if not isinstance(legacy, list):
        errors.append("budget legacy_references: expected a list")
    else:
        seen = set()
        for item in legacy:
            if _mapping(item, "legacy_references", {"path", "section", "reason"}, {"path", "section", "reason"}, errors):
                if any(not isinstance(item.get(key), str) or not item[key].strip() for key in ("path", "section", "reason")):
                    errors.append("budget legacy_references: path, section and reason must be nonempty strings")
                    continue
                pair = item["path"], item["section"]
                if pair in seen:
                    errors.append(f"budget legacy_references: duplicate path/section {pair}")
                seen.add(pair)
    return errors


def check_page(path: Path, limits: dict, root: Path,
               tracked_paths: list[str] | set[str] | None = None) -> tuple[list[str], MarkdownPage]:
    """Validate a page's complete and per-section limits plus local references."""
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        return ["page leaves repository"], MarkdownPage()
    root = root.resolve()
    path = root / relative
    if not path.resolve().is_relative_to(root):
        return ["page leaves repository"], MarkdownPage()
    errors = _page_schema(limits, path.name)
    if errors:
        return errors, MarkdownPage()
    page = parse_markdown(path.read_text(encoding="utf-8"))
    tracked = _tracked_paths(root) if tracked_paths is None else set(tracked_paths)
    errors.extend(page.syntax_errors)
    actuals = {"words": sum(word_count(text) for text in page.sections.values()),
               "fenced_lines": page.fenced_lines, "fences": page.fences,
               "commands": page.commands, "images": page.images}
    for metric, actual in actuals.items():
        limit = limits.get("max_" + metric)
        if limit is not None and actual > limit:
            errors.append(f"{metric}: {actual} exceeds {limit}")
    if page.unclosed_fence:
        errors.append("unclosed fenced block")
    errors.extend(f"empty heading: {anchor}" for anchor in page.empty_headings)
    sections = limits.get("sections", {})
    for anchor, text in page.sections.items():
        if anchor not in sections:
            errors.append(f"section {anchor}: missing budget")
        elif word_count(text) > sections[anchor]:
            errors.append(f"section {anchor}: {word_count(text)} words exceeds {sections[anchor]}")
    for anchor in sections.keys() - page.sections.keys():
        errors.append(f"section {anchor}: budget has no matching section")
    for anchor, constraints in limits.get("section_constraints", {}).items():
        if anchor not in page.sections:
            errors.append(f"section {anchor}: constraints have no matching section")
            continue
        text = page.sections[anchor]
        paragraphs = [block for block in re.split(r"\n\s*\n", text)
                      if any(word_count(line) and not re.match(r"^\s*(?:#{1,6}\s|\|)", line)
                             for line in block.splitlines())]
        metrics = {"paragraphs": len(paragraphs),
                   "links": page.section_links.get(anchor, 0)}
        for metric, actual in metrics.items():
            limit = constraints.get("max_" + metric)
            if limit is not None and actual > limit:
                errors.append(f"section {anchor}: {actual} {metric} exceeds {limit}")
    for anchor, rows in page.table_rows.items():
        row_limit = limits.get("table_row_limits", {}).get(anchor)
        if row_limit is not None and len(rows) > row_limit:
            errors.append(f"section {anchor}: {len(rows)} table rows exceeds {row_limit}")
        word_limit = limits.get("max_table_row_words")
        if word_limit is not None:
            for row in rows:
                if word_count(row) > word_limit:
                    errors.append(f"section {anchor}: table row has {word_count(row)} words, exceeds {word_limit}")
    for anchor in limits.get("table_row_limits", {}):
        if anchor not in page.table_rows:
            errors.append(f"section {anchor}: table row budget has no matching table")
    for target in page.links:
        errors.extend(_link_errors(target, path, root, tracked))
    return errors, page


def check_repository(root: Path, budget: dict | None = None,
                     tracked_paths: list[str] | None = None) -> tuple[list[str], dict[str, int]]:
    """Check all tracked Markdown; explicit legacy exceptions are section-scoped."""
    root = root.resolve()
    if budget is None:
        budget_path = root / "docs/_budgets.toml"
        if not budget_path.resolve().is_relative_to(root):
            return ["budget file leaves repository"], {}
        try:
            budget = tomllib.loads(budget_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            return [f"budget file: {error}"], {}
    errors = _budget_schema(budget)
    if errors:
        return errors, {}
    if tracked_paths is None:
        tracked_paths = _tracked_paths(root)
    tracked_paths = set(tracked_paths)
    pages = budget["pages"]
    counts = {}
    checked = {}
    for name in sorted(set(tracked_paths) | pages.keys()):
        if not _markdown(name):
            continue
        if name not in pages:
            errors.append(f"{name}: missing page budget")
            continue
        if name not in tracked_paths or not (root / name).is_file():
            errors.append(f"{name}: budget has no matching tracked file")
            continue
        failures, page = check_page(root / name, pages[name], root, tracked_paths)
        checked[name] = page
        errors.extend(f"{name}: {failure}" for failure in failures)
        counts[name] = sum(word_count(text) for text in page.sections.values())
        for section, text in page.raw_sections.items():
            # Repository URLs retain their actual upstream identities.
            text = re.sub(r"https?://[^\s)<>]+", "", text)
            if not LEGACY.search(text):
                continue
            allowed = any(item.get("path") == name and item.get("section") == section
                          and item.get("reason") for item in budget.get("legacy_references", []))
            if not allowed:
                errors.append(f"{name}: section {section}: undocumented legacy name")
    for item in budget.get("legacy_references", []):
        page = checked.get(item["path"])
        if page is None or item["section"] not in page.raw_sections:
            errors.append(f"budget legacy_references: {item['path']} section {item['section']} has no matching section")
    totals = budget["totals"]
    for name in totals["entry_pages"]:
        if name not in counts:
            errors.append(f"entry page missing from checked inventory: {name}")
    total = sum(counts.get(name, 0) for name in totals["entry_pages"])
    if total > totals["max_words"]:
        errors.append(f"entry pages: {total} words exceeds {totals['max_words']}")
    return errors, counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    errors, counts = check_repository(args.root)
    for name, count in sorted(counts.items()):
        print(f"{name}: {count} words")
    for error in errors:
        print(f"ERROR: {error}")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
