#!/usr/bin/env python3


from __future__ import annotations

import argparse
import base64
import json
import math
import struct
import sys
import uuid
from pathlib import Path

KEY_BANK = "MelvorBase:Bank"
KEY_GP = "MelvorBase:GoldPieces"
KEY_SLAYER = "MelvorBase:SlayerCoins"
INT32_MAX = 2_147_483_647
INT64_MAX = 9_223_372_036_854_775_807
LEVEL_CAP = 99
XP_BASE = 2 ** (1 / 7)

try:
    import brotli
except ImportError:
    try:
        import brotlicffi as brotli
    except ImportError:
        sys.exit(
            "This script needs the brotli package.\n"
            "Install it with:\n"
            "  python3 -m venv .venv\n"
            "  .venv/bin/pip install brotli\n"
            "Then run:  .venv/bin/python melvor2saveeditor.py ..."
        )


def build_xp_table() -> list[int]:
    table = [0]
    total = 0
    for i in range(1, 100):
        total += math.floor(i + 300 * XP_BASE**i)
        table.append(math.floor(0.25 * total))
    return table


XP_TABLE = build_xp_table()


def level_from_xp(xp: float) -> int:
    if xp <= 0:
        return 1
    level = 1
    while level < LEVEL_CAP and xp >= XP_TABLE[level]:
        level += 1
    return level


class Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def need(self, n: int) -> None:
        if self.pos + n > len(self.data):
            raise ValueError(f"unexpected end of data at byte {self.pos}")

    def i32(self) -> int:
        self.need(4)
        value = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return value

    def i64(self) -> int:
        self.need(8)
        value = struct.unpack_from("<q", self.data, self.pos)[0]
        self.pos += 8
        return value

    def f64(self) -> float:
        self.need(8)
        value = struct.unpack_from("<d", self.data, self.pos)[0]
        self.pos += 8
        return value

    def bool(self) -> bool:
        self.need(1)
        value = self.data[self.pos] != 0
        self.pos += 1
        return value

    def raw(self, n: int) -> bytes:
        self.need(n)
        value = self.data[self.pos : self.pos + n]
        self.pos += n
        return value

    def string(self) -> str:
        length = 0
        shift = 0
        while True:
            self.need(1)
            byte = self.data[self.pos]
            self.pos += 1
            length |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        return self.raw(length).decode("utf-8")


class Writer:
    def __init__(self):
        self.buf = bytearray()

    def i32(self, value: int) -> None:
        self.buf += struct.pack("<i", value)

    def i64(self, value: int) -> None:
        self.buf += struct.pack("<q", value)

    def f64(self, value: float) -> None:
        self.buf += struct.pack("<d", value)

    def bool(self, value: bool) -> None:
        self.buf.append(1 if value else 0)

    def bytes(self, value: bytes) -> None:
        self.buf += value

    def string(self, value: str) -> None:
        encoded = value.encode("utf-8")
        length = len(encoded)
        while True:
            byte = length & 0x7F
            length >>= 7
            if length:
                byte |= 0x80
            self.buf.append(byte)
            if not length:
                break
        self.buf += encoded

    def region(self, write) -> None:
        at = len(self.buf)
        self.i32(0)
        write()
        struct.pack_into("<i", self.buf, at, len(self.buf) - at - 4)

    def finish(self) -> bytes:
        return bytes(self.buf)


def decompress_save(raw: bytes) -> bytes:
    try:
        return brotli.decompress(raw)
    except Exception:
        pass
    try:
        return brotli.decompress(raw + b"\x03")
    except Exception as exc:
        raise ValueError(
            "could not decompress the save; the string is corrupt or incomplete"
        ) from exc


def compress_save(body: bytes) -> bytes:
    return brotli.compress(body, quality=9)


def parse_header(reader: Reader) -> dict:
    uuid_bytes = reader.raw(16)
    return {
        "uuid": uuid_bytes,
        "uuid_string": str(uuid.UUID(bytes_le=uuid_bytes)),
        "character_name": reader.string(),
        "gamemode": reader.string(),
        "timestamp": reader.i64(),
        "active_entity": reader.string(),
        "active_entity_icon": reader.string(),
        "active_action": reader.string(),
        "active_action_icon": reader.string(),
        "total_level": reader.i32(),
        "gp": reader.i64(),
        "slayer_coins": reader.i64(),
        "demo": reader.bool(),
    }


