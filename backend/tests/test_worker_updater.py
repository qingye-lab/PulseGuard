from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from backend.app import worker_updater


class WorkerUpdaterTokenTests(unittest.TestCase):
    def test_current_token_prefers_environment_token(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            token_file = Path(temp_dir) / "worker-token"
            token_file.write_text("file-token\n", encoding="utf-8")

            with patch.object(worker_updater, "TOKEN_FILE", token_file), patch.dict(
                os.environ, {"PULSEGUARD_WORKER_TOKEN": "env-token"}, clear=False
            ):
                self.assertEqual(worker_updater._current_token(), "env-token")

    def test_current_token_falls_back_to_token_file(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            token_file = Path(temp_dir) / "worker-token"
            token_file.write_text("file-token\n", encoding="utf-8")

            with patch.object(worker_updater, "TOKEN_FILE", token_file), patch.dict(
                os.environ, {"PULSEGUARD_WORKER_TOKEN": ""}, clear=False
            ):
                self.assertEqual(worker_updater._current_token(), "file-token")

    def test_current_token_returns_empty_when_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            token_file = Path(temp_dir) / "missing-token"

            with patch.object(worker_updater, "TOKEN_FILE", token_file), patch.dict(
                os.environ, {"PULSEGUARD_WORKER_TOKEN": ""}, clear=False
            ):
                self.assertEqual(worker_updater._current_token(), "")


class WorkerUpdaterImageTests(unittest.TestCase):
    def test_target_image_accepts_exact_repository_with_tag_or_digest(self) -> None:
        with patch.object(worker_updater, "ALLOWED_PREFIXES", ("pulseguard-worker",)):
            tagged = worker_updater._target_image({"target_image": "pulseguard-worker:stable"})
            pinned = worker_updater._target_image(
                {"target_image": "pulseguard-worker@sha256:" + ("a" * 64)}
            )

        self.assertEqual(tagged, "pulseguard-worker:stable")
        self.assertTrue(pinned.startswith("pulseguard-worker@sha256:"))

    def test_target_image_rejects_repository_prefix_lookalike(self) -> None:
        with patch.object(worker_updater, "ALLOWED_PREFIXES", ("pulseguard-worker",)):
            with self.assertRaisesRegex(ValueError, "not allowed"):
                worker_updater._target_image(
                    {"target_image": "pulseguard-worker.attacker.example/evil:latest"}
                )

    def test_target_image_matches_registry_port_repository_exactly(self) -> None:
        with patch.object(
            worker_updater,
            "ALLOWED_PREFIXES",
            ("registry.example.com:5000/ops/pulseguard-worker",),
        ):
            accepted = worker_updater._target_image(
                {"target_image": "registry.example.com:5000/ops/pulseguard-worker:v2"}
            )
            with self.assertRaisesRegex(ValueError, "not allowed"):
                worker_updater._target_image(
                    {"target_image": "registry.example.com:5000/ops/pulseguard-worker-extra:v2"}
                )

        self.assertEqual(accepted, "registry.example.com:5000/ops/pulseguard-worker:v2")

    def test_target_image_supports_explicit_namespace_allowlist(self) -> None:
        with patch.object(worker_updater, "ALLOWED_PREFIXES", ("ghcr.io/example/*",)):
            accepted = worker_updater._target_image(
                {"target_image": "ghcr.io/example/pulseguard-worker:latest"}
            )
            with self.assertRaisesRegex(ValueError, "not allowed"):
                worker_updater._target_image(
                    {"target_image": "ghcr.io/example-attacker/pulseguard-worker:latest"}
                )

        self.assertEqual(accepted, "ghcr.io/example/pulseguard-worker:latest")

    def test_ensure_image_available_uses_pulled_image(self) -> None:
        with patch.object(worker_updater, "_run", return_value="pulled") as run:
            worker_updater._ensure_image_available("pulseguard-worker:local")

        run.assert_called_once_with(["docker", "pull", "pulseguard-worker:local"])

    def test_ensure_image_available_accepts_existing_local_image_when_pull_fails(self) -> None:
        with patch.object(
            worker_updater,
            "_run",
            side_effect=[RuntimeError("pull access denied"), "[]"],
        ) as run:
            worker_updater._ensure_image_available("pulseguard-worker:local")

        self.assertEqual(
            run.mock_calls,
            [
                call(["docker", "pull", "pulseguard-worker:local"]),
                call(["docker", "image", "inspect", "pulseguard-worker:local"]),
            ],
        )

    def test_ensure_image_available_raises_when_pull_and_local_lookup_fail(self) -> None:
        with patch.object(
            worker_updater,
            "_run",
            side_effect=[RuntimeError("pull access denied"), RuntimeError("missing image")],
        ):
            with self.assertRaises(RuntimeError):
                worker_updater._ensure_image_available("pulseguard-worker:missing")

    def test_compose_up_hydrates_relay_environment_from_running_containers(self) -> None:
        worker_env = {
            "PULSEGUARD_WORKER_RUNNER_ID": "runner-1",
            "PULSEGUARD_WORKER_TOKEN": "worker-token",
            "PULSEGUARD_WORKER_NAME": "li-win",
        }
        relay_env = {
            "PULSEGUARD_RUNNER_ID": "runner-1",
            "PULSEGUARD_RELAY_URL": "wss://main.example/relay/connect",
            "PULSEGUARD_RELAY_TOKEN": "relay-token",
            "PULSEGUARD_RELAY_FINGERPRINT": "fingerprint",
        }

        def container_environment(container: str) -> dict[str, str]:
            return relay_env if container == "pulseguard-relay-client" else worker_env

        with patch.object(worker_updater, "UPDATE_SERVICES", ("pulseguard-worker", "pulseguard-relay-client")), patch.object(
            worker_updater, "_container_environment", side_effect=container_environment
        ), patch.object(worker_updater, "_run", return_value="updated") as run:
            worker_updater._compose_up("pulseguard-worker:local")

        command = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        self.assertEqual(command[-2:], ["pulseguard-worker", "pulseguard-relay-client"])
        self.assertEqual(env["PULSEGUARD_WORKER_IMAGE"], "pulseguard-worker:local")
        self.assertEqual(env["PULSEGUARD_RUNNER_ID"], "runner-1")
        self.assertEqual(env["PULSEGUARD_RELAY_URL"], "wss://main.example/relay/connect")
        self.assertEqual(env["PULSEGUARD_RELAY_TOKEN"], "relay-token")

    def test_status_reports_current_image_and_update_availability(self) -> None:
        with patch.object(worker_updater, "DEFAULT_IMAGE", "pulseguard-worker:local"), patch.object(
            worker_updater, "_current_container_image", return_value="pulseguard-worker:old"
        ), patch.object(worker_updater, "_current_container_image_id", return_value="sha256:old"), patch.object(
            worker_updater, "_image_id", return_value="sha256:new"
        ), patch.object(worker_updater, "UPDATE_SERVICES", ("pulseguard-worker", "pulseguard-relay-client")):
            status = worker_updater._status_with_runtime({"status": "idle", "message": "ready"})

        self.assertEqual(status["current_image"], "pulseguard-worker:old")
        self.assertEqual(status["target_image"], "pulseguard-worker:local")
        self.assertTrue(status["update_available"])
        self.assertEqual(status["update_services"], ["pulseguard-worker", "pulseguard-relay-client"])

    def test_status_detects_same_tag_with_new_image_id_as_update_available(self) -> None:
        with patch.object(worker_updater, "DEFAULT_IMAGE", "pulseguard-worker:local"), patch.object(
            worker_updater, "_current_container_image", return_value="pulseguard-worker:local"
        ), patch.object(worker_updater, "_current_container_image_id", return_value="sha256:old"), patch.object(
            worker_updater, "_image_id", return_value="sha256:new"
        ):
            status = worker_updater._status_with_runtime({"status": "idle", "message": "ready"})

        self.assertTrue(status["update_available"])

    def test_status_falls_back_to_image_reference_when_image_ids_are_unavailable(self) -> None:
        with patch.object(worker_updater, "DEFAULT_IMAGE", "pulseguard-worker:new"), patch.object(
            worker_updater, "_current_container_image", return_value="pulseguard-worker:old"
        ), patch.object(worker_updater, "_current_container_image_id", return_value=""), patch.object(worker_updater, "_image_id", return_value=""):
            status = worker_updater._status_with_runtime({"status": "idle", "message": "ready"})

        self.assertTrue(status["update_available"])


if __name__ == "__main__":
    unittest.main()
