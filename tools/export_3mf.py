"""Export separate material volumes without surface painting.
Run: python3 tools/export_3mf.py --help
"""
import argparse
from pathlib import Path
import shutil

from verify import check_accent_profile, compile_stl, write_3mf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openscad", default=shutil.which("openscad"))
    parser.add_argument("--codename", default="AgentName")
    parser.add_argument("--location", default="CHINA")
    parser.add_argument("--faction", choices=("Resistance", "Enlightened"), default="Resistance")
    parser.add_argument("--mode", choices=("Raised", "Inset"), default="Raised")
    parser.add_argument("--output", type=Path, default=Path("outputs/biocard_multipart.3mf"))
    args = parser.parse_args()
    if not args.openscad:
        parser.error("Install OpenSCAD with textmetrics support or pass --openscad")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    settings = dict(codename=args.codename, location=args.location,
                    faction=args.faction, content_mode=args.mode)
    parts = []
    for part in ("Base", "Accent"):
        target = args.output.with_name(args.output.stem+"_"+part.lower()+".stl")
        parts.append(compile_stl(args.openscad, target, {**settings, "export_part": part}))
    check_accent_profile(parts[1])
    color = "#168BD2" if args.faction == "Resistance" else "#39B54A"
    write_3mf(args.output, parts, ["#F5F1E8", color])
    print(f"Exported {args.output}: Base / Accent; flat caps and identical layer outlines verified")


if __name__ == "__main__":
    main()