def write_header(writer: Writer, header: dict) -> None:
    writer.bytes(header["uuid"])
    writer.string(header["character_name"])
    writer.string(header["gamemode"])
    writer.i64(header["timestamp"])
    writer.string(header["active_entity"])
    writer.string(header["active_entity_icon"])
    writer.string(header["active_action"])
    writer.string(header["active_action_icon"])
    writer.i32(header["total_level"])
    writer.i64(header["gp"])
    writer.i64(header["slayer_coins"])
    writer.bool(header["demo"])


def parse_experience(raw: bytes, version: int) -> dict | None:
    reader = Reader(raw)
    xp = reader.f64()
    if version < 29:
        level = reader.i32()
    else:
        level = level_from_xp(xp)
    if reader.pos != len(raw):
        return None
    return {"xp": xp, "level": level}


def encode_experience(writer: Writer, data: dict, version: int) -> None:
    writer.f64(float(data["xp"]))
    if version < 29:
        writer.i32(int(data["level"]))


def parse_wallet(raw: bytes) -> dict | None:
    reader = Reader(raw)
    stacks = []
    for _ in range(reader.i32()):
        stacks.append({"quantity": reader.i64(), "currency": reader.string()})
    if reader.pos != len(raw):
        return None
    return {"stacks": stacks}


def encode_wallet(writer: Writer, data: dict) -> None:
    stacks = data["stacks"]
    writer.i32(len(stacks))
    for stack in stacks:
        writer.i64(int(stack["quantity"]))
        writer.string(str(stack["currency"]))


def parse_inventory(raw: bytes) -> dict | None:
    reader = Reader(raw)
    inventory = {"default_tab": reader.i32(), "tabs": []}
    for _ in range(reader.i32()):
        tab = {
            "max_stacks": reader.i32(),
            "min_stack_quantity": reader.i32(),
            "max_stack_quantity": reader.i32(),
            "allow_stack_combining": reader.bool(),
            "allow_same_item_stacks": reader.bool(),
            "stacks": [],
            "tags": [],
        }
        for _ in range(reader.i32()):
            tab["stacks"].append(
                {
                    "quantity": reader.i32(),
                    "placeholder": reader.bool(),
                    "locked": reader.bool(),
                    "item": reader.string(),
                }
            )
        tab["label"] = reader.string()
        for _ in range(reader.i32()):
            tab["tags"].append(
                {
                    "is_product": reader.bool(),
                    "is_ingredient": reader.bool(),
                    "bank_tag": reader.string(),
                }
            )
        inventory["tabs"].append(tab)
    inventory["rest"] = raw[reader.pos :]
    return inventory


def encode_inventory(writer: Writer, data: dict) -> None:
    writer.i32(int(data["default_tab"]))
    writer.i32(len(data["tabs"]))
    for tab in data["tabs"]:
        writer.i32(int(tab["max_stacks"]))
        writer.i32(int(tab["min_stack_quantity"]))
        writer.i32(int(tab["max_stack_quantity"]))
        writer.bool(bool(tab["allow_stack_combining"]))
        writer.bool(bool(tab["allow_same_item_stacks"]))
        writer.i32(len(tab["stacks"]))
        for stack in tab["stacks"]:
            writer.i32(int(stack["quantity"]))
            writer.bool(bool(stack["placeholder"]))
            writer.bool(bool(stack["locked"]))
            writer.string(str(stack["item"]))
        writer.string(str(tab["label"]))
        writer.i32(len(tab["tags"]))
        for tag in tab["tags"]:
            writer.bool(bool(tag["is_product"]))
            writer.bool(bool(tag["is_ingredient"]))
            writer.string(str(tag["bank_tag"]))
    writer.bytes(data["rest"])


