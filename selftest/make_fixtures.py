"""Record real kernel outputs so the JS output schemas can be checked against them.

`selftest/schema_contract.mjs` validates these recordings against each tool's
declared output schema. The Python self test cannot see that layer, which is how
0.3.0 shipped an `objects[].size` key its own schema rejected.

Usage:
    python selftest/make_fixtures.py --samples D:\\下载 --out <fixtures-dir>
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
from fixture_packs import make_sliced_3mf  # noqa: E402


def write(out_dir: str, tool: str, case: str, value: dict) -> None:
    payload = {"tool": tool, "case": case, "value": value}
    name = f"{tool.replace('bambu3mf_', '')}__{case}.json"
    with open(os.path.join(out_dir, name), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    print(f"  wrote {name}")


def expect_backup(value: dict, label: str) -> None:
    """A real in_place save must return a usable backup path.

    `backup` only ever appears on that branch, which is why a dry_run-only
    fixture set let 0.3.1 ship mesh/settings without the key in their schema.
    Asserting here means the contract sample cannot silently lose the case.
    """
    backup = value.get("backup")
    if not backup or not os.path.isfile(str(backup)):
        raise SystemExit(
            f"{label}: 内核没有返回可用的 backup（{backup!r}）——"
            "schema 契约样本失去意义，先查 scene.save / cli._finish"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", default=r"D:\下载")
    parser.add_argument("--out", default=os.path.join(ROOT, "selftest", "fixtures"))
    parser.add_argument("--workdir", default=os.path.join(ROOT, "..", "3mf-sandbox", "fixtures"))
    args = parser.parse_args()

    sample = os.path.join(args.samples, "Cutter.3mf")
    if not os.path.isfile(sample):
        print(f"sample not found: {sample}")
        return 2
    os.makedirs(args.out, exist_ok=True)
    workdir = os.path.abspath(args.workdir)
    os.makedirs(workdir, exist_ok=True)
    work = os.path.join(workdir, "fixture_src.3mf")
    shutil.copy2(sample, work)

    write(args.out, "bambu3mf_inspect", "summary", cli.cmd_inspect({"file": work}))
    write(
        args.out,
        "bambu3mf_inspect",
        "full",
        cli.cmd_inspect({"file": work, "detail": "full", "settings_keys": ["wipe_tower"]}),
    )
    write(
        args.out,
        "bambu3mf_edit",
        "move",
        cli.cmd_edit({"file": work, "dry_run": True, "ops": [{"op": "move", "object": 2, "dx": 1}]}),
    )
    write(
        args.out,
        "bambu3mf_edit",
        "duplicate",
        cli.cmd_edit(
            {"file": work, "dry_run": True, "ops": [{"op": "duplicate", "object": 2, "dx": 8}]}
        ),
    )
    write(
        args.out,
        "bambu3mf_edit",
        "delete",
        cli.cmd_edit({"file": work, "dry_run": True, "ops": [{"op": "delete", "object": 6}]}),
    )
    write(
        args.out,
        "bambu3mf_edit",
        "set_position_bed",
        cli.cmd_edit(
            {
                "file": work,
                "dry_run": True,
                "ops": [{"op": "set_position", "object": 2, "z": 0, "z_mode": "bed"}],
            }
        ),
    )
    info = cli.cmd_inspect({"file": work})
    first = info["objects"][0]
    centre = (first["bbox"][0] + first["bbox"][3]) / 2.0
    write(
        args.out,
        "bambu3mf_mesh",
        "stretch",
        cli.cmd_mesh(
            {
                "file": work,
                "object": first["id"],
                "op": "stretch",
                "axis": "x",
                "band": [centre - 10, centre + 10],
                "delta": 4,
                "dry_run": True,
            }
        ),
    )
    write(
        args.out,
        "bambu3mf_mesh",
        "components",
        cli.cmd_mesh({"file": work, "object": first["id"], "op": "components"}),
    )
    write(
        args.out,
        "bambu3mf_mesh",
        "export_stl",
        cli.cmd_mesh(
            {
                "file": work,
                "object": first["id"],
                "op": "export_stl",
                "stl": os.path.join(workdir, "fixture.stl"),
            }
        ),
    )
    write(
        args.out,
        "bambu3mf_settings",
        "set",
        cli.cmd_settings(
            {"file": work, "dry_run": True, "ops": [{"op": "set", "key": "layer_height", "value": "0.24"}]}
        ),
    )
    # ---- write paths -------------------------------------------------------
    # Every case above is dry_run or read-only, so none of them can produce
    # `backup`. Record the real in_place / output branches too, each on its own
    # copy so the in_place mutations cannot leak into the next case.
    def fresh(name: str) -> str:
        target = os.path.join(workdir, name)
        shutil.copy2(sample, target)
        return target

    edit_in_place = cli.cmd_edit(
        {
            "file": fresh("case_edit_inplace.3mf"),
            "in_place": True,
            "ops": [{"op": "move", "object": 2, "dx": 1}],
        }
    )
    expect_backup(edit_in_place, "edit in_place")
    write(args.out, "bambu3mf_edit", "in_place", edit_in_place)

    write(
        args.out,
        "bambu3mf_edit",
        "output",
        cli.cmd_edit(
            {
                "file": fresh("case_edit_output.3mf"),
                "output": os.path.join(workdir, "edit_out.3mf"),
                "ops": [{"op": "move", "object": 2, "dx": 1}],
            }
        ),
    )

    mesh_in_place = cli.cmd_mesh(
        {
            "file": fresh("case_mesh_inplace.3mf"),
            "object": first["id"],
            "op": "stretch",
            "axis": "x",
            "band": [centre - 10, centre + 10],
            "delta": 4,
            "in_place": True,
        }
    )
    expect_backup(mesh_in_place, "mesh in_place")
    write(args.out, "bambu3mf_mesh", "in_place", mesh_in_place)

    settings_in_place = cli.cmd_settings(
        {
            "file": fresh("case_settings_inplace.3mf"),
            "in_place": True,
            "ops": [{"op": "set", "key": "layer_height", "value": "0.24"}],
        }
    )
    expect_backup(settings_in_place, "settings in_place")
    write(args.out, "bambu3mf_settings", "in_place", settings_in_place)

    write(
        args.out,
        "bambu3mf_mesh",
        "analyze",
        cli.cmd_mesh({"file": work, "object": first["id"], "op": "analyze"}),
    )

    # diff needs a second file: a copy with one setting changed, so the fixture
    # exercises both the changed and the identical paths.
    diff_other = fresh("case_diff_other.3mf")
    cli.cmd_settings(
        {
            "file": diff_other,
            "in_place": True,
            "ops": [{"op": "set", "key": "layer_height", "value": "0.16"}],
        }
    )
    write(args.out, "bambu3mf_diff", "changed", cli.cmd_diff({"file": work, "against": diff_other}))

    # inspect of a sliced package carries sliceInfo; build one without a slicer.
    sliced_fixture = os.path.join(workdir, "case_sliced.3mf")
    make_sliced_3mf(sliced_fixture)
    write(
        args.out,
        "bambu3mf_inspect",
        "sliced",
        cli.cmd_inspect({"file": sliced_fixture}),
    )

    from threemf.render import cmd_render

    rendered = cmd_render(
        {"file": work, "views": ["iso"], "width": 300, "out_dir": os.path.join(workdir, "preview")},
        {},
    )
    # `images` is attached by the JS execute() wrapper (attachment upload), not by
    # the kernel, so mirror that seam here instead of pretending the kernel owns it.
    rendered.setdefault("images", [])
    write(args.out, "bambu3mf_render", "iso", rendered)
    # `preflight` has no tool definition of its own (slice.mjs consumes it), so it
    # has no output schema to check; its shape is asserted in run_selftest.py.
    print(f"fixtures written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
