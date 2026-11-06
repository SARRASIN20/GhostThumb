"""Le client scrcpy, vérifié contre un portage Python de ControlMessageReader.java (scrcpy 5.0.1)."""

import io
import socket
import struct
import threading
import unittest

from ghostthumb.adbtools import PhoneError
from ghostthumb.control import (
    ACTION_DOWN, ACTION_MOVE, ACTION_UP, POINTER_ID_GENERIC_FINGER, ControlChannel, connect_control, i16_fixed,
    keyable, msg_get_clipboard, msg_keycode, msg_scroll, msg_set_clipboard, msg_start_app, msg_text, msg_touch,
    swipe_points, u16_fixed,
)


class JavaReader:
    """Lecture identique à DataInputStream + ControlMessageReader (types utilisés par GhostThumb)."""

    def __init__(self, data):
        self.s = data if hasattr(data, "read") else io.BytesIO(data)

    def _read(self, n):
        b = self.s.read(n)
        if len(b) != n:
            raise EOFError
        return b

    def u8(self):
        return self._read(1)[0]

    def i32(self):
        return struct.unpack(">i", self._read(4))[0]

    def i64(self):
        return struct.unpack(">q", self._read(8))[0]

    def u16(self):
        return struct.unpack(">H", self._read(2))[0]

    def i16(self):
        return struct.unpack(">h", self._read(2))[0]

    def string(self, size_bytes=4):
        n = 0
        for _ in range(size_bytes):
            n = (n << 8) | self.u8()
        return self._read(n).decode("utf-8")

    def read(self):
        t = self.u8()
        if t == 0:
            return {"type": "key", "action": self.u8(), "keycode": self.i32(), "repeat": self.i32(), "meta": self.i32()}
        if t == 1:
            return {"type": "text", "text": self.string()}
        if t == 2:
            return {"type": "touch", "action": self.u8(), "pointer": self.i64(), "x": self.i32(), "y": self.i32(),
                    "w": self.u16(), "h": self.u16(), "pressure": self.u16(), "action_button": self.i32(),
                    "buttons": self.i32()}
        if t == 3:
            return {"type": "scroll", "x": self.i32(), "y": self.i32(), "w": self.u16(), "h": self.u16(),
                    "h_scroll": self.i16() / 0x8000 * 16, "v_scroll": self.i16() / 0x8000 * 16, "buttons": self.i32()}
        if t in (5, 6, 7):
            return {"type": "empty", "code": t}
        if t == 8:
            return {"type": "get_clipboard", "copy_key": self.u8()}
        if t == 9:
            return {"type": "set_clipboard", "sequence": self.i64(), "paste": self.u8() != 0, "text": self.string()}
        if t == 16:
            return {"type": "start_app", "name": self.string(1)}
        raise AssertionError(f"type inconnu {t}")


class MessageFormatTest(unittest.TestCase):
    def test_keycode(self):
        data = msg_keycode(ACTION_DOWN, 66, 0, 0x3000)
        self.assertEqual(len(data), 14)
        self.assertEqual(JavaReader(data).read(), {"type": "key", "action": 0, "keycode": 66, "repeat": 0, "meta": 0x3000})

    def test_touch_is_32_bytes_and_round_trips(self):
        data = msg_touch(ACTION_DOWN, 360, 1500, 720, 1612)
        self.assertEqual(len(data), 32)
        m = JavaReader(data).read()
        self.assertEqual((m["action"], m["pointer"], m["x"], m["y"], m["w"], m["h"]),
                         (ACTION_DOWN, POINTER_ID_GENERIC_FINGER, 360, 1500, 720, 1612))
        self.assertEqual(m["pressure"], 0xFFFF)
        self.assertEqual((m["action_button"], m["buttons"]), (0, 0))
        self.assertEqual(JavaReader(msg_touch(ACTION_UP, 1, 2, 3, 4, pressure=0.0)).read()["pressure"], 0)

    def test_text_utf8_length_is_in_bytes(self):
        m = JavaReader(msg_text("ça va ?")).read()
        self.assertEqual(m, {"type": "text", "text": "ça va ?"})

    def test_set_clipboard_any_alphabet(self):
        text = "Salut 👋 مرحبا, ça marche"
        m = JavaReader(msg_set_clipboard(7, text, paste=True)).read()
        self.assertEqual(m, {"type": "set_clipboard", "sequence": 7, "paste": True, "text": text})
        self.assertFalse(JavaReader(msg_set_clipboard(8, "x", paste=False)).read()["paste"])

    def test_scroll_fixed_point(self):
        m = JavaReader(msg_scroll(10, 20, 720, 1612, 0.0, -2.0)).read()
        self.assertEqual((m["x"], m["y"], m["h_scroll"]), (10, 20, 0.0))
        self.assertAlmostEqual(m["v_scroll"], -2.0, places=3)

    def test_get_clipboard_and_start_app(self):
        self.assertEqual(JavaReader(msg_get_clipboard()).read(), {"type": "get_clipboard", "copy_key": 0})
        self.assertEqual(JavaReader(msg_start_app("com.whatsapp")).read(), {"type": "start_app", "name": "com.whatsapp"})
        with self.assertRaises(PhoneError):
            msg_start_app("x" * 256)

    def test_messages_concatenate_without_ambiguity(self):
        stream = msg_touch(ACTION_DOWN, 1, 2, 3, 4) + msg_text("ok") + msg_keycode(ACTION_UP, 4)
        r = JavaReader(stream)
        self.assertEqual([r.read()["type"] for _ in range(3)], ["touch", "text", "key"])

    def test_fixed_point_helpers(self):
        self.assertEqual(u16_fixed(1.0), 0xFFFF)
        self.assertEqual(u16_fixed(0.5), 0x8000)
        self.assertEqual(i16_fixed(1.0), 0x7FFF)
        self.assertEqual(i16_fixed(-1.0), -0x8000)

    def test_keyable_and_swipe(self):
        self.assertTrue(keyable("Hello, world! 123"))
        self.assertFalse(keyable("café"))
        self.assertFalse(keyable("ligne 1\nligne 2"))
        pts = swipe_points(0, 0, 100, 50, 4)
        self.assertEqual(pts[-1], (100, 50))
        self.assertEqual(len(pts), 4)