def parse_pets(raw: bytes) -> list[dict] | None:
    reader = Reader(raw)
    pets = []
    for _ in range(reader.i32()):
        pet_id = reader.string()
        region_length = reader.i32()
        end = reader.pos + region_length
        if end > len(raw):
            return None
        pets.append({"id": pet_id, "unlocked": reader.bool()})
        reader.pos = end
    return pets if reader.pos == len(raw) else None


def decode_component(entity_id: str, component_id: str, raw: bytes, version: int):
    try:
        if component_id == "Experience":
            return parse_experience(raw, version)
        if entity_id == KEY_BANK and component_id == "Wallet":
            return parse_wallet(raw)
        if entity_id == KEY_BANK and component_id == "Inventory":
            return parse_inventory(raw)
        if component_id == "Pets":
            return parse_pets(raw)
    except (ValueError, struct.error, UnicodeDecodeError):
        return None
    return None


def can_encode(entity_id: str, component_id: str) -> bool:
    return component_id == "Experience" or (
        entity_id == KEY_BANK and component_id in ("Wallet", "Inventory")
    )


def encode_component(writer: Writer, entity_id: str, component_id: str, data: dict, version: int) -> None:
    if component_id == "Experience":
        encode_experience(writer, data, version)
    elif entity_id == KEY_BANK and component_id == "Wallet":
        encode_wallet(writer, data)
    elif entity_id == KEY_BANK and component_id == "Inventory":
        encode_inventory(writer, data)
    else:
        raise ValueError(f"no encoder for {entity_id}/{component_id}")


def parse_save(body: bytes) -> dict:
    reader = Reader(body)
    version = reader.i32()
    header = parse_header(reader)
    entities = []
    for _ in range(reader.i32()):
        entity_id = reader.string()
        region_length = reader.i32()
        entity_end = reader.pos + region_length
        if entity_end > len(body):
            raise ValueError(f"entity {entity_id} overruns the save")
        components = []
        for _ in range(reader.i32()):
            component_id = reader.string()
            component_length = reader.i32()
            raw = reader.raw(component_length)
            components.append(
                {
                    "id": component_id,
                    "raw": raw,
                    "data": decode_component(entity_id, component_id, raw, version),
                }
            )
        if reader.pos > entity_end:
            raise ValueError(f"entity {entity_id} overruns its region")
        tail = reader.raw(entity_end - reader.pos)
        entities.append({"id": entity_id, "components": components, "tail": tail})
    if reader.pos > len(body):
        raise ValueError("trailing data overruns the save")
    trailing = reader.raw(len(body) - reader.pos)
    return {
        "version": version,
        "header": header,
        "entities": entities,
        "trailing": trailing,
    }


def serialize_save(save: dict) -> bytes:
    writer = Writer()
    version = save["version"]
    writer.i32(version)
    write_header(writer, save["header"])
    writer.i32(len(save["entities"]))
    for entity in save["entities"]:
        writer.string(entity["id"])

        def write_entity(entity=entity):
            writer.i32(len(entity["components"]))
            for component in entity["components"]:
                writer.string(component["id"])

                def write_component(component=component, entity=entity):
                    if component["data"] is not None and can_encode(entity["id"], component["id"]):
                        encode_component(
                            writer, entity["id"], component["id"], component["data"], version
                        )
                    else:
                        writer.bytes(component["raw"])

                writer.region(write_component)
            writer.bytes(entity["tail"])

        writer.region(write_entity)
    writer.bytes(save["trailing"])
    return writer.finish()


def format_number(value: int | float) -> str:
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,.2f}"
    return f"{value:,}"


def short_id(entity_id: str) -> str:
    return entity_id.removeprefix("MelvorBase:")


def get_skills(save: dict) -> list[tuple[str, dict]]:
    result = []
    for entity in save["entities"]:
        for component in entity["components"]:
            if component["id"] == "Experience" and component["data"] is not None:
                result.append((entity["id"], component["data"]))
    return result


def get_bank_component(save: dict, component_id: str):
    for entity in save["entities"]:
        if entity["id"] == KEY_BANK:
            for component in entity["components"]:
                if component["id"] == component_id:
                    return component["data"]
    return None


