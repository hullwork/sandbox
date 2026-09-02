from __future__ import annotations

import pathlib
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ConsoleRuntimeConfigTests(unittest.TestCase):
    def test_console_generates_dns_resolver_and_uses_runtime_upstream(self) -> None:
        nginx = (ROOT / "console/nginx.conf").read_text(encoding="utf-8")
        dockerfile = (ROOT / "console/Dockerfile").read_text(encoding="utf-8")
        entrypoint = (ROOT / "console/entrypoint.sh").read_text(encoding="utf-8")

        self.assertIn("resolver __SANDBOX_RESOLVER__ valid=10s", nginx)
        self.assertIn("proxy_pass http://$sandbox_control_plane_upstream", nginx)
        self.assertNotIn(
            "proxy_pass http://sandbox-control-plane.sandbox-system.svc.cluster.local",
            nginx,
        )
        self.assertIn("/etc/resolv.conf", entrypoint)
        self.assertIn("/tmp/sandbox-console.conf", entrypoint)
        self.assertIn(
            "/etc/nginx/templates/default.conf.template",
            dockerfile,
        )
        self.assertIn('ENTRYPOINT ["/usr/local/bin/sandbox-console-entrypoint"]', dockerfile)
        self.assertIn(
            'CMD ["/usr/sbin/nginx", "-c", "/tmp/sandbox-console-nginx.conf", "-g", "daemon off;"]',
            dockerfile,
        )

    def test_console_deployment_uses_image_entrypoint_and_writable_tmp(self) -> None:
        documents = list(
            yaml.safe_load_all((ROOT / "k8s/console.yaml").read_text(encoding="utf-8"))
        )
        deployment = next(
            document
            for document in documents
            if document and document.get("kind") == "Deployment"
        )
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertNotIn("command", container)
        self.assertNotIn("args", container)
        self.assertIn("tmp", container["volumeMounts"][0]["mountPath"])

    def test_mobile_root_overflow_is_disabled_while_tables_scroll(self) -> None:
        styles = (ROOT / "console/src/styles.css").read_text(encoding="utf-8")

        self.assertIn("html,\nbody {\n  overflow-x: hidden;\n}", styles)
        self.assertIn(".table-scroll {\n  overflow-x: auto;\n}", styles)

    def test_mock_api_key_puts_random_material_before_scope(self) -> None:
        mock = (ROOT / "console/src/mock.ts").read_text(encoding="utf-8")

        self.assertIn(
            "return `sk_${random.slice(0, 9)}_${prefix}_${random.slice(9)}`;",
            mock,
        )


if __name__ == "__main__":
    unittest.main()
