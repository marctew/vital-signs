"""The About page and the sidebar footer's version line (Design Uniformity 7.15)."""
from server import __version__


def test_about_shows_the_version_and_counts(server):
    server.poll()
    html = server.client.get("/about").text
    assert f"v{__version__}" in html
    assert "Your data" in html and "Built with" in html
    assert "Displays" in html and "Python" in html


def test_every_page_ends_with_the_version_line(server):
    html = server.client.get("/").text
    assert 'class="version-link"' in html and f"v{__version__}" in html and "· About" in html