def get_pets(save: dict) -> list[dict] | None:
    result = []
    for entity in save["entities"]:
        for component in entity["components"]:
            if component["id"] == "Pets" and isinstance(component["data"], list):
                result.extend(component["data"])
    return result or None


def load_game_data():
    path = Path(__file__).with_name("game-data.json")
    if not path.exists():
        return {}, {}, {}, {}
    data = json.loads(path.read_text(encoding="utf-8"))
    items = {item["id"]: item["name"] for item in data.get("items", [])}
    currencies = {item["id"]: item["name"] for item in data.get("currencies", [])}
    item_names = {name.lower(): item_id for item_id, name in items.items()}
    currency_names = {name.lower(): item_id for item_id, name in currencies.items()}
    return items, currencies, item_names, currency_names


def build_decoded_text(save: dict, original_string: str) -> str:
    header = save["header"]
    wallet = get_bank_component(save, "Wallet")
    inventory = get_bank_component(save, "Inventory")

    lines = [
        "[header]",
        f"character_name = {header['character_name']}",
        f"gamemode = {header['gamemode']}",
        "",
        "[currencies]",
    ]
    if wallet:
        for stack in wallet["stacks"]:
            lines.append(f"{stack['currency']} = {stack['quantity']}")
    lines.extend(["", "[skills]"])
    for entity_id, data in get_skills(save):
        name = short_id(entity_id)
        lines.append(f"{name}.level = {data['level']}")
        lines.append(f"{name}.xp = {data['xp']!r}")
    lines.extend(["", "[bank]"])
    if inventory:
        for tab in inventory["tabs"]:
            for stack in tab["stacks"]:
                lines.append(f"{stack['item']} = {stack['quantity']}")
    lines.extend(["", "[original]", f"save_string = {original_string}", ""])
    return "\n".join(lines)


def parse_decoded_text(text: str) -> dict:
    sections: dict[str, dict[str, str]] = {}
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip().lower()
            sections.setdefault(current, {})
            continue
        if current is None or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if " #" in value:
            value = value.split(" #", 1)[0].strip()
        sections[current][key] = value
    return sections


def parse_int(value: str) -> int:
    text = value.replace(",", "").replace("_", "").strip()
    try:
        return int(text)
    except ValueError:
        return int(float(text))


def parse_float(value: str) -> float:
    return float(value.replace(",", "").replace("_", "").strip())


def resolve_currency_id(key: str, currencies: dict[str, str], names: dict[str, str]) -> str:
    if key in currencies:
        return key
    return names.get(key.lower(), key)


def resolve_item_id(key: str, items: dict[str, str], names: dict[str, str]) -> str:
    if key in items:
        return key
    return names.get(key.lower(), key)


def find_skill(save: dict, name: str) -> dict:
    wanted = name.lower()
    candidates = []
    for entity in save["entities"]:
        if any(c["id"] == "Experience" and c["data"] is not None for c in entity["components"]):
            candidates.append(entity)
    for entity in candidates:
        if entity["id"].lower() == wanted or short_id(entity["id"]).lower() == wanted:
            for component in entity["components"]:
                if component["id"] == "Experience":
                    return component["data"]
    raise ValueError(
        "unknown skill in [skills]: "
        + name
        + " (known: "
        + ", ".join(short_id(e["id"]) for e in candidates)
        + ")"
    )


