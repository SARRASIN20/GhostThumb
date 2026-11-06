"""Arbre d'interface (uiautomator dump) → liste courte d'éléments numérotés que l'IA peut viser."""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .adbtools import PhoneError

BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
EDIT_CLASSES = ("EditText", "AutoCompleteTextView", "SearchAutoComplete")
LABEL_MAX = 80
TEXT_MAX = 120
# Un conteneur cliquable plus grand que ça n'absorbe pas les textes de ses enfants
# (sinon un écran entier cliquable deviendrait un seul élément illisible).
ABSORB_MAX_AREA = 0.5
# Au-delà, un nœud cliquable n'est pas compté comme cache (fond d'écran racine, pas un panneau).
OCCLUDER_MAX_AREA = 0.7
MIN_VISIBLE = 8


@dataclass
class Node:
    attrs: dict
    parent: "Node | None" = None
    children: list = field(default_factory=list)

    def flag(self, key: str) -> bool:
        return self.attrs.get(key) == "true"

    @property
    def cls(self) -> str:
        return self.attrs.get("class", "")

    @property
    def editable(self) -> bool:
        return self.cls.endswith(EDIT_CLASSES)

    @property
    def clickable(self) -> bool:
        return self.flag("clickable") or self.flag("long-clickable")

    @property
    def actionable(self) -> bool:
        return self.clickable or self.flag("checkable") or self.editable

    @property
    def own_label(self) -> str:
        text = clean(self.attrs.get("text", ""))
        desc = clean(self.attrs.get("content-desc", ""))
        if text and desc and desc.casefold() not in text.casefold():
            return f"{text} ({desc})"
        return text or desc


@dataclass
class Element:
    number: int
    kind: str
    label: str
    bounds: tuple[int, int, int, int]  # pixels de l'écran, rognés à l'écran
    flags: list[str]
    package: str = ""

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    def to_dict(self) -> dict:
        return {"number": self.number, "kind": self.kind, "label": self.label,
                "bounds": list(self.bounds), "flags": self.flags, "package": self.package}

    @classmethod
    def from_dict(cls, d: dict) -> "Element":
        return cls(d["number"], d["kind"], d["label"], tuple(d["bounds"]), list(d["flags"]), d.get("package", ""))


def clean(text: str, limit: int = LABEL_MAX) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def fold(text: str) -> str:
    """Forme de comparaison : sans casse ni accents."""
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


def parse_hierarchy(xml_text: str) -> tuple[list[Node], int]:
    start, end = xml_text.find("<hierarchy"), xml_text.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise PhoneError("Arbre d'interface illisible.")
    try:
        root = ET.fromstring(xml_text[start:end + len("</hierarchy>")])
    except ET.ParseError as e:
        raise PhoneError(f"Arbre d'interface mal formé ({e}).") from None
    nodes: list[Node] = []

    def walk(el, parent):
        node = Node(dict(el.attrib), parent)
        if parent:
            parent.children.append(node)
        nodes.append(node)
        for child in el:
            if child.tag == "node":
                walk(child, node)

    for child in root:
        if child.tag == "node":
            walk(child, None)
    rotation = int(root.attrib.get("rotation", "0") or 0)
    return nodes, rotation


def node_bounds(node: Node, screen_w: int, screen_h: int) -> tuple[int, int, int, int] | None:
    m = BOUNDS_RE.fullmatch(node.attrs.get("bounds", ""))
    if not m:
        return None
    x1, y1, x2, y2 = (int(v) for v in m.groups())
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(screen_w, x2), min(screen_h, y2)
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None
    return x1, y1, x2, y2


def descendant_text(node: Node, own: str = "", limit: int = LABEL_MAX) -> str:
    """Textes des descendants, sans entrer dans les descendants actionnables (ils ont leur propre ligne)
    et sans répéter ce que le libellé propre dit déjà (« 2,ABC » + « 2 » + « ABC » → « 2,ABC »)."""
    parts: list[str] = []
    seen = fold(own)

    def walk(n: Node):
        nonlocal seen
        for child in n.children:
            if child.actionable:
                continue
            label = child.own_label
            if label and fold(label) not in seen:
                parts.append(label)
                seen += "\n" + fold(label)
            if len(parts) < 4:
                walk(child)

    walk(node)
    return clean(" · ".join(parts[:4]), limit)


def has_actionable_descendant(node: Node) -> bool:
    return any(child.actionable or has_actionable_descendant(child) for child in node.children)


def visible_part(bounds, subtree_end: int, occluders) -> tuple[int, int, int, int] | None:
    """Partie d'un élément que ne recouvre aucun nœud cliquable dessiné après lui.

    Sans ordre de dessin dans l'arbre, l'ordre du document fait foi : un nœud qui suit le sous-arbre
    de l'élément est dessiné par-dessus (ex. le pavé du composeur sur le bas du journal d'appels).
    Toucher le centre d'une ligne cachée viserait en réalité le panneau au-dessus.
    """
    x1, y1, x2, y2 = bounds
    for index, (ox1, oy1, ox2, oy2) in occluders:
        if index <= subtree_end or ox2 <= x1 or ox1 >= x2 or oy2 <= y1 or oy1 >= y2:
            continue
        if ox1 <= x1 and ox2 >= x2:  # bande sur toute la largeur
            if oy1 <= y1 and oy2 >= y2:
                return None
            if oy1 > y1:
                y2 = oy1
            else:
                y1 = oy2
        elif oy1 <= y1 and oy2 >= y2:  # bande sur toute la hauteur
            if ox1 > x1:
                x2 = ox1
            else:
                x1 = ox2
        elif ox1 <= (x1 + x2) / 2 <= ox2 and oy1 <= (y1 + y2) / 2 <= oy2:  # coin qui cache le centre
            if ox1 - x1 >= x2 - ox2:
                x2 = ox1
            else:
                x1 = ox2
        if x2 - x1 < MIN_VISIBLE or y2 - y1 < MIN_VISIBLE:
            return None
    return x1, y1, x2, y2


