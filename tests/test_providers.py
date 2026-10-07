import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from code_agent.config import Config
from code_agent.errors import ProviderError
from code_agent.providers import LLMProvider, Reply, ToolCall


class ProviderTests(unittest.TestCase):
    def provider(self, style="chat_completions", retries=0):
        return LLMProvider(Config(api_key="test-key", model="test-model", api_style=style,
                                  max_retries=retries), sleeper=lambda seconds: None)

    def test_chat_tool_calls_and_private_fields(self):
        provider = self.provider()
        raw = {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"x.py"}'}}]}}]}
        messages = [{"role": "system", "content": "policy"},
                    {"role": "user", "content": "q", "_private": "must not send"}]
        with patch.object(provider, "_post", return_value=raw) as post:
            reply = provider.complete(messages, [])
        self.assertEqual(reply.calls[0].name, "read_file")
        self.assertNotIn("_private", post.call_args.args[1]["messages"][1])

    def test_responses_preserve_reasoning_and_call_output(self):
        provider = self.provider("responses")
        original = [{"type": "reasoning", "id": "r1", "summary": [], "encrypted_content": "opaque"},
                    {"type": "function_call", "id": "f1", "call_id": "c1", "name": "read_file", "arguments": '{"path":"x.py"}'}]
        messages = [{"role": "system", "content": "policy"}, {"role": "user", "content": "q"},
                    {"role": "assistant", "content": None, "_provider_items": original},
                    {"role": "tool", "tool_call_id": "c1", "content": '{"ok":true}'}]
        raw = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "report"}]}]}
        schema = {"type": "function", "function": {"name": "read_file", "parameters": {}, "strict": True}}
        with patch.object(provider, "_post", return_value=raw) as post:
            reply = provider.complete(messages, [schema])
        payload = post.call_args.args[1]
        self.assertEqual(reply.content, "report")
        self.assertEqual(payload["input"][1:3], original)
        self.assertEqual(payload["input"][-1]["type"], "function_call_output")
        self.assertEqual(payload["tools"][0]["name"], "read_file")
        self.assertFalse(payload["store"])

    def test_malformed_and_truncated_response(self):
        for raw in ({}, {"choices": []}, {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}):
            provider = self.provider()
            with patch.object(provider, "_post", return_value=raw), self.assertRaises(ProviderError):
                provider.complete([{"role": "user", "content": "q"}], [])
        provider = self.provider("responses")
        with patch.object(provider, "_post", return_value={"status": "incomplete", "output": []}), self.assertRaises(ProviderError):
            provider.complete([{"role": "system", "content": "s"}], [])

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ProviderError):
            self.provider()._validate_calls([ToolCall("c", "a", "{}"), ToolCall("c", "b", "{}")])

    def test_no_tool_choice_without_tools(self):
        provider = self.provider()
        with patch.object(provider, "_post", return_value={"choices": [{"message": {"content": "OK"}}]}) as post:
            provider.complete([{"role": "user", "content": "test"}], [])
        self.assertNotIn("tool_choice", post.call_args.args[1])
        self.assertNotIn("tools", post.call_args.args[1])

    def test_reasoning_content_survives_tool_round_trip(self):
        provider = self.provider()
        raw = {"choices": [{"message": {"reasoning_content": "opaque vendor reasoning", "content": None,
               "tool_calls": [{"id": "c", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}}]}
        with patch.object(provider, "_post", return_value=raw):
            reply = provider.complete([{"role": "user", "content": "q"}], [])
        self.assertEqual(reply.message()["reasoning_content"], "opaque vendor reasoning")

    def test_connection_checks_both_tool_call_and_tool_result(self):
        provider = self.provider()
        replies = [Reply(calls=[ToolCall("c", "connection_check", '{"value":"OK"}')]), Reply("OK")]
        with patch.object(provider, "stream_complete", side_effect=replies) as complete:
            result = provider.check_connection()
        self.assertTrue(result["tool_calling"])
        self.assertEqual(complete.call_args.args[0][-1]["role"], "tool")
        self.assertEqual(complete.call_args_list[0].kwargs["tool_choice"], "connection_check")
        self.assertEqual(complete.call_args_list[1].kwargs["tool_choice"], "none")
        with patch.object(provider, "stream_complete", return_value=Reply("I cannot call tools")):
            with self.assertRaises(ProviderError):
                provider.check_connection()

    def test_error_body_is_actionable_and_redacted(self):
        provider = self.provider()
        body = json.dumps({"error": {"message": "Bad model; key test-key; Bearer other-secret"}}).encode()
        failure = HTTPError("https://example", 400, "bad", {}, io.BytesIO(body))
        with patch.object(provider.opener, "open", side_effect=failure):
            with self.assertRaises(ProviderError) as captured:
                provider._post("chat/completions", {})
        message = str(captured.exception)
        self.assertIn("Bad model", message)
        self.assertNotIn("test-key", message)
        self.assertNotIn("other-secret", message)

    def test_transient_http_and_network_retry(self):
        for failure in (HTTPError("https://example", 429, "rate limit", {"Retry-After": "0"}, io.BytesIO(b"")),
                        URLError("network")):
            provider = self.provider(retries=1)
            success = io.BytesIO(b'{"choices":[]}')
            with patch.object(provider.opener, "open", side_effect=[failure, success]) as opened:
                self.assertEqual(provider._post("chat/completions", {}), {"choices": []})
                self.assertEqual(opened.call_count, 2)

    def test_auth_errors_do_not_retry_or_leak_key(self):
        provider = self.provider(retries=2)
        failure = HTTPError("https://example", 401, "test-key", {}, io.BytesIO(b"test-key"))
        with patch.object(provider.opener, "open", side_effect=failure) as opened:
            with self.assertRaises(ProviderError) as captured:
                provider._post("chat/completions", {})
        self.assertEqual(opened.call_count, 1)
        self.assertNotIn("test-key", str(captured.exception))

    def test_real_http_serialization_and_bearer_header(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                received.append((self.path, self.headers["Authorization"],
                                 json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                body = b'{"choices":[{"message":{"content":"ok"}}]}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = Config(api_key="test-key", model="model", api_style="chat_completions",
                            base_url=f"http://127.0.0.1:{server.server_port}")
            reply = LLMProvider(config).complete([{"role": "user", "content": "中文"}], [])
            self.assertEqual(reply.content, "ok")
            self.assertEqual(received[0][0], "/chat/completions")
            self.assertEqual(received[0][1], "Bearer test-key")
            self.assertEqual(received[0][2]["messages"][0]["content"], "中文")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_chat_stream_assembles_fragmented_calls_and_keeps_reasoning_private(self):
        provider, visible = self.provider(), []
        events = [
            {"choices": [{"delta": {"reasoning_content": "private reasoning", "content": "读取", "tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "read_", "arguments": '{"path":'}}]}}]},
            {"choices": [{"delta": {"content": "文件", "tool_calls": [
                {"index": 0, "function": {"name": "file", "arguments": '"x.py"}'}}]}, "finish_reason": "tool_calls"}]},
        ]
        with patch.object(provider, "_stream_events", return_value=iter(events)) as stream:
            reply = provider.stream_complete([{"role": "user", "content": "q"}], [], visible.append)
        self.assertEqual("".join(visible), "读取文件")
        self.assertNotIn("private reasoning", "".join(visible))
        self.assertEqual(reply.message()["reasoning_content"], "private reasoning")
        self.assertEqual(reply.calls[0], ToolCall("c1", "read_file", '{"path":"x.py"}'))
        self.assertTrue(stream.call_args.args[1]["stream"])

    def test_responses_stream_only_forwards_public_output_text(self):
        provider, visible = self.provider("responses"), []
        final = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "report"}]}]}
        events = [{"type": "response.reasoning_text.delta", "delta": "private"},
                  {"type": "response.output_text.delta", "delta": "report"},
                  {"type": "response.completed", "response": final}]
        with patch.object(provider, "_stream_events", return_value=iter(events)):
            reply = provider.stream_complete([{"role": "system", "content": "s"}], [], visible.append)
        self.assertEqual(visible, ["report"])
        self.assertEqual(reply.content, "report")

    def test_stream_forced_tool_uses_each_protocols_native_choice_shape(self):
        for style in ("responses", "chat_completions"):
            provider = self.provider(style)
            events = ([{"type": "response.completed", "response": {"status": "completed", "output": []}}]
                      if style == "responses" else [{"choices": [{"delta": {}, "finish_reason": "stop"}]}])
            with patch.object(provider, "_stream_events", return_value=iter(events)) as stream:
                provider.stream_complete([{"role": "system", "content": "s"}], [], lambda text: None,
                                         tool_choice="connection_check")
            choice = stream.call_args.args[1]["tool_choice"]
            expected = ({"type": "function", "name": "connection_check"} if style == "responses"
                        else {"type": "function", "function": {"name": "connection_check"}})
            self.assertEqual(choice, expected)

    def test_stream_rejects_missing_completion_truncation_and_malformed_calls(self):
        for events in (
            [{"choices": [{"delta": {"content": "partial"}}]}],
            [{"choices": [{"delta": {}, "finish_reason": "length"}]}],
            [{"choices": [{"delta": {"tool_calls": [{"function": {}}]}}]}],
            [{"choices": None}],
        ):
            provider = self.provider()
            with patch.object(provider, "_stream_events", return_value=iter(events)), self.assertRaises(ProviderError):
                provider.stream_complete([{"role": "user", "content": "q"}], [], lambda text: None)
        provider = self.provider("responses")
        with patch.object(provider, "_stream_events", return_value=iter([])), self.assertRaises(ProviderError):
            provider.stream_complete([{"role": "system", "content": "s"}], [], lambda text: None)

    def test_sse_transport_parses_crlf_and_does_not_retry_after_partial_content(self):
        provider = self.provider(retries=2)
        sse = ': heartbeat\r\nevent: message\r\ndata: {"choices":[{"delta":{"content":"中文"}}]}\r\n\r\n' \
              'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\r\n\r\ndata: [DONE]\r\n\r\n'
        with patch.object(provider.opener, "open", return_value=io.BytesIO(sse.encode())) as opened:
            reply = provider.stream_complete([{"role": "user", "content": "q"}], [], lambda text: None)
        self.assertEqual(reply.content, "中文")
        self.assertEqual(opened.call_count, 1)
        with patch.object(provider.opener, "open", return_value=io.BytesIO(b'data: invalid\n\n')) as opened:
            with self.assertRaises(ProviderError):
                provider.stream_complete([{"role": "user", "content": "q"}], [], lambda text: None)
        self.assertEqual(opened.call_count, 1)
