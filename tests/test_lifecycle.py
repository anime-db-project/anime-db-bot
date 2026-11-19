from __future__ import annotations

import signal
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.settings import AppSettings, LifecycleSettings
from scripts import lifecycle, project


class LifecycleConfigurationTests(unittest.TestCase):
    def test_default_policies_preserve_stop_behavior(self) -> None:
        settings = AppSettings.from_env({})
        self.assertEqual(settings.lifecycle.on_failure, "stop")
        self.assertEqual(settings.lifecycle.on_clean_exit, "stop")
        self.assertEqual(settings.lifecycle.restart_max_attempts, 5)

    def test_custom_policies_are_typed_and_bounded(self) -> None:
        settings = AppSettings.from_env(
            {
                "BOT_ON_FAILURE": "RESTART",
                "BOT_ON_CLEAN_EXIT": "restart",
                "BOT_RESTART_MAX_ATTEMPTS": "7",
                "BOT_RESTART_DELAY_SECONDS": "1.5",
                "BOT_RESTART_MAX_DELAY_SECONDS": "12",
                "BOT_RESTART_RESET_AFTER_SECONDS": "180",
                "BOT_SHUTDOWN_TIMEOUT_SECONDS": "20",
            }
        )
        self.assertEqual(settings.lifecycle.on_failure, "restart")
        self.assertEqual(settings.lifecycle.on_clean_exit, "restart")
        self.assertEqual(settings.lifecycle.restart_max_attempts, 7)
        self.assertEqual(settings.lifecycle.restart_delay_seconds, 1.5)
        self.assertEqual(settings.lifecycle.restart_max_delay_seconds, 12)
        self.assertEqual(settings.lifecycle.restart_reset_after_seconds, 180)
        self.assertEqual(settings.lifecycle.shutdown_timeout_seconds, 20)

    def test_invalid_lifecycle_actions_and_values_fail_fast(self) -> None:
        invalid = (
            ("BOT_ON_FAILURE", "invalid"),
            ("BOT_ON_CLEAN_EXIT", "never"),
            ("BOT_RESTART_MAX_ATTEMPTS", "-1"),
            ("BOT_RESTART_MAX_ATTEMPTS", "1000"),
            ("BOT_RESTART_DELAY_SECONDS", "-2"),
            ("BOT_RESTART_MAX_DELAY_SECONDS", "nan"),
            ("BOT_RESTART_RESET_AFTER_SECONDS", "0"),
            ("BOT_SHUTDOWN_TIMEOUT_SECONDS", "0"),
        )
        for key, value in invalid:
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(ValueError, key):
                    AppSettings.from_env({key: value})

    def test_lifecycle_config_is_visible_to_both_launchers(self) -> None:
        self.assertIn("lifecycle.env", project.ENV_FILES)
        template = project.ROOT_DIR / ".env.example.d" / "lifecycle.env"
        self.assertTrue(template.is_file())
        start_sh = (project.ROOT_DIR / "start.sh").read_text(encoding="utf-8")
        self.assertIn("lifecycle.env", start_sh)
        self.assertIn("-m scripts.lifecycle", start_sh)
        self.assertIn("scripts.lifecycle", Path(project.__file__).read_text(encoding="utf-8"))