def apply_edits(save: dict, sections: dict) -> None:
    header = save["header"]
    items, currencies, item_name_map, currency_name_map = load_game_data()

    header_edits = sections.get("header", {})
    if "character_name" in header_edits:
        header["character_name"] = header_edits["character_name"].strip().strip('"')
    if "gamemode" in header_edits:
        header["gamemode"] = header_edits["gamemode"].strip().strip('"')

    wallet = get_bank_component(save, "Wallet")
    if wallet is not None:
        for key, value in sections.get("currencies", {}).items():
            currency_id = resolve_currency_id(key, currencies, currency_name_map)
            quantity = parse_int(value)
            if quantity < 0 or quantity > INT64_MAX:
                raise ValueError(f"invalid currency quantity for {currency_id}: {value}")
            stack = next((s for s in wallet["stacks"] if s["currency"] == currency_id), None)
            if stack is None:
                wallet["stacks"].append({"quantity": quantity, "currency": currency_id})
            else:
                stack["quantity"] = quantity

    skill_levels: dict[str, str] = {}
    skill_xps: dict[str, str] = {}
    for key, value in sections.get("skills", {}).items():
        if key.endswith(".level"):
            skill_levels[key[: -len(".level")]] = value
        elif key.endswith(".xp"):
            skill_xps[key[: -len(".xp")]] = value
        else:
            skill_levels[key] = value
    for name in sorted(set(skill_levels) | set(skill_xps)):
        data = find_skill(save, name)
        original_level = int(data["level"])
        original_xp = float(data["xp"])
        new_xp = original_xp
        if name in skill_levels:
            level = max(1, min(LEVEL_CAP, int(parse_float(skill_levels[name]))))
            if level != original_level:
                new_xp = XP_TABLE[level - 1]
            elif name in skill_xps:
                xp = parse_float(skill_xps[name])
                if xp != original_xp:
                    new_xp = xp
        elif name in skill_xps:
            new_xp = parse_float(skill_xps[name])
        if new_xp < 0:
            raise ValueError(f"invalid XP for {name}: {new_xp}")
        data["xp"] = new_xp
        data["level"] = level_from_xp(new_xp)

    if "bank" in sections:
        inventory = get_bank_component(save, "Inventory")
        if inventory is None:
            raise ValueError("save has no bank Inventory component")
        wanted: dict[str, int] = {}
        for key, value in sections["bank"].items():
            item_id = resolve_item_id(key, items, item_name_map)
            quantity = parse_int(value)
            if quantity < 0:
                raise ValueError(f"bank quantity cannot be negative: {item_id}")
            wanted[item_id] = min(INT32_MAX, quantity)
        seen: set[str] = set()
        for tab in inventory["tabs"]:
            kept = []
            for stack in tab["stacks"]:
                item_id = stack["item"]
                if item_id in wanted and wanted[item_id] > 0 and item_id not in seen:
                    stack["quantity"] = wanted[item_id]
                    kept.append(stack)
                    seen.add(item_id)
            tab["stacks"] = kept
        if inventory["tabs"]:
            default_tab = max(0, min(len(inventory["tabs"]) - 1, inventory["default_tab"]))
        else:
            inventory["tabs"].append(
                {
                    "max_stacks": INT32_MAX,
                    "min_stack_quantity": 0,
                    "max_stack_quantity": INT32_MAX,
                    "allow_stack_combining": False,
                    "allow_same_item_stacks": False,
                    "stacks": [],
                    "label": "Tab 1",
                    "tags": [],
                }
            )
            default_tab = 0
        for item_id, quantity in wanted.items():
            if quantity > 0 and item_id not in seen:
                inventory["tabs"][default_tab]["stacks"].append(
                    {
                        "quantity": quantity,
                        "placeholder": False,
                        "locked": False,
                        "item": item_id,
                    }
                )

    total_level = 0
    for entity_id, data in get_skills(save):
        data["level"] = level_from_xp(data["xp"])
        total_level += data["level"]
    header["total_level"] = total_level
    if wallet is not None:
        gp_stack = next((s for s in wallet["stacks"] if s["currency"] == KEY_GP), None)
        slayer_stack = next((s for s in wallet["stacks"] if s["currency"] == KEY_SLAYER), None)
        if gp_stack is not None:
            header["gp"] = gp_stack["quantity"]
        if slayer_stack is not None:
            header["slayer_coins"] = slayer_stack["quantity"]


def normalize_save_string(text: str) -> str:
    return "".join(text.strip().strip("\"'").split())


def decode_string_to_body(text: str) -> tuple[bytes, bytes]:
    normalized = normalize_save_string(text)
    if not normalized:
        raise ValueError("the save string is empty")
    try:
        compressed = base64.b64decode(normalized + "=" * (-len(normalized) % 4), validate=True)
    except Exception as exc:
        raise ValueError(f"input is not valid base64: {exc}") from exc
    return compressed, decompress_save(compressed)


