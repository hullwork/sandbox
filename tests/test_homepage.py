"""Contract checks for the zero-build GitHub Pages homepage."""

from __future__ import annotations

from html.parser import HTMLParser
import pathlib
import unittest
from urllib.parse import urlparse


ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE = ROOT / "docs"


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[str] = []
        self.assets: list[str] = []
        self.landmarks: set[str] = set()
        self.images_without_alt: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"] or "")
        if tag in {"header", "nav", "main", "footer"}:
            self.landmarks.add(tag)
        if tag == "a" and values.get("href"):
            self.links.append(values["href"] or "")
        if tag in {"link", "script", "img"}:
            target = values.get("href") or values.get("src")
            if target:
                self.assets.append(target)
        if tag == "img" and "alt" not in values:
            self.images_without_alt.append(values.get("src") or "<unknown>")


def parse(name: str = "index.html") -> tuple[str, _PageParser]:
    text = (SITE / name).read_text(encoding="utf-8")
    parser = _PageParser()
    parser.feed(text)
    return text, parser


class HomepageTests(unittest.TestCase):
    def test_homepage_has_navigation_landmarks_and_skip_link(self) -> None:
        _, page = parse()
        self.assertEqual({"header", "nav", "main", "footer"}, page.landmarks)
        self.assertIn("#main", page.links)
        self.assertEqual([], page.images_without_alt)

    def test_local_assets_exist_and_are_project_path_safe(self) -> None:
        for document in ("index.html", "404.html"):
            _, page = parse(document)
            for target in page.assets:
                parsed = urlparse(target)
                self.assertFalse(parsed.scheme, target)
                self.assertFalse(target.startswith("/"), target)
                self.assertTrue((SITE / target).resolve().is_file(), target)

    def test_homepage_has_no_third_party_runtime_dependency(self) -> None:
        text, page = parse()
        self.assertNotIn("http://", text)
        for target in page.assets:
            self.assertFalse(target.startswith("https://"), target)

    def test_project_claims_match_the_documented_benchmark(self) -> None:
        homepage, _ = parse()
        report = (SITE / "BENCHMARK_REPORT_2026-09-01.md").read_text(encoding="utf-8")
        for value in ("2.497", "35.72", "29.21"):
            self.assertIn(value, homepage)
            self.assertIn(value, report)

    def test_homepage_is_sandbox_only(self) -> None:
        homepage, _ = parse()
        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        # Keep retired product names out of tracked text while still making the
        # homepage test fail if one is assembled into the generated page.
        forbidden = (
            "platform-" + "composition",
            "mi" + "ni-" + "agent",
            "mi" + "ni-" + "sites",
            "hullwork/" + "agent",
        )
        for name in forbidden:
            self.assertNotIn(name, homepage)
        self.assertIn("https://github.com/hullwork/sandbox", homepage)
        self.assertIn('Homepage = "https://hullwork.github.io/sandbox/"', metadata)


if __name__ == "__main__":
    unittest.main()
