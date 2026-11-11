import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

URL = "https://openjiuwen.iir.nsu.ru/mcp"
REQUIRED_TOOLS = {"create_registration_challenge", "register_agent", "heartbeat", "deregister_agent"}
RATE_LIMIT_CODE = -32029


class RegistryError(RuntimeError):
    pass


def retry_after(value):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 1.0


class RateLimitTransport(httpx.AsyncBaseTransport):
    """Translate HTTP 429 into a local JSON-RPC error without killing MCP's task group.

    MCP SDK 1.30 raises non-2xx in a background task. Catching HTTPStatusError
    around call_tool alone cannot keep that session alive. This adapter only
    translates explicit rejected POST requests; it never replays a mutation.
    Protocol then waits and requests a NEW nonce before another signed attempt.
    """

    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        if response.status_code != 429 or request.method != "POST":
            return response
        payload = json.loads(request.content)
        if payload.get("id") is None:
            return response
        seconds = retry_after(response.headers.get("Retry-After", "1"))
        await response.aclose()
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "error": {"code": RATE_LIMIT_CODE, "message": "HTTP 429", "data": {"retry_after": seconds}},
            },
        )

    async def aclose(self):
        await self.inner.aclose()


@asynccontextmanager
async def connect(url=URL):
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=60), transport=RateLimitTransport()) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield Registry(session)


class Registry:
    def __init__(self, session):
        self.session = session

    async def discover(self):
        tools = await self.session.list_tools()
        missing = REQUIRED_TOOLS - {t.name for t in tools.tools}
        if missing:
            raise RegistryError(f"Сервер не публикует инструменты: {sorted(missing)}")
        return tools.model_dump(mode="json", by_alias=True)

    async def call(self, name, arguments):
        try:
            result = await self.session.call_tool(name, arguments)
        except McpError as exc:
            if exc.error.code == RATE_LIMIT_CODE and isinstance(exc.error.data, dict):
                raise Overloaded(retry_after(exc.error.data.get("retry_after"))) from None
            raise RegistryError(f"{name}: ошибка MCP, код {exc.error.code}") from None
        if result.isError:
            # Do not echo a server error that might contain submitted participant data.
            messages = " ".join(c.text for c in result.content if c.type == "text")
            if "overloaded" in messages:
                import re

                match = re.search(r"retry after (\d+) seconds", messages)
                raise Overloaded(int(match[1]) if match else 1)
            raise RegistryError(
                f"{name}: сервер отклонил запрос. "
                "Проверьте протокол, анкету, согласие и собственный registration_id."
            )
        data = result.structuredContent
        if data is None:
            texts = [c.text for c in result.content if c.type == "text"]
            if len(texts) != 1:
                raise RegistryError(f"{name}: неожиданный формат ответа")
            data = json.loads(texts[0])
        if not isinstance(data, dict):
            raise RegistryError(f"{name}: ожидался JSON-объект")
        return data


class Overloaded(RegistryError):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__("Сервер перегружен")
