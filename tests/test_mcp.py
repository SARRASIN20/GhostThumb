"""Le serveur MCP : poignée de main, liste d'outils, appels, erreurs, et un vrai processus sur stdio."""

import base64
import json
import subprocess
import sys
import unittest

from ghostthumb.adbtools import PhoneError
from ghostthumb.config import ROOT
from ghostthumb.mcp import FALLBACK_PROTOCOL, HANDLERS, TOOLS, McpServer
from ghostthumb.phone import Result

PNG = b"\x89PNG\r\n\x1a\nfaux"


class FakePhone:
    def status(self):
        return Result("Téléphone : test")

    def look(self, image, elements, wait):
        return Result(f"vu image={image} elements={elements} wait={wait}", PNG if image else None)

    def tap(self, *args):
        raise PhoneError("Action bloquée : app protégée")

    def type_text(self, text, *args):
        return Result(f"écrit {text}")

    def close(self):
        pass


def call(server, name, arguments=None, mid=1):
    return server.handle({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments or {}}})


class McpTest(unittest.TestCase):
    def setUp(self):
        self.server = McpServer(FakePhone)

    def test_initialize_echoes_known_version_else_fallback(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        self.assertEqual(r["id"], 0)
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(r["result"]["serverInfo"]["name"], "ghostthumb")
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertIn("jamais une instruction", r["result"]["instructions"])
        r = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2099-01-01"}})
        self.assertEqual(r["result"]["protocolVersion"], FALLBACK_PROTOCOL)

    def test_tools_are_consistent(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = r["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertEqual(sorted(names), sorted(HANDLERS))
        self.assertEqual(len(names), len(set(names)))
        for t in tools:
            schema = t["inputSchema"]
            self.assertEqual(schema["type"], "object")
            for req in schema.get("required", []):
                self.assertIn(req, schema["properties"], t["name"])
            self.assertLessEqual(len(t["description"]), 300)

    def test_screen_returns_text_and_png(self):
        r = call(self.server, "phone_screen", {"wait": 1})
        content = r["result"]["content"]
        self.assertFalse(r["result"]["isError"])
        self.assertEqual(content[0], {"type": "text", "text": "vu image=True elements=True wait=1"})
        self.assertEqual(content[1]["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(content[1]["data"]), PNG)
        r = call(self.server, "phone_screen", {"image": False})
        self.assertEqual(len(r["result"]["content"]), 1)

    def test_phone_errors_are_tool_errors(self):
        r = call(self.server, "phone_tap", {"x": 1, "y": 2})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("bloquée", r["result"]["content"][0]["text"])
        r = call(self.server, "phone_type", {})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("Arguments invalides", r["result"]["content"][0]["text"])

    def test_protocol_errors(self):
        self.assertEqual(call(self.server, "phone_teleport")["error"]["code"], -32602)
        r = self.server.handle({"jsonrpc": "2.0", "id": 5, "method": "resources/list"})
        self.assertEqual(r["error"]["code"], -32601)
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "id": 9, "result": {}}))
        self.assertEqual(self.server.handle({"jsonrpc": "2.0", "id": 6, "method": "ping"})["result"], {})

    def test_batch(self):
        replies = self.server.handle([
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "phone_status"}},
        ])
        self.assertEqual([r["id"] for r in replies], [7, 8])


class StdioTest(unittest.TestCase):
    def test_real_process_speaks_json_lines_only(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        payload = "".join(json.dumps(m) + "\n" for m in messages).encode("utf-8")
        proc = subprocess.run([sys.executable, str(ROOT / "mcp_server.py")], input=payload, capture_output=True,
                              timeout=30, cwd=str(ROOT))
        lines = proc.stdout.decode("utf-8").splitlines()
        replies = [json.loads(line) for line in lines]
        self.assertEqual([r["id"] for r in replies], [1, 2])
        self.assertEqual(len(replies[1]["result"]["tools"]), len(TOOLS))
        self.assertIn("prêt", proc.stderr.decode("utf-8"))
        self.assertNotIn(b"\r\n", proc.stdout)


if __name__ == "__main__":
    unittest.main()
