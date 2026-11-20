from html.parser import HTMLParser
from pathlib import Path

from pytest_bdd import then, when


@when("a social platform reads the workdash site", target_fixture="site_metadata")
def read_site_metadata():
    class MetadataParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.metadata = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "meta" and "content" in attrs:
                self.metadata[attrs.get("property", attrs.get("name"))] = attrs["content"]

    parser = MetadataParser()
    parser.feed((Path(__file__).resolve().parents[3] / "site/index.html").read_text())
    return parser.metadata


@then("it can display the workdash title, description, and dashboard image in a large preview")
def check_social_preview(site_metadata):
    image = "https://alessandro.molina.fyi/workdash/workdash.png"
    description = "Text-based GitHub work triage dashboard. Pick what to work on next, then jump straight into a review, analysis, or coding session."
    assert (
        site_metadata.items()
        >= {
            "og:title": "workdash",
            "og:description": description,
            "og:type": "website",
            "og:url": "https://alessandro.molina.fyi/workdash/",
            "og:image": image,
            "og:image:width": "967",
            "og:image:height": "520",
            "og:image:alt": "workdash dashboard showing GitHub issues and pull requests",
            "twitter:card": "summary_large_image",
            "twitter:title": "workdash",
            "twitter:description": description,
            "twitter:image": image,
        }.items()
    )
