"""Split-screen panes that hide when their module has nothing to show (server side)."""
from test_admin_ui import pane, save_split


def test_autohide_is_saved_per_pane_and_marked_on_the_page(server):
    server.post("/content/module/plex", name="Plex", opt_server="http://plex.lan:32400", opt_token="t")
    server.post("/content/module/clock", name="Clock")
    plex, clock = server.ids()["Plex"], server.ids()["Clock"]
    editor = server.client.get("/content").text
    assert f"var hideable = [{plex}];" in editor, "only modules that can report being empty are offered the option"

    panes = [dict(pane(clock, 0, 0, 2, 1), autohide=0), dict(pane(plex, 0, 1, 2, 1), autohide=1)]
    form = {"name": "Wall", "grid": "2x4"}
    for key in ("content_id", "col", "row", "col_span", "row_span", "autohide"):
        form[key] = [str(p[key]) for p in panes]
    server.post("/content/split", **form)
    split_id = server.ids()["Wall"]

    html = server.client.get(f"/split/{split_id}/").text
    plex_pane = html.split(f'src="/pages/plex/?m={plex}"')[0].rsplit('<div class="pane"', 1)[1]
    clock_pane = html.split(f'src="/pages/clock/?m={clock}"')[0].rsplit('<div class="pane"', 1)[1]
    assert "data-autohide" in plex_pane and "data-autohide" not in clock_pane
    assert '"autohide": 1' in server.client.get(f"/content/{split_id}").text, "the editor gets the saved setting back"


def test_splits_saved_without_the_flag_still_work(server):
    a = server.add_content("A")
    save_split(server, "/content/split", "Wall", "2x4", [pane(a, 0, 0)])
    html = server.client.get(f"/split/{server.ids()['Wall']}/").text
    assert html.count("<iframe") == 1 and "data-autohide>" not in html
