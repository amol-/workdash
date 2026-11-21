"""Render terminal captures using the isolated world's Pillow installation."""

import argparse
import shutil
import subprocess
from pathlib import Path


def render(python, renderer, cast, output, animated=False, card=None):
    subprocess.run(
        [
            str(python),
            str(Path(__file__).resolve()),
            str(renderer),
            str(cast),
            str(output),
            *(["--animated"] if animated else []),
            *(["--card", str(card)] if card else []),
        ],
        check=True,
    )


def render_cast(renderer, cast, output, animated=False):
    """Render the capture, keeping only its final frame for a still."""
    from PIL import Image

    rendered = cast.with_suffix(".gif")
    subprocess.run(
        [
            str(renderer),
            "--font-size",
            "16",
            "--theme",
            "asciinema",
            "--idle-time-limit",
            "1",
            *(["--speed", "30", "--last-frame-duration", "1"] if animated else []),
            str(cast),
            str(rendered),
        ],
        check=True,
    )
    if animated:
        shutil.copy2(rendered, output)
    else:
        with Image.open(rendered) as image:
            image.seek(image.n_frames - 1)
            image.save(output)


def render_png(gif, output):
    """Use the dashboard's final frame for the fixed-size social preview."""
    from PIL import Image, ImageOps

    with Image.open(gif) as image:
        image.seek(image.n_frames - 1)
        ImageOps.pad(image.convert("RGB"), (967, 520), color="#202020").save(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("renderer", type=Path)
    parser.add_argument("cast", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--animated", action="store_true")
    parser.add_argument("--card", type=Path)
    args = parser.parse_args()
    render_cast(args.renderer, args.cast, args.output, args.animated)
    if args.card:
        render_png(args.output, args.card)
