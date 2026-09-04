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
        """The tool names are documented somewhere a reader will find them.

        The list moved into docs/USAGE.md with the rest of the surfaces, so
        both files count as the documented surface.
        """
        readme = ((ROOT / "README.md").read_text(encoding="utf-8")
                  + (ROOT / "docs/USAGE.md").read_text(encoding="utf-8"))
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
        """The worked example imports from the published distribution.

        `sandbox_client` on its own is the in-tree module name and does not
        exist in an installed wheel, so a reader copying it gets ImportError.
        The example moved from the README into docs/USAGE.md; both are checked
        so that neither can carry the wrong form.
        """
        wanted = "from sandbox_platform.sandbox_client import Sandbox"
        wrong = "from sandbox_client import Sandbox"
        sources = {
            name: (ROOT / name).read_text(encoding="utf-8")
            for name in ("README.md", "docs/USAGE.md")
        }
        self.assertTrue(
            any(wanted in text for text in sources.values()),
            f"no worked SDK example in any of {sorted(sources)}",
        )
        for name, text in sources.items():
            self.assertNotIn(wrong, text, f"{name} uses the in-tree import")

    def test_docs_do_not_point_at_sibling_repositories(self) -> None:
        """This repository is released on its own and documents itself.

        It once carried a pointer to a composition repository that described
        four repositories together. That is no longer the model: nothing here
        depends on another repository, and other products integrate with this
        one as an external tenant. A link to a sibling would send a reader to
        something private or gone, so README and docs/ may name none of them.
        Test sources are not covered: a comment explaining history is fine.
        """
        forbidden = (
            "platform-composition",
            "hullwork/agent",
            "hullwork/site",
            "hullwork/infra",
        )
        offenders: list[str] = []
        for path in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]:
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {needle}")
        self.assertEqual(offenders, [])

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

    def test_readme_closes_the_local_console_login_loop(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        heading = readme.index("### Open the Console")
        section = readme[heading:readme.index("\n## ", heading + 4)]
        self.assertIn("make console-forward", section)
        self.assertIn("http://127.0.0.1:18081", section)
        self.assertIn("make --no-print-directory dev-token", section)
        self.assertIn("API key", section)
        self.assertIn("administrator-equivalent", section)

    def test_dev_token_make_target_does_not_echo_its_recipe(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        target = makefile.split("dev-token:", 1)[1].split("\n\n", 1)[0]
        recipe = next(line for line in target.splitlines() if line.startswith("\t"))
        self.assertTrue(recipe.startswith("\t@"), recipe)


if __name__ == "__main__":
    unittest.main()
