"""Fixtures for the 0.5.0 additions (build / diagnose / section / measure / framing / geometry diff).

The original ``make_fixtures.py`` is tied to ``Cutter.3mf`` and its object ids.
This companion records the new tools from any project file, so the JS output
schemas can be checked without that sample.

Usage:
    python selftest/make_fixtures_v05.py --file H:/bambu/CardBrickH.3mf
    node selftest/schema_contract.mjs selftest/fixtures_v05
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from threemf import cli  # noqa: E402


def write(out_dir: str, tool: str, case: str, value: dict) -> None:
    payload = {"tool": tool, "case": case, "value": value}
    name = f"{tool.replace('bambu3mf_', '')}__{case}.json"
    with open(os.path.join(out_dir, name), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    print(f"  wrote {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=r"H:\bambu\CardBrickH.3mf")
    parser.add_argument("--out", default=os.path.join(ROOT, "selftest", "fixtures_v05"))
    parser.add_argument("--workdir", default=os.path.join(ROOT, "..", "3mf-sandbox", "fixtures_v05"))
    args = parser.parse_args()
    if not os.path.isfile(args.file):
        print(f"sample not found: {args.file}")
        return 2
    os.makedirs(args.out, exist_ok=True)
    workdir = os.path.abspath(args.workdir)
    os.makedirs(workdir, exist_ok=True)
    work = os.path.join(workdir, "fixture_v05.3mf")
    shutil.copy2(args.file, work)

    info = cli.cmd_inspect({"file": work, "detail": "full"})
    write(args.out, "bambu3mf_inspect", "full_v05", info)
    first = info["objects"][0]
    object_id = first["id"]
    bbox = first["bbox"]
    centre = [(bbox[index] + bbox[index + 3]) / 2.0 for index in range(3)]
    span = [max(bbox[index + 3] - bbox[index], 1.0) for index in range(3)]

    write(args.out, "bambu3mf_mesh", "diagnose", cli.cmd_mesh({"file": work, "object": object_id, "op": "diagnose"}))
    write(args.out, "bambu3mf_mesh", "analyze", cli.cmd_mesh({"file": work, "object": object_id, "op": "analyze"}))
    write(
        args.out,
        "bambu3mf_mesh",
        "section",
        cli.cmd_mesh(
            {
                "file": work,
                "object": object_id,
                "op": "section",
                "plane": {"axis": "z", "value": centre[2]},
            }
        ),
    )
    write(
        args.out,
        "bambu3mf_mesh",
        "measure",
        cli.cmd_mesh(
            {
                "file": work,
                "object": object_id,
                "op": "measure",
                "bbox": [
                    centre[0] - span[0] / 4,
                    centre[1] - span[1] / 4,
                    bbox[2],
                    centre[0] + span[0] / 4,
                    centre[1] + span[1] / 4,
                    bbox[5],
                ],
                "axis": "z",
                "mode": "heightmap",
                "grid": 8,
            }
        ),
    )
    from threemf.render import cmd_render

    rendered = cmd_render(
        {"file": work, "views": ["iso"], "width": 300, "framing": "objects", "zoom": 1.2},
        {},
    )
    rendered.setdefault("images", [])
    write(args.out, "bambu3mf_render", "framing", rendered)
    write(args.out, "bambu3mf_diff", "geometry", cli.cmd_diff({"file": work, "against": work, "geometry": True}))

    write(
        args.out,
        "bambu3mf_build",
        "split",
        cli.cmd_build(
            {
                "file": work,
                "dry_run": True,
                "ops": [
                    {
                        "op": "split",
                        "object": object_id,
                        "plane": {"axis": "x", "value": centre[0]},
                        "gap": 0.4,
                    }
                ],
            }
        ),
    )
    write(
        args.out,
        "bambu3mf_build",
        "split_output",
        cli.cmd_build(
            {
                "file": work,
                "output": os.path.join(workdir, "fixture_split.3mf"),
                "ops": [
                    {
                        "op": "split",
                        "object": object_id,
                        "plane": {"axis": "x", "value": centre[0]},
                        "gap": 0.4,
                    }
                ],
            }
        ),
    )
    write(
        args.out,
        "bambu3mf_build",
        "boolean",
        cli.cmd_build(
            {
                "file": work,
                "dry_run": True,
                "ops": [
                    {
                        "op": "boolean",
                        "object": object_id,
                        "operation": "subtract",
                        "with": {
                            "primitive": {
                                "shape": "box",
                                "size": [min(span[0] / 4, 5), min(span[1] / 4, 5), span[2]],
                                "center": [centre[0], centre[1], centre[2]],
                            }
                        },
                    }
                ],
            }
        ),
    )
    write(
        args.out,
        "bambu3mf_build",
        "primitive",
        cli.cmd_build(
            {
                "file": work,
                "dry_run": True,
                "ops": [
                    {
                        "op": "primitive",
                        "primitive": {
                            "shape": "cylinder",
                            "radius": 4,
                            "height": 8,
                            "center": [centre[0] + span[0], centre[1], 4],
                        },
                        "name": "fixture_pin",
                    }
                ],
            }
        ),
    )
    stl = os.path.join(workdir, "fixture_v05.stl")
    cli.cmd_mesh({"file": work, "object": object_id, "op": "export_stl", "stl": stl})
    write(
        args.out,
        "bambu3mf_build",
        "replace",
        cli.cmd_build(
            {
                "file": work,
                "dry_run": True,
                "ops": [{"op": "replace", "object": object_id, "source": {"stl": stl}}],
            }
        ),
    )
    print(f"fixtures written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