class LifecyclePolicyTests(unittest.TestCase):
    def test_exit_action_and_restart_budget(self) -> None:
        policy = LifecycleSettings(on_failure="restart", restart_max_attempts=2)
        self.assertTrue(lifecycle.restart_allowed(policy, 1, 0))
        self.assertTrue(lifecycle.restart_allowed(policy, -9, 1))
        self.assertFalse(lifecycle.restart_allowed(policy, 1, 2))
        self.assertFalse(lifecycle.restart_allowed(policy, 0, 0))
        self.assertTrue(
            lifecycle.restart_allowed(
                LifecycleSettings(on_clean_exit="restart"), 0, 0
            )
        )
        self.assertFalse(
            lifecycle.restart_allowed(
                LifecycleSettings(
                    on_failure="restart", restart_max_attempts=0
                ),
                1,
                0,
            )
        )

    def test_exponential_backoff_is_bounded(self) -> None:
        policy = LifecycleSettings(
            restart_delay_seconds=2, restart_max_delay_seconds=9
        )
        self.assertEqual(
            [lifecycle.restart_delay(policy, i) for i in range(1, 6)],
            [2, 4, 8, 9, 9],
        )
        with self.assertRaises(ValueError):
            lifecycle.restart_delay(policy, 0)

    def test_failed_child_restarts_then_stops_after_budget(self) -> None:
        policy = LifecycleSettings(
            on_failure="restart",
            restart_max_attempts=2,
            restart_delay_seconds=0,
        )
        children = []
        for value in (1, 2, 3):
            child = Mock()
            child.pid = 123
            child.wait.return_value = value
            children.append(child)
        with (
            patch("scripts.lifecycle.subprocess.Popen", side_effect=children) as popen,
            patch("scripts.lifecycle.signal.signal"),
            patch("scripts.lifecycle.signal.getsignal", return_value=signal.SIG_DFL),
            patch("scripts.lifecycle.threading.Event") as event_class,
            patch("scripts.lifecycle._info"),
        ):
            event = event_class.return_value
            event.is_set.return_value = False
            self.assertEqual(lifecycle.supervise_bot(policy), 3)
        self.assertEqual(popen.call_count, 3)
        self.assertEqual(event.wait.call_count, 2)
        for call in event.wait.call_args_list:
            self.assertEqual(call.args, (0,))

    def test_clean_exit_can_request_one_restart(self) -> None:
        policy = LifecycleSettings(
            on_clean_exit="restart", restart_delay_seconds=0
        )
        children = []
        for code in (0, 4):
            child = Mock()
            child.pid = 123
            child.wait.return_value = code
            children.append(child)
        with (
            patch("scripts.lifecycle.subprocess.Popen", side_effect=children) as popen,
            patch("scripts.lifecycle.signal.signal"),
            patch("scripts.lifecycle.signal.getsignal", return_value=signal.SIG_DFL),
            patch("scripts.lifecycle.threading.Event") as event_class,
            patch("scripts.lifecycle._info"),
        ):
            event_class.return_value.is_set.return_value = False
            self.assertEqual(lifecycle.supervise_bot(policy), 4)
        self.assertEqual(popen.call_count, 2)

    def test_explicit_shutdown_never_restarts_the_child(self) -> None:
        policy = LifecycleSettings(
            on_failure="restart", on_clean_exit="restart"
        )
        callbacks = {}
        stopped = Mock()
        stopped.is_set.side_effect = [False, True, True, True]
        stopped.wait.return_value = False
        child = Mock()
        child.pid = 20
        child.poll.return_value = None
        child.wait.return_value = 0

        def install(signum, handler):
            callbacks[signum] = handler

        def spawn(*_args, **_kwargs):
            callbacks[signal.SIGTERM](signal.SIGTERM, None)
            return child

        with (
            patch("scripts.lifecycle.subprocess.Popen", side_effect=spawn) as popen,
            patch("scripts.lifecycle.signal.signal", side_effect=install),
            patch("scripts.lifecycle.signal.getsignal", return_value=signal.SIG_DFL),
            patch("scripts.lifecycle.threading.Event", return_value=stopped),
            patch("scripts.lifecycle._info"),
        ):
            self.assertEqual(lifecycle.supervise_bot(policy), 0)
        popen.assert_called_once()
        child.terminate.assert_called_once()
        child.wait.assert_called_once()
        self.assertNotIn("restart", str(stopped.wait.call_args_list))

    def test_unresponsive_child_is_killed_after_shutdown_timeout(self) -> None:
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [
            subprocess.TimeoutExpired(["python"], 2),
            0,
        ]
        lifecycle._stop_child(child, 2)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)


if __name__ == "__main__":
    unittest.main()
