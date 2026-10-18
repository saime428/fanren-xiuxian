import contextlib
import io
import unittest
from unittest.mock import AsyncMock, patch

import send_topic_message


class SendTopicMainExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_expected_hard_wait_exit_is_returned_without_task_traceback(self) -> None:
        with (
            patch.object(send_topic_message, "load_dotenv"),
            patch.object(send_topic_message, "env", return_value="180"),
            patch.object(
                send_topic_message,
                "run_once",
                new=AsyncMock(side_effect=send_topic_message.ExpectedFlowExit(75)),
            ),
        ):
            code = await send_topic_message.main()

        self.assertEqual(75, code)

    async def test_deadline_expiration_keeps_nonzero_timeout_exit(self) -> None:
        with (
            patch.object(send_topic_message, "load_dotenv"),
            patch.object(send_topic_message, "env", return_value="180"),
            patch.object(send_topic_message, "run_once", AsyncMock(side_effect=TimeoutError())),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(await send_topic_message.main(), 124)
        self.assertIn("exceeded TG_RUN_MAX_SECONDS=180", output.getvalue())

    async def test_unexpected_error_is_not_converted_to_expected_exit(self) -> None:
        with (
            patch.object(send_topic_message, "load_dotenv"),
            patch.object(send_topic_message, "env", return_value="180"),
            patch.object(send_topic_message, "run_once", AsyncMock(side_effect=ValueError("broken state"))),
            self.assertRaises(ValueError),
        ):
            await send_topic_message.main()


if __name__ == "__main__":
    unittest.main()
