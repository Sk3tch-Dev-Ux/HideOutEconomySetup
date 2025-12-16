import json
import re
import sys
from pathlib import Path


def parse_units(config_text: str):
    """
    Find the units[] array in CfgPatches and return a list of classnames.
    Example:
        units[] = {"FOG_Vest_FCPC_RG","FOG_Vest_FCPC_CB", ...};
    """
    match = re.search(r'units\[\]\s*=\s*\{([^}]*)\};', config_text, re.DOTALL)
    if not match:
        raise ValueError("Could not find units[] array in config.cpp")

    inner = match.group(1)
    raw_items = inner.split(',')
    units = []

    for item in raw_items:
        item = item.strip()
        if not item:
            continue
        item = item.strip('"').strip("'")
        if item:
            units.append(item)

    if not units:
        raise ValueError("Parsed units[] array, but it appears to be empty")

    return units


def parse_all_classes(text: str, start_pos: int = 0, depth_limit: int = 20):
    """
    Recursively parse 'class X[: Y] { ... };' constructs in the given text.

    Returns:
        child_parent: dict[child_name] = parent_name
        bodies:       dict[class_name] = body_text
    """
    child_parent = {}
    bodies = {}

    pos = start_pos
    length = len(text)

    while True:
        idx = text.find("class ", pos)
        if idx == -1:
            break

        # Parse class name
        name_start = idx + len("class ")
        while name_start < length and text[name_start].isspace():
            name_start += 1

        name_end = name_start
        while name_end < length and (text[name_end].isalnum() or text[name_end] == "_"):
            name_end += 1

        class_name = text[name_start:name_end]
        if not class_name:
            pos = name_end
            continue

        # Locate ':' (parent) or '{' (body)
        colon_pos = text.find(":", name_end)
        brace_pos = text.find("{", name_end)

        parent_name = None
        if colon_pos != -1 and (brace_pos == -1 or colon_pos < brace_pos):
            # There is a parent class
            parent_start = colon_pos + 1
            while parent_start < length and text[parent_start].isspace():
                parent_start += 1

            parent_end = parent_start
            while parent_end < length and (text[parent_end].isalnum() or text[parent_end] == "_"):
                parent_end += 1

            parent_name = text[parent_start:parent_end]
            brace_pos = text.find("{", parent_end)

        if brace_pos == -1:
            # No body -> forward declaration like 'class Clothing;'
            pos = name_end
            continue

        # Find matching closing brace for the class body
        depth = 1
        i = brace_pos + 1
        while i < length and depth > 0:
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1

        body = text[brace_pos + 1 : i - 1]
        bodies[class_name] = body

        if parent_name:
            child_parent[class_name] = parent_name

        # Recursively parse nested classes in the body
        if depth_limit > 0:
            nested_child_parent, nested_bodies = parse_all_classes(
                body, 0, depth_limit - 1
            )
            child_parent.update(nested_child_parent)
            bodies.update(nested_bodies)

        pos = i

    return child_parent, bodies


def parse_base_class(config_text: str, units: list[str]) -> str:
    """
    Determine the base class for this item.

    Priority:
      1. Look at all class definitions and find any class whose name is in units[]
         and that extends a parent -> use that parent as base candidate.
         Prefer parents that have 'scope = 0;'.
      2. If that fails, look for any class with 'scope = 0;' whose name ends
         with _ColorBase or _Base.
      3. Fallback: any class *_(ColorBase|Base) : Clothing
    """
    child_parent, bodies = parse_all_classes(config_text)

    units_set = set(units)

    # 1) Parents of unit classes
    parents_for_units = {
        parent for child, parent in child_parent.items() if child in units_set
    }

    # Filter out container-y stuff like CfgPatches / CfgVehicles etc.
    def is_container_name(name: str) -> bool:
        lower = name.lower()
        return lower.startswith("cfg") or lower in ("clothingtypes",)

    parents_for_units = {p for p in parents_for_units if not is_container_name(p)}

    if parents_for_units:
        # Among these parents, prefer those that have scope = 0;
        scope0_parents = {
            p
            for p in parents_for_units
            if p in bodies and re.search(r"\bscope\s*=\s*0\s*;", bodies[p])
        }

        if scope0_parents:
            # Deterministic pick
            return sorted(scope0_parents)[0]

        return sorted(parents_for_units)[0]

    # 2) No parent found via units -> look for scope=0 *_Base / *_ColorBase
    scope0_classes = [
        name
        for name, body in bodies.items()
        if re.search(r"\bscope\s*=\s*0\s*;", body)
        and not is_container_name(name)
    ]

    preferred = [
        n
        for n in scope0_classes
        if n.lower().endswith("_colorbase") or n.lower().endswith("_base")
    ]

    if preferred:
        return preferred[0]

    if scope0_classes:
        return scope0_classes[0]

    # 3) Final fallback: look for *_ColorBase / *_Base extending Clothing in whole file
    match = re.search(
        r"class\s+([A-Za-z0-9_]+_(?:ColorBase|Base))\s*:\s*Clothing",
        config_text,
    )
    if match:
        return match.group(1)

    raise ValueError(
        "Could not determine base class. Tried parents of units[], scope=0 classes, "
        "and '*_ColorBase'/'*_Base' extending Clothing."
    )


