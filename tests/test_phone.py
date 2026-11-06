"""Logique du téléphone sans appareil : état dumpsys, liste d'apps, garde-fou, échelle, gestes calculés."""

import unittest

from ghostthumb.adbtools import Device, PhoneError, choose_device, parse_devices
from ghostthumb.config import DEFAULTS
from ghostthumb.phone import Phone, parse_app_list, parse_state
from ghostthumb.uitree import Element

DUMPSYS = """  mCurrentFocus=Window{a1b2c3 u0 com.whatsapp/com.whatsapp.HomeActivity}
  mFocusedApp=ActivityRecord{d4e5f6 u0 com.whatsapp/.HomeActivity t123}
    mInputShown=true
  mKeyguardShowing=false
  mWakefulness=Awake
"""

APP_LIST = """[server] INFO: Device: [TECNO] TECNO TECNO BG7n (Android 13)
[server] INFO: List of apps:
 * Paramètres                     com.android.settings
 - WhatsApp                       com.whatsapp
 - Un nom d'application vraiment long
                                 com.example.longname
 - MyBank                         com.example.mybank
 - Photos                         com.example.photos
 - Photo Editor                   com.example.photoeditor
"""


class FakeHands:
    name = "fake"

    def __init__(self):
        self.calls = []

    def alive(self):
        return True

    def close(self):
        pass

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return "frappe" if name == "type_text" else None
        return record


def offline_phone(foreground="com.whatsapp"):
    phone = Phone(config={**DEFAULTS, "settle_seconds": 0})
    phone.serial = "TEST"
    phone.screen_size = (720, 1612)
    phone.scale = 572 / 720
    phone.hands_obj = FakeHands()
    phone.ensure_device = lambda: "TEST"
    phone.foreground = lambda: foreground
    phone.get_hands = lambda: phone.hands_obj
    phone.journal = lambda *a, **k: None
    phone.apps = lambda refresh=False: parse_app_list(APP_LIST)
    return phone


class StateParsingTest(unittest.TestCase):
    def test_full_state(self):
        st = parse_state(DUMPSYS)
        self.assertEqual(st, {"package": "com.whatsapp", "window": "com.whatsapp/com.whatsapp.HomeActivity",
                              "keyboard": True, "locked": False, "awake": True})

    def test_notification_shade_keeps_the_app_underneath(self):
        st = parse_state("mCurrentFocus=Window{abc u0 NotificationShade}\n"
                         "mFocusedApp=ActivityRecord{def u0 com.android.launcher3/.Launcher t1}")
        self.assertEqual((st["package"], st["window"]), ("com.android.launcher3", "NotificationShade"))
        self.assertIsNone(st["locked"])

    def test_devices(self):
        devices = parse_devices("List of devices attached\n"
                                "0123ABC device usb:1-1 product:BG7n model:TECNO_BG7n transport_id:3\n"
                                "192.168.1.20:5555 device product:BG7n transport_id:4\n"
                                "XYZ unauthorized usb:1-2 transport_id:5\n")
        self.assertEqual([d.serial for d in devices], ["0123ABC", "192.168.1.20:5555", "XYZ"])
        self.assertEqual(devices[0].info["model"], "TECNO_BG7n")
        self.assertEqual(choose_device(devices).serial, "0123ABC")  # USB d'abord
        self.assertEqual(choose_device(devices[1:2]).serial, "192.168.1.20:5555")
        with self.assertRaisesRegex(PhoneError, "Autoriser"):
            choose_device([Device("XYZ", "unauthorized")])
        with self.assertRaisesRegex(PhoneError, "Aucun"):
            choose_device([])


class AppsTest(unittest.TestCase):
    def test_parse_app_list(self):
        apps = parse_app_list(APP_LIST)
        self.assertEqual(apps[0], {"label": "Paramètres", "package": "com.android.settings", "system": True})
        self.assertIn({"label": "Un nom d'application vraiment long", "package": "com.example.longname",
                       "system": False}, apps)
        self.assertEqual(len(apps), 6)

    def test_resolve(self):
        phone = offline_phone()
        self.assertEqual(phone.resolve_app("whatsapp")[0], "com.whatsapp")
        self.assertEqual(phone.resolve_app("parametres")[0], "com.android.settings")
        self.assertEqual(phone.resolve_app("com.whatsapp")[0], "com.whatsapp")
        self.assertEqual(phone.resolve_app("com.inconnue.app")[0], "com.inconnue.app")
        self.assertEqual(phone.resolve_app("Photos")[0], "com.example.photos")  # le nom exact gagne
        with self.assertRaisesRegex(PhoneError, "Plusieurs"):
            phone.resolve_app("photo")
        with self.assertRaisesRegex(PhoneError, "Aucune app"):
            phone.resolve_app("zzz")


