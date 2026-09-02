from __future__ import annotations

import pathlib
import re
import unittest

from sandbox_platform import mcp


ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP_DIRECTORIES = {".git", ".planning", ".venv", "dist", "node_modules"}


class DocumentationTests(unittest.TestCase):
    def test_relative_markdown_links_resolve(self) -> None:
        broken: list[str] = []
        link_pattern = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
        for path in ROOT.rglob("*.md"):
            relative = path.relative_to(ROOT)
            if SKIP_DIRECTORIES.intersection(relative.parts):
                continue
            for raw_target in link_pattern.findall(path.read_text(encoding="utf-8")):
                target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
                target = target.split("#", 1)[0]
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                if not (path.parent / target).resolve().exists():
                    broken.append(f"{relative}: {raw_target}")
        self.assertEqual(broken, [])

    def test_every_decision_record_is_listed_in_its_index(self) -> None:
        """An unlisted ADR is one nobody finds when they are about to undo it.

        Link resolution alone does not catch this: a record can be perfectly
        valid Markdown, and simply absent from the table that is the only way
        anyone browses these.
        """
        directory = ROOT / "docs/adr"
        index = (directory / "README.md").read_text(encoding="utf-8")
        records = sorted(
            path.name for path in directory.glob("*.md") if path.name != "README.md"
        )
        self.assertTrue(records, "no decision records found")
        missing = [name for name in records if f"({name})" not in index]
        self.assertEqual(missing, [], f"not linked from docs/adr/README.md: {missing}")

    def test_readme_lists_the_current_mcp_tool_surface(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        names = [tool["name"] for tool in mcp.TOOLS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 9)
        for name in names:
            self.assertIn(f"`{name}`", readme)

    def test_mcp_source_instructions_use_real_entrypoints(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        module_doc = mcp.__doc__ or ""
        self.assertNotIn("python3 -m sandbox.mcp", readme)
        self.assertNotIn("python3 -m sandbox.mcp", module_doc)
        self.assertIn("sandbox-mcp", readme)
        self.assertIn("sandbox-mcp", module_doc)

    def test_quickstart_uses_the_wheel_sdk_import(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("from sandbox_platform.sandbox_client import Sandbox", readme)
        self.assertNotIn("from sandbox_client import Sandbox", readme)

    def test_readme_names_and_links_the_composition_repository(self) -> None:
        """The pointer to the repository that describes all four must survive.

        This repository documents itself and deliberately does not restate the
        cross-repository picture, so the pointer is the only thing connecting a
        reader to it.  A rename on the other side, or an edit that drops the
        paragraph, would otherwise leave that reader with nothing and produce
        no signal here.

        IMPORTANT: this reads a literal in this repository's own README and
        nothing else.  Whether the link resolves, whether the target repository
        exists, and whether it is public are not observable from inside this
        tree; a gate that implied otherwise would be trusted and would be
        wrong.  Cross-repository link health needs a check that can reach the
        other side.
        """
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        # A rename changes the slug; a reader clicks the URL. Each is asserted
        # with its own terminator - backticks, and the closing parenthesis of
        # the Markdown link - because a bare substring stays green through the
        # rename that matters most: "hullwork/platform-composition-v2" contains
        # "hullwork/platform-composition". Same shape as an unanchored pattern
        # matching inside a longer word.
        self.assertIn("`hullwork/platform-composition`", readme)
        self.assertIn("](https://github.com/hullwork/platform-composition)", readme)

    def test_contributor_clone_path_matches_this_repository(self) -> None:
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn("github.com:<your-github-user>/sandbox.git", contributing)
        self.assertIn("cd sandbox", contributing)
        self.assertNotIn("sandbox-platform.git", contributing)

    def test_documented_vm_disk_matches_installer_default(self) -> None:
        script = (ROOT / "scripts/local-cluster.sh").read_text(encoding="utf-8")
        match = re.search(r'VM_DISK_GIB="\$\{SANDBOX_LOCAL_DISK_GIB:-(\d+)\}"', script)
        self.assertIsNotNone(match)
        size = match.group(1)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        doctor = (ROOT / "scripts/dev-doctor.sh").read_text(encoding="utf-8")
        self.assertIn(f"{size} GiB disk by default", readme)
        self.assertIn(f"its {size} GiB disk", readme)
        self.assertIn(f"a {size} GiB disk", doctor)

    def test_mysql_driver_documentation_matches_the_image(self) -> None:
        dockerfile = (ROOT / "control_plane/Dockerfile").read_text(encoding="utf-8")
        deployment = (ROOT / "docs/DEPLOYMENT.md").read_text(encoding="utf-8")
        self.assertIn('python3 -c "import psycopg, pymysql"', dockerfile)
        self.assertIn("image includes the checksum-locked `PyMySQL` driver", deployment)
        self.assertNotIn("does not include the `pymysql` driver", deployment)

    def test_console_credential_wording_matches_browser_storage(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("it holds no credentials", readme)
        self.assertIn("sessionStorage", readme)
        auth_source = (ROOT / "console/src/auth.ts").read_text(encoding="utf-8")
        self.assertIn("window.sessionStorage.setItem", auth_source)

    def test_dev_token_make_target_does_not_echo_its_recipe(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        target = makefile.split("dev-token:", 1)[1].split("\n\n", 1)[0]
        recipe = next(line for line in target.splitlines() if line.startswith("\t"))
        self.assertTrue(recipe.startswith("\t@"), recipe)


if __name__ == "__main__":
    unittest.main()
