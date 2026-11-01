#!/usr/bin/env python3
"""Rebuild two MIT-licensed community reference lines, without executing source JS."""
import json
import math
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
from game_tracks import validate_map

COMMIT = "07ead73fa04402c860b21039d5aa8c22a33a7d93"
REPO = "leroythelegend/rough_idea_project_cars"
BASE = f"https://raw.githubusercontent.com/{REPO}/{COMMIT}"


def read(path):
    with urllib.request.urlopen(BASE + "/" + path, timeout=30) as response:
        return response.read().decode("utf8")


def main():
    maps = []
    for filename, track, variation in [
        ("zolder_raceline_track.json", "Zolder", ""),
        ("DubaiAeroDromeClub_raceline_track.json", "Dubai Autodrome", "Club"),
    ]:
        path = "bin/capture_lap_data/" + filename
        raw = read(path).strip()
        # The upstream file is a JS string assignment, despite the .json suffix.
        data = json.loads(raw[raw.index("'")+1:raw.rindex("'")])["data"]
        points = []
        for item in data:
            point = [float(item["x"]), float(item["z"])]
            if not points or math.dist(points[-1], point) >= 5:
                points.append(point)
        end = [float(data[-1]["x"]), float(data[-1]["z"])]
        if points[-1] != end:
            points.append(end)
        maps.append(validate_map(dict(version=1, track=track, variation=variation,
                                      label=track + " " + variation, kind="community_reference",
                                      source=f"Community recorded racing line, MIT (c) 2018 leroythelegend; not official circuit boundaries. https://github.com/{REPO}/blob/{COMMIT}/{path}",
                                      paths=[points])))
        print(track, variation, len(points), "points")
    (ROOT / "shared/pcars_track_maps.json").write_text(json.dumps(maps, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf8")
    notice = ROOT / "docs/third_party/pcars-reference-maps-LICENSE.txt"
    notice.parent.mkdir(parents=True, exist_ok=True)
    notice.write_text(f"Source: https://github.com/{REPO}/tree/{COMMIT}\nDerived x/z reference lines in shared/pcars_track_maps.json\n\n" + read("LICENSE"), encoding="utf8")


if __name__ == "__main__":
    main()
