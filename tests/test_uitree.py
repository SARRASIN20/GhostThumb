"""Extraction des éléments à partir d'un arbre uiautomator réaliste (écran 720×1612)."""

import unittest

from ghostthumb.adbtools import PhoneError
from ghostthumb.uitree import extract_elements, find_by_text, fold, format_elements, parse_hierarchy


def node(attrs: str, children: str = "") -> str:
    base = {"text": "", "resource-id": "", "class": "android.widget.TextView", "package": "com.whatsapp",
            "content-desc": "", "checkable": "false", "checked": "false", "clickable": "false", "enabled": "true",
            "focusable": "false", "focused": "false", "scrollable": "false", "long-clickable": "false",
            "password": "false", "selected": "false"}
    given = dict(part.split("=", 1) for part in attrs.split("|"))
    base.update(given)
    rendered = " ".join(f'{k}="{v}"' for k, v in base.items())
    return f"<node {rendered}>{children}</node>" if children else f"<node {rendered} />"


ROW1 = node("class=android.widget.RelativeLayout|clickable=true|long-clickable=true|bounds=[0,250][720,380]",
            node("text=Ahmed|bounds=[150,270][500,310]")
            + node("text=Salut, ça va ?|bounds=[150,320][600,360]")
            + node("text=10:42|bounds=[600,270][700,310]")
            + node("class=android.widget.ImageView|content-desc=Épingler|clickable=true|bounds=[620,320][700,370]"))
ROW2 = node("class=android.widget.RelativeLayout|clickable=true|bounds=[0,380][720,510]",
            node("text=Maman|bounds=[150,400][500,440]") + node("text=Photo|bounds=[150,450][600,490]"))
HIDDEN = node("class=android.widget.RelativeLayout|clickable=true|bounds=[0,1700][720,1800]",
              node("text=Caché|bounds=[150,1710][500,1750]"))
LIST = node("class=androidx.recyclerview.widget.RecyclerView|resource-id=com.whatsapp:id/conversations"
            "|scrollable=true|bounds=[0,250][720,1400]", ROW1 + ROW2 + HIDDEN)
FAB = node("class=android.widget.ImageButton|content-desc=Nouvelle discussion|clickable=true|bounds=[600,1500][700,1600]",
           node("class=android.widget.ImageButton|content-desc=Nouvelle discussion|clickable=true"
                "|bounds=[600,1500][700,1600]"))
ROOT = node(
    "class=android.widget.FrameLayout|clickable=true|bounds=[0,0][720,1612]",
    node("text=WhatsApp|resource-id=com.whatsapp:id/toolbar_title|bounds=[32,80][300,140]")
    + node("class=android.widget.ImageButton|content-desc=Rechercher|clickable=true|bounds=[560,80][640,140]")
    + node("class=android.widget.EditText|clickable=true|focused=true|hint=Rechercher...|bounds=[32,160][688,230]")
    + LIST
    + node("class=android.widget.Switch|text=Wi-Fi|checkable=true|checked=true|clickable=true|bounds=[500,1420][700,1480]")
    + node("class=android.widget.EditText|password=true|clickable=true|resource-id=com.whatsapp:id/password_field"
           "|bounds=[32,1490][560,1550]")
    + FAB,
)
XML = ("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
       f'<hierarchy rotation="0">{ROOT}</hierarchy>')


class ExtractTest(unittest.TestCase):
    def setUp(self):
        nodes, self.rotation = parse_hierarchy("UI hierchary dumped to: /dev/tty\n" + XML)
        self.elements = extract_elements(nodes, 720, 1612)
        self.labels = [e.label for e in self.elements]

    def test_reading_order_and_kinds(self):
        self.assertEqual(self.rotation, 0)
        self.assertEqual([(e.kind, e.label) for e in self.elements], [
            ("texte", "WhatsApp"),
            ("bouton", "Rechercher"),
            ("champ", "Rechercher..."),
            ("liste", "conversations"),
            ("bouton", "Ahmed · Salut, ça va ? · 10:42"),
            ("bouton", "Épingler"),
            ("bouton", "Maman · Photo"),
            ("interrupteur", "Wi-Fi"),
            ("champ", "password field"),
            ("bouton", "Nouvelle discussion"),
        ])
        self.assertEqual([e.number for e in self.elements], list(range(1, 11)))

    def test_full_screen_clickable_root_is_not_an_element_and_absorbs_nothing(self):
        self.assertNotIn("FrameLayout", self.labels)
        self.assertIn("WhatsApp", self.labels)

    def test_child_texts_are_merged_into_their_row_only_once(self):
        self.assertNotIn("Ahmed", self.labels)
        self.assertNotIn("10:42", self.labels)

    def test_offscreen_and_duplicates_are_dropped(self):
        self.assertFalse(any("Caché" in label for label in self.labels))
        self.assertEqual(self.labels.count("Nouvelle discussion"), 1)

    def test_flags(self):
        by_label = {e.label: e for e in self.elements}
        self.assertEqual(by_label["Wi-Fi"].flags, ["coché"])
        self.assertEqual(by_label["password field"].flags, ["mot de passe"])
        self.assertEqual(by_label["Rechercher..."].flags, ["curseur ici"])
        self.assertEqual(by_label["conversations"].flags, ["défilable"])

    def test_format_uses_image_coordinates(self):
        text = format_elements(self.elements, 0.5)
        self.assertIn("[5] bouton « Ahmed · Salut, ça va ? · 10:42 » (180,158)", text)
        self.assertIn("[8] interrupteur « Wi-Fi » (300,725) [coché]", text)

    def test_find_by_text(self):
        self.assertEqual([e.number for e in find_by_text(self.elements, "rechercher")], [2])
        self.assertEqual(find_by_text(self.elements, "ahmed")[0].number, 5)
        self.assertEqual(find_by_text(self.elements, "EPINGLER")[0].label, "Épingler")
        self.assertEqual(find_by_text(self.elements, "introuvable"), [])
        self.assertEqual(find_by_text(self.elements, "   "), [])

    def test_limit_keeps_actionable_elements_first(self):
        nodes, _ = parse_hierarchy(XML)
        few = extract_elements(nodes, 720, 1612, limit=3)
        self.assertEqual(len(few), 3)
        self.assertTrue(all(e.kind != "texte" for e in few))

    def test_bad_xml(self):
        with self.assertRaises(PhoneError):
            parse_hierarchy("ERROR: could not get idle state.")
        with self.assertRaises(PhoneError):
            parse_hierarchy("<hierarchy><node></hierarchy>")

    def test_fold(self):
        self.assertEqual(fold("Paramètres"), "parametres")