def kind_of(node: Node) -> str:
    cls = node.cls
    if node.editable:
        return "champ"
    if node.flag("checkable"):
        if "Switch" in cls or "Toggle" in cls:
            return "interrupteur"
        return "choix" if "Radio" in cls else "case"
    if node.clickable:
        return "bouton"
    if node.flag("scrollable"):
        return "liste"
    if "Image" in cls:
        return "image"
    return "texte"


def flags_of(node: Node) -> list[str]:
    flags = []
    if node.flag("checkable"):
        flags.append("coché" if node.flag("checked") else "non coché")
    if node.flag("selected"):
        flags.append("sélectionné")
    if node.attrs.get("enabled") == "false":
        flags.append("inactif")
    if node.flag("password"):
        flags.append("mot de passe")
    if node.flag("focused") and node.editable:
        flags.append("curseur ici")
    if node.flag("scrollable"):
        flags.append("défilable")
    return flags


def extract_elements(nodes: list[Node], screen_w: int, screen_h: int, limit: int = 90) -> list[Element]:
    screen_area = screen_w * screen_h
    position = {id(n): i for i, n in enumerate(nodes)}

    def subtree_end(n: Node) -> int:
        return subtree_end(n.children[-1]) if n.children else position[id(n)]

    absorbers: set[int] = set()
    occluders: list[tuple[int, tuple[int, int, int, int]]] = []
    for node in nodes:
        b = node_bounds(node, screen_w, screen_h)
        if not (node.actionable and b):
            continue
        area = (b[2] - b[0]) * (b[3] - b[1])
        if area <= ABSORB_MAX_AREA * screen_area:
            absorbers.add(id(node))
        if area <= OCCLUDER_MAX_AREA * screen_area:
            occluders.append((position[id(node)], b))

    def absorbed(node: Node) -> bool:
        p = node.parent
        while p is not None:
            if id(p) in absorbers:
                return True
            p = p.parent
        return False

    found: list[tuple[bool, Element]] = []
    for node in nodes:
        b = node_bounds(node, screen_w, screen_h)
        if b is not None:
            b = visible_part(b, subtree_end(node), occluders)
        if b is None:
            continue
        own = node.own_label
        if node.actionable:
            absorber = id(node) in absorbers
            if not absorber and not own:
                continue  # grand conteneur cliquable sans nom : inutile à viser, ses enfants sont listés
            # Un libellé propre déjà long (description d'accessibilité complète) se suffit.
            extra = descendant_text(node, own) if absorber and len(own) < 40 else ""
            label = clean(f"{own} · {extra}") if own and extra else (own or extra)
            if not label:
                if has_actionable_descendant(node):
                    continue  # simple enveloppe de boutons : ce sont eux qu'on vise
                label = clean(node.attrs.get("hint", "")) or short_id(node) or node.cls.rsplit(".", 1)[-1]
        elif node.flag("scrollable"):
            label = own or short_id(node) or "zone défilable"
        elif own and not absorbed(node):
            label = clean(own, TEXT_MAX)
        else:
            continue
        el = Element(0, kind_of(node), label, b, flags_of(node), node.attrs.get("package", ""))
        found.append((node.actionable or node.flag("scrollable"), el))

    # Doublons : même libellé, même centre (conteneur cliquable et enfant cliquable superposés).
    unique: list[tuple[bool, Element]] = []
    for actionable, el in found:
        cx, cy = el.center
        if any(o.label == el.label and abs(o.center[0] - cx) <= 12 and abs(o.center[1] - cy) <= 12
               for _, o in unique):
            continue
        unique.append((actionable, el))

    if len(unique) > limit:
        keep = [e for a, e in unique if a][:limit]
        keep += [e for a, e in unique if not a][:limit - len(keep)]
    else:
        keep = [e for _, e in unique]
    keep.sort(key=lambda e: (e.bounds[1], e.bounds[0]))
    for i, el in enumerate(keep, 1):
        el.number = i
    return keep


def short_id(node: Node) -> str:
    rid = node.attrs.get("resource-id", "")
    return rid.split(":id/", 1)[-1].replace("_", " ") if rid else ""


def format_elements(elements: list[Element], scale: float) -> str:
    lines = []
    for el in elements:
        cx, cy = el.center
        flags = f" [{', '.join(el.flags)}]" if el.flags else ""
        lines.append(f"[{el.number}] {el.kind} « {el.label} » ({round(cx * scale)},{round(cy * scale)}){flags}")
    return "\n".join(lines)


def find_by_text(elements: list[Element], query: str) -> list[Element]:
    """Meilleures correspondances : libellé exact, puis début, puis contenu ; actionnables d'abord."""
    q = fold(query.strip())
    if not q:
        return []
    for test in (lambda lab: lab == q, lambda lab: lab.startswith(q), lambda lab: q in lab):
        hits = [e for e in elements if test(fold(e.label))]
        if hits:
            hits.sort(key=lambda e: e.kind in ("texte", "image", "liste"))
            return hits
    return []
