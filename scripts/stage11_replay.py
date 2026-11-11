"""Serve scripted completions for stage 11 on an isolated Docker network."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
from pathlib import Path
from time import sleep


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode()


def serve(fixtures, fail_after=None, host="127.0.0.1", port=8080):
    cases = json.loads(fixtures.read_text(encoding="utf-8"))
    requests = count(1)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.reply(200, {"ready": True})

        def do_POST(self):
            request_number = next(requests)
            if fail_after is not None and request_number > fail_after:
                self.reply(503, {"error": {"message": "fixture outage"}})
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            messages = request["messages"]
            user = next(row["content"] for row in messages if row["role"] == "user")
            matching = [row for row in cases if user.startswith(row["instruction"])]
            if not matching:
                self.reply(400, {"error": {"message": "missing fixture"}})
                return
            # Count recorded exchanges rather than using global task state.
            turns = [row["tool_calls"][0]["id"] for row in messages if row.get("tool_calls")]
            index = max((int(value.split("-")[-1]) + 1 for value in turns), default=0)
            message = {"role": "assistant", "content": "Required input unavailable."}
            if index < len(matching[0]["steps"]):
                steps = [row["steps"][index] for row in matching]
                if any(step != steps[0] for step in steps):
                    tool_texts = [str(row.get("content")).lstrip() for row in messages if row["role"] == "tool"]
                    marked = [row for row in matching if row.get("marker")
                              and any(text.startswith(row["marker"]) for text in tool_texts)]
                    if len(marked) != 1:
                        self.reply(400, {"error": {"message": "ambiguous replay tool output"}})
                        return
                    steps = [marked[0]["steps"][index]]
                name, args = steps[0]
                message = {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": f"fixture-{index}", "type": "function",
                                    "function": {"name": name, "arguments": encode(args).decode()}}],
                }
            sleep(0.02)
            # Synthetic counters depend on the actual serialized messages and schemas.
            prompt = max(1, len(encode([messages, request.get("tools", [])])) // 4)
            output = max(1, len(encode(message)) // 4)
            self.reply(200, {
                "id": "stage11-replay", "object": "chat.completion", "created": 0,
                "model": request["model"], "choices": [{
                    "index": 0, "message": message,
                    "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                }],
                "usage": {"prompt_tokens": prompt, "completion_tokens": output,
                          "total_tokens": prompt + output,
                          "prompt_tokens_details": {"cached_tokens": 0},
                          "completion_tokens_details": {"reasoning_tokens": 0}},
            })

        def reply(self, status, payload):
            body = encode(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    with ThreadingHTTPServer((host, port), Handler) as server:
        print(server.server_port, flush=True)
        server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures", type=Path)
    parser.add_argument("--fail-after", type=int)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    serve(args.fixtures, args.fail_after, args.host, args.port)