class GuardAndGestureTest(unittest.TestCase):
    def test_protection(self):
        phone = offline_phone()
        self.assertTrue(phone.is_protected("com.example.mybank"))
        self.assertTrue(phone.is_protected("com.paypal.android.p2pmobile"))
        self.assertFalse(phone.is_protected("com.whatsapp"))
        self.assertFalse(phone.is_protected(""))

    def test_protected_app_blocks_actions_but_allows_leaving(self):
        phone = offline_phone(foreground="com.example.mybank")
        with self.assertRaisesRegex(PhoneError, "bloquée"):
            phone.tap(x=10, y=10)
        with self.assertRaisesRegex(PhoneError, "bloquée"):
            phone.type_text("1234")
        with self.assertRaisesRegex(PhoneError, "bloquée"):
            phone.key("enter")
        self.assertEqual(phone.hands_obj.calls, [])
        phone.key("home")
        self.assertEqual(phone.hands_obj.calls, [("key", (3,), {})])

    def test_opening_a_protected_app_is_refused(self):
        phone = offline_phone()
        with self.assertRaisesRegex(PhoneError, "bloquée"):
            phone.open_app("MyBank")

    def test_tapping_a_protected_app_icon_is_refused(self):
        phone = offline_phone(foreground="com.transsion.hilauncher")
        phone._apps = parse_app_list(APP_LIST)
        phone.elements = [Element(6, "bouton", "MyBank", (400, 150, 500, 250), []),
                          Element(7, "bouton", "WhatsApp", (500, 150, 600, 250), [])]
        with self.assertRaisesRegex(PhoneError, "Appui bloqué"):
            phone.tap(element=6)
        with self.assertRaisesRegex(PhoneError, "Appui bloqué"):
            phone.tap(x=450 * 572 / 720, y=200 * 572 / 720)
        self.assertEqual(phone.hands_obj.calls, [])
        phone.tap(element=7)
        self.assertEqual(phone.hands_obj.calls, [("tap", (550, 200), {})])

    def test_image_coordinates_are_converted(self):
        phone = offline_phone()
        self.assertEqual(phone.to_device(286, 640), (360, 806))
        self.assertEqual(phone.to_device(-5, 99999), (0, 1611))
        phone.tap(x=286, y=640)
        self.assertEqual(phone.hands_obj.calls, [("tap", (360, 806), {})])

    def test_tap_by_element_uses_screen_pixels(self):
        phone = offline_phone()
        phone.elements = [Element(1, "bouton", "Envoyer", (600, 1500, 700, 1600), [])]
        res = phone.tap(element=1)
        self.assertEqual(phone.hands_obj.calls, [("tap", (650, 1550), {})])
        self.assertIn("Envoyer", res.text)
        with self.assertRaisesRegex(PhoneError, "Pas d'élément"):
            phone.tap(element=9)

    def test_scroll_down_moves_finger_up_away_from_edges(self):
        phone = offline_phone()
        phone.scroll("down")
        self.assertEqual(phone.hands_obj.calls,
                         [("swipe", (360, 1095, 360, 515), {"duration_ms": 450, "hold_ms": 250})])
        with self.assertRaises(PhoneError):
            phone.scroll("sideways")

    def test_type_taps_field_then_types(self):
        phone = offline_phone()
        phone.elements = [Element(3, "champ", "Message", (100, 1400, 500, 1460), [])]
        res = phone.type_text("Salut", element=3, clear=True, submit=True)
        names = [c[0] for c in phone.hands_obj.calls]
        self.assertEqual(names, ["tap", "clear_field", "type_text", "key"])
        self.assertEqual(phone.hands_obj.calls[-1], ("key", (66,), {}))
        self.assertIn("5 caractères", res.text)

    def test_unknown_key(self):
        with self.assertRaisesRegex(PhoneError, "Touche inconnue"):
            offline_phone().key("hyperdrive")


if __name__ == "__main__":
    unittest.main()