class FakeDevice:
    """Côté téléphone simulé : refuse `refusals` connexions (serveur pas encore prêt), puis
    envoie l'octet témoin, lit les messages, acquitte le presse-papiers et répond à GET_CLIPBOARD."""

    def __init__(self, refusals=0, clipboard="ancien contenu"):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.refusals = refusals
        self.clipboard = clipboard
        self.received = []
        self.done = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        for _ in range(self.refusals):
            conn, _ = self.listener.accept()
            conn.close()
        conn, _ = self.listener.accept()
        conn.sendall(b"\x00")
        reader = JavaReader(conn.makefile("rb"))
        try:
            while True:
                m = reader.read()
                self.received.append(m)
                if m["type"] == "set_clipboard":
                    self.clipboard = m["text"]
                    if m["sequence"]:
                        conn.sendall(b"\x01" + struct.pack(">q", m["sequence"]))
                elif m["type"] == "get_clipboard" and self.clipboard is not None:
                    data = self.clipboard.encode("utf-8")
                    conn.sendall(b"\x00" + struct.pack(">I", len(data)) + data)
        except (EOFError, OSError):
            pass
        finally:
            conn.close()
            self.listener.close()
            self.done.set()


class ChannelTest(unittest.TestCase):
    def test_connect_retries_until_dummy_byte(self):
        dev = FakeDevice(refusals=3)
        sock = connect_control(dev.port, timeout=5)
        channel = ControlChannel(sock)
        channel.send(msg_touch(ACTION_DOWN, 5, 6, 720, 1612))
        channel.send(msg_touch(ACTION_MOVE, 5, 60, 720, 1612))
        channel.send(msg_touch(ACTION_UP, 5, 60, 720, 1612, pressure=0))
        channel.close()
        self.assertTrue(dev.done.wait(5))
        self.assertEqual([m["action"] for m in dev.received], [ACTION_DOWN, ACTION_MOVE, ACTION_UP])

    def test_clipboard_round_trip_and_ack(self):
        dev = FakeDevice()
        channel = ControlChannel(connect_control(dev.port, timeout=5))
        self.assertEqual(channel.get_clipboard(timeout=2), "ancien contenu")
        channel.send(msg_set_clipboard(42, "ça marche ✅", paste=True))
        self.assertTrue(channel.wait_ack(42, timeout=2))
        self.assertFalse(channel.wait_ack(43, timeout=0.2))
        self.assertEqual(channel.get_clipboard(timeout=2), "ça marche ✅")
        channel.close()
        self.assertTrue(dev.done.wait(5))

    def test_empty_clipboard_returns_none(self):
        dev = FakeDevice(clipboard=None)
        channel = ControlChannel(connect_control(dev.port, timeout=5))
        self.assertIsNone(channel.get_clipboard(timeout=0.3))
        channel.close()

    def test_connect_gives_up_when_server_dies(self):
        with self.assertRaises(PhoneError):
            connect_control(1, timeout=3, server_alive=lambda: False, describe=lambda: "mort")

    def test_send_after_close_is_a_clear_error(self):
        dev = FakeDevice()
        channel = ControlChannel(connect_control(dev.port, timeout=5))
        channel.close()
        with self.assertRaises(PhoneError):
            channel.send(msg_text("x"))


if __name__ == "__main__":
    unittest.main()