def read_source(source: str) -> tuple[bytes, str, str]:
    path = Path(source)
    if path.exists():
        if path.suffix.lower() in (".sav", ".br"):
            compressed = path.read_bytes()
            return compressed, base64.b64encode(compressed).decode("ascii"), str(path)
        text = path.read_text(encoding="utf-8")
        label = str(path)
    else:
        text = source
        label = "<command line>"
    normalized = normalize_save_string(text)
    compressed = base64.b64decode(
        normalized + "=" * (-len(normalized) % 4), validate=True
    )
    return compressed, normalized, label


def looks_like_decoded(text: str) -> bool:
    return "[original]" in text and "save_string" in text


def decode_mode(source: str, output: str, raw_output: str | None, sav_output: str | None) -> None:
    compressed, original_string, label = read_source(source)
    body = decompress_save(compressed)
    save = parse_save(body)
    text = build_decoded_text(save, original_string)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(text, encoding="utf-8")
    if raw_output:
        Path(raw_output).parent.mkdir(parents=True, exist_ok=True)
        Path(raw_output).write_bytes(body)
    if sav_output:
        Path(sav_output).parent.mkdir(parents=True, exist_ok=True)
        Path(sav_output).write_bytes(compress_save(body))
    print(f"wrote {output}")
    print(
        f"{save['header']['character_name']} ({save['header']['gamemode']}), "
        f"version {save['version']}, total level {save['header']['total_level']}, "
        f"{len(save['entities'])} entities"
    )
    print("edit it, then run: python3 melvor2saveeditor.py --encode " + output)


def encode_mode(source: str, output: str, sav_output: str | None) -> None:
    text = Path(source).read_text(encoding="utf-8")
    sections = parse_decoded_text(text)
    original = sections.get("original", {}).get("save_string")
    if not original:
        raise ValueError(
            "decoded.txt is missing the [original] save_string block; "
            "decode the save string again"
        )
    compressed, body = decode_string_to_body(original)
    save = parse_save(body)
    apply_edits(save, sections)
    new_body = serialize_save(save)
    new_compressed = compress_save(new_body)
    new_string = base64.b64encode(new_compressed).decode("ascii")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(new_string + "\n", encoding="ascii")
    if sav_output:
        Path(sav_output).parent.mkdir(parents=True, exist_ok=True)
        Path(sav_output).write_bytes(new_compressed)

    check = parse_save(decompress_save(new_compressed))
    if check["header"]["character_name"] != save["header"]["character_name"]:
        raise ValueError("verification failed after encoding")
    print(f"wrote {output}")
    print(
        f"{check['header']['character_name']} ({check['header']['gamemode']}), "
        f"version {check['version']}, total level {check['header']['total_level']}, "
        f"gold {format_number(check['header']['gp'])}, "
        f"{len(check['entities'])} entities"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Decode a Melvor Idle 2 save string, or encode an edited decoded.txt"
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="save string file / .sav (decode) or decoded.txt (encode)",
    )
    parser.add_argument("-o", "--output", help="output file")
    parser.add_argument(
        "--encode", action="store_true", help="re-encode an edited decoded.txt"
    )
    parser.add_argument(
        "--raw", metavar="FILE", help="decode mode: also write the raw decoded body"
    )
    parser.add_argument(
        "--sav", metavar="FILE", help="also write a Brotli-compressed .sav file"
    )
    args = parser.parse_args()

    try:
        if args.encode:
            source = args.input or "decoded.txt"
            output = args.output or "encoded.txt"
            encode_mode(source, output, args.sav)
            return

        source = args.input or "save_string.txt"
        input_path = Path(source)
        if input_path.exists() and input_path.suffix.lower() not in (".sav", ".br"):
            text = input_path.read_text(encoding="utf-8")
            if looks_like_decoded(text):
                output = args.output or "encoded.txt"
                print("input looks like decoded.txt; encoding")
                encode_mode(source, output, args.sav)
                return
        output = args.output or "decoded.txt"
        decode_mode(source, output, args.raw, args.sav)
    except (OSError, ValueError, IndexError, KeyError, struct.error) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