def load_or_init_json(json_path: Path):
    if not json_path.exists():
        return {"SkinSets": []}

    with json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if "SkinSets" not in data or not isinstance(data["SkinSets"], list):
        raise ValueError("SkinsSets.json does not contain a 'SkinSets' array at the root")

    return data


def add_skinset_from_config(config_path: Path, data: dict) -> bool:
    """
    Parse a single config.cpp and add its SkinSet to `data` if not already present.

    Returns True if a new set was added, False otherwise.
    """
    config_text = config_path.read_text(encoding="utf-8", errors="ignore")

    # 1. Parse units[] and base class
    units = parse_units(config_text)
    base_class = parse_base_class(config_text, units)

    print(f"\n=== Processing {config_path} ===")
    print(f"Found base class: {base_class}")
    print(f"Found {len(units)} unit(s): {', '.join(units)}")

    # 2. Check for existing set with same Classname_Base
    for entry in data["SkinSets"]:
        if entry.get("Classname_Base") == base_class:
            print(
                f"SKIP: A set with Classname_Base '{base_class}' already exists in SkinsSets.json."
            )
            return False

    # 3. Append new set
    new_set = {
        "Classname_Base": base_class,
        "Skin_Classnames": units,
    }
    data["SkinSets"].append(new_set)

    print(f"ADDED: New skin set for base class '{base_class}'.")
    return True


def main(path_str: str, json_path_str: str):
    target_path = Path(path_str)
    json_path = Path(json_path_str)

    if not target_path.exists():
        raise FileNotFoundError(f"Path not found: {target_path}")

    # Load or init JSON once
    data = load_or_init_json(json_path)

    added_count = 0
    failed_count = 0

    if target_path.is_file():
        # Single config.cpp mode (old behavior)
        print(f"Processing single config file: {target_path}")
        try:
            if add_skinset_from_config(target_path, data):
                added_count += 1
        except Exception as e:
            failed_count += 1
            print(f"ERROR while processing {target_path}: {e}")

    elif target_path.is_dir():
        # Folder mode: find all config.cpp recursively
        print(f"Searching for config.cpp files under: {target_path}")
        config_files = list(target_path.rglob("config.cpp"))

        if not config_files:
            print("No config.cpp files found.")
        else:
            print(f"Found {len(config_files)} config.cpp file(s).")

        for cfg in config_files:
            try:
                if add_skinset_from_config(cfg, data):
                    added_count += 1
            except Exception as e:
                failed_count += 1
                print(f"ERROR while processing {cfg}: {e}")
    else:
        raise ValueError(f"Provided path is neither a file nor a directory: {target_path}")

    # Only write JSON back if we actually added something
    if added_count > 0:
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.write("\n")
        print(f"\nDone. Added {added_count} new skin set(s) to {json_path}.")
    else:
        print("\nDone. No new skin sets were added (all already present or errors).")

    if failed_count > 0:
        print(f"{failed_count} file(s) failed to process. See errors above.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(
            "Usage:\n"
            "  python make_skinsets.py <path/to/config.cpp or folder> <path/to/SkinsSets.json>\n\n"
            "Examples:\n"
            "  python make_skinsets.py \"C:\\Path\\to\\config.cpp\" \"C:\\Path\\to\\SkinsSets.json\"\n"
            "  python make_skinsets.py \"C:\\Program Files (x86)\\Steam\\steamapps\\common\\DayZServer\\@Forward Operator Gear\\Addons\\Vests\\FOG_MOD\\Vests\" \"C:\\Path\\to\\SkinsSets.json\""
        )
        sys.exit(1)

    main(sys.argv[1], sys.argv[2])
