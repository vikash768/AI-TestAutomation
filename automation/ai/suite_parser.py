"""
Test Suite Parser.

Reads a project-specific `.testsuite.md` file and extracts structured
testing context: pages, testing categories, instructions, credentials,
and element locator hints.

The parser converts the markdown instruction document into a data structure
that the test generator and planner can consume.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class LocatorHint:
    """A hint for locating an element, extracted from the testsuite."""

    element_name: str
    strategies: list[str] = field(default_factory=list)


@dataclass
class TestInstruction:
    """A single testing instruction within a category."""

    instruction: str
    details: str = ""


@dataclass
class TestCategory:
    """A testing category within a page (e.g., Authentication, Validation)."""

    category_id: str
    category_name: str
    description: str = ""
    instructions: list[TestInstruction] = field(default_factory=list)


@dataclass
class PageContext:
    """Parsed context for a single page of the application."""

    page_name: str
    page_number: int
    url_pattern: str = ""
    description: str = ""
    preconditions: list[str] = field(default_factory=list)
    elements: list[LocatorHint] = field(default_factory=list)
    categories: list[TestCategory] = field(default_factory=list)


@dataclass
class SuiteContext:
    """Complete parsed context from a testsuite.md file."""

    application_name: str = ""
    base_url_var: str = "${BASE_URL}"
    credentials: dict[str, str] = field(default_factory=dict)
    pages: list[PageContext] = field(default_factory=list)
    global_rules: list[str] = field(default_factory=list)
    raw_content: str = ""
    suite_path: str = ""


class SuiteParser:
    """Parses a testsuite.md file into a SuiteContext."""

    def parse(self, suite_path: str) -> SuiteContext:
        """Parse the given suite file and return structured context.

        Args:
            suite_path: Path to the .testsuite.md file.

        Returns:
            A SuiteContext containing all parsed pages and categories.
        """
        path = Path(suite_path)
        if not path.exists():
            raise FileNotFoundError(f"Suite file not found: {suite_path}")

        content = path.read_text(encoding="utf-8")
        logger.info("Parsing suite file: %s (%d bytes)", suite_path, len(content))

        ctx = SuiteContext(
            raw_content=content,
            suite_path=str(path.resolve()),
        )

        self._parse_header(content, ctx)
        self._parse_credentials(content, ctx)
        self._parse_pages(content, ctx)
        self._parse_global_rules(content, ctx)

        logger.info(
            "Parsed suite: app=%s, pages=%d, total_categories=%d",
            ctx.application_name,
            len(ctx.pages),
            sum(len(p.categories) for p in ctx.pages),
        )
        return ctx

    def _parse_header(self, content: str, ctx: SuiteContext) -> None:
        """Extract application name from the document header."""
        # Look for "# <AppName> — AI Testing Instruction Suite" pattern
        header_match = re.search(r"^#\s+(.+?)(?:\s*[—–-]\s*AI Testing)", content, re.MULTILINE)
        if header_match:
            ctx.application_name = header_match.group(1).strip()
        else:
            # Fallback: first H1 heading
            h1_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
            if h1_match:
                ctx.application_name = h1_match.group(1).strip()

    def _parse_credentials(self, content: str, ctx: SuiteContext) -> None:
        """Extract credential variable mappings if they exist."""
        pass # Credentials are now strictly in env vars

    def _parse_pages(self, content: str, ctx: SuiteContext) -> None:
        """Extract page sections and their testing categories."""
        # Match "# 6. Page 1 — Login" pattern or "# 12. Page 7 — Projects & Tasks"
        page_pattern = re.compile(
            r"^#\s+\d+\.\s+Page\s+(\d+)\s+[—–-]\s+(.+?)$", re.MULTILINE | re.IGNORECASE
        )
        page_matches = list(page_pattern.finditer(content))

        for i, match in enumerate(page_matches):
            page_num = int(match.group(1))
            page_name = match.group(2).strip()

            start = match.end()
            end = page_matches[i + 1].start() if i + 1 < len(page_matches) else len(content)
            page_content = content[start:end]

            page = PageContext(
                page_name=page_name,
                page_number=page_num,
            )

            self._parse_page_url(page_content, page)
            self._parse_page_categories(page_content, page)

            ctx.pages.append(page)

    def _parse_page_url(self, content: str, page: PageContext) -> None:
        """Extract URL pattern for a page."""
        url_match = re.search(
            r"\*\*Route:\*\*\s*`([^`]+)`", content
        )
        if url_match:
            page.url_pattern = url_match.group(1).strip()

    def _parse_page_categories(self, content: str, page: PageContext) -> None:
        """Extract testing categories and their instructions."""
        # Match "## 6.1 Authentication"
        cat_pattern = re.compile(
            r"^##\s+(\d+\.\d+)\s+(.+?)$", re.MULTILINE
        )
        cat_matches = list(cat_pattern.finditer(content))

        for i, match in enumerate(cat_matches):
            cat_id = match.group(1)
            cat_name = match.group(2).strip()

            start = match.end()
            end = cat_matches[i + 1].start() if i + 1 < len(cat_matches) else len(content)
            cat_content = content[start:end]

            category = TestCategory(
                category_id=cat_id,
                category_name=cat_name,
            )

            # Look for bullet point list items as instructions: "- something to test"
            instr_pattern = re.compile(
                r"^-\s+(.+)$", re.MULTILINE
            )
            for instr_match in instr_pattern.finditer(cat_content):
                instruction = TestInstruction(
                    instruction=instr_match.group(1).strip()
                )
                category.instructions.append(instruction)

            # Include the raw content as description so the AI model can read the context
            category.description = cat_content.strip()

            page.categories.append(category)

    def _parse_global_rules(self, content: str, ctx: SuiteContext) -> None:
        """Extract global test execution rules."""
        pass # Simplified for the new format