class RealWorldShapesTest(unittest.TestCase):
    """Formes relevées sur le composeur du Tecno (données personnelles remplacées)."""

    def test_wrappers_vanish_and_redundant_child_texts_are_dropped(self):
        wrapper = node(
            "class=android.widget.FrameLayout|resource-id=com.sh.smart.caller:id/os_swipe_layout|clickable=true"
            "|bounds=[0,300][720,420]",
            node("class=android.widget.LinearLayout|content-desc=Détails de contact pour Ali|clickable=true"
                 "|bounds=[0,300][620,420]", node("text=Ali|bounds=[100,310][400,350]"))
            + node("class=android.widget.ImageView|content-desc=Détails du journal des appels|clickable=true"
                   "|bounds=[620,320][700,400]"))
        key = node("class=android.widget.FrameLayout|content-desc=2,ABC|clickable=true|bounds=[200,600][400,700]",
                   node("text=2|bounds=[280,610][320,660]") + node("text=ABC|bounds=[270,665][330,690]"))
        zero = node("class=android.widget.FrameLayout|content-desc=0|clickable=true|bounds=[200,900][400,1000]",
                    node("text=0|bounds=[280,910][320,960]") + node("text=+|bounds=[290,965][310,990]"))
        xml = f'<hierarchy rotation="0">{wrapper}{key}{zero}</hierarchy>'
        labels = [e.label for e in extract_elements(parse_hierarchy(xml)[0], 720, 1612)]
        self.assertEqual(labels, ["Détails de contact pour Ali", "Détails du journal des appels", "2,ABC", "0 · +"])

    def test_rows_under_a_later_panel_are_trimmed_or_dropped(self):
        rows = (node("class=android.widget.LinearLayout|clickable=true|bounds=[0,392][720,532]",
                     node("text=Ali|bounds=[96,422][190,463]"))
                + node("class=android.widget.LinearLayout|clickable=true|bounds=[0,672][720,812]",
                       node("text=Sara|bounds=[96,702][253,743]"))
                + node("class=android.widget.LinearLayout|clickable=true|bounds=[0,812][720,892]",
                       node("text=Omar|bounds=[96,842][253,883]")))
        listing = node("class=androidx.recyclerview.widget.RecyclerView|scrollable=true|bounds=[0,392][720,892]", rows)
        pad = node("class=android.widget.FrameLayout|bounds=[0,80][720,1412]",
                   node("class=android.widget.LinearLayout|resource-id=x:id/dialpad_view|clickable=true"
                        "|bounds=[0,755][720,1412]",
                        node("class=android.widget.LinearLayout|content-desc=2,ABC|clickable=true"
                             "|bounds=[244,788][474,889]")))
        xml = ('<hierarchy rotation="0">'
               + node("class=android.widget.FrameLayout|bounds=[0,0][720,1612]", listing + pad) + "</hierarchy>")
        by_label = {e.label: e for e in extract_elements(parse_hierarchy(xml)[0], 720, 1612)}
        self.assertEqual(by_label["Ali"].bounds, (0, 392, 720, 532))
        self.assertEqual(by_label["Sara"].center, (360, 713))  # seule la bande visible au-dessus du pavé
        self.assertNotIn("Omar", by_label)  # entièrement sous le pavé : la toucher composerait un chiffre
        self.assertIn("2,ABC", by_label)
        self.assertNotIn("dialpad view", by_label)


if __name__ == "__main__":
    unittest.main()
