"""Render terminal captures using the isolated world's Pillow installation."""

import argparse
import json
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
    if not animated:
        _last_frame(renderer, cast).save(output)
        return

    # Replay the full terminal state up to each milestone, without showing idle time.
    header, *events = cast.read_text().splitlines(keepends=True)
    chapter = cast.with_name("loop-chapter.cast")
    from PIL import Image, ImageDraw, ImageFont

    captions = (
        "1  Choose Claude or Codex",
        "2  Top row: the agent is implementing the issue",
        "3  Agent pane: watch the work happen",
        "4  Top row: the pull request is in review",
        "5  The pull request merges and closes the issue",
    )
    frames = []
    for end, caption in zip(_loop_checkpoints(events), captions, strict=True):
        chapter.write_text(header + "".join(events[:end]))
        terminal = _last_frame(renderer, chapter)
        frame = Image.new("RGB", (terminal.width, terminal.height + 54), "#102a43")
        ImageDraw.Draw(frame).text(
            (16, 10), caption, fill="white", font=ImageFont.load_default(size=30)
        )
        frame.paste(terminal, (0, 54))
        frames.append(frame)
    frames[0].save(
        output, save_all=True, append_images=frames[1:], duration=[2400] * 4 + [3000], loop=0
    )


def _loop_checkpoints(events):
    milestones = (
        "(2) codex",
        "[automerge] project  demo/project#42  developing (implement)",
        "Updating the shared workspace",
        "reviewing",
        "(closed)",
    )
    rows = [json.loads(event) for event in events]
    end = 0
    for marker in milestones:
        for index in range(end, len(rows)):
            timestamp, _, text = rows[index]
            if marker in text:
                end = index + 1
                # Include the rest of the redraw if it arrived in multiple PTY reads.
                while end < len(rows) and rows[end][0] - timestamp < 0.08:
                    end += 1
                yield end
                break
        else:
            raise RuntimeError(f"loop: missing demo milestone {marker!r}")


def _last_frame(renderer, cast):
    from PIL import Image

    rendered = cast.with_suffix(".gif")
    subprocess.run(
        [
            str(renderer),
            "--font-dir",
            str(renderer.parent / "fonts"),
            "--font-size",
            "16",
            "--theme",
            "asciinema",
            "--idle-time-limit",
            "1",
            str(cast),
            str(rendered),
        ],
        check=True,
    )
    with Image.open(rendered) as image:
        image.seek(image.n_frames - 1)
        return image.convert("RGB")


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
