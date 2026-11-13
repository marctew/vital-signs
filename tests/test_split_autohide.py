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


def _save(server, panes, name="Wall"):
    form = {"name": name, "grid": "2x4"}
    for key in ("content_id", "col", "row", "col_span", "row_span", "autohide", "fallback_id"):
        form[key] = [str(p.get(key, "")) for p in panes]
    return server.post("/content/split", **form)


def test_a_pane_can_show_another_page_while_its_module_has_nothing(server):
    server.post("/content/module/plex", name="Plex", opt_server="http://plex.lan:32400", opt_token="t")
    server.post("/content/module/clock", name="Clock")
    weather = server.add_content("Weather")
    plex, clock = server.ids()["Plex"], server.ids()["Clock"]
    _save(server, [dict(pane(clock, 0, 0, 2, 1)), dict(pane(plex, 0, 1, 2, 3), autohide=0, fallback_id=weather)])
    split_id = server.ids()["Wall"]

    html = server.client.get(f"/split/{split_id}/").text
    plex_pane = html.split('src="https://weather.example"')[0].rsplit('<div class="pane"', 1)[1]
    assert f'src="/pages/plex/?m={plex}"' in plex_pane, "both pages load in the same pane"
    assert "data-autohide" in plex_pane, "a stand-in page implies reacting to 'nothing to show'"
    assert 'class="primary standby"' in plex_pane and 'class="fallback"' in html, "the stand-in shows until the module reports"
    assert html.count("<iframe") == 3
    assert f'"fallback_id": {weather}' in server.client.get(f"/content/{split_id}").text, "the editor gets it back"

    # Deleting the stand-in leaves the pane hiding when empty, as before.
    with server.db() as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("DELETE FROM content_items WHERE id = ?", (weather,))
    html = server.client.get(f"/split/{split_id}/").text
    assert 'class="fallback"' not in html and html.count("<iframe") == 2 and "data-autohide" in html


def test_bad_stand_in_pages_are_refused(server):
    a, b = server.add_content("A"), server.add_content("B")
    assert "cannot stand in for itself" in _save(server, [dict(pane(a, 0, 0), fallback_id=a)]).text
    assert "no longer exists" in _save(server, [dict(pane(a, 0, 0), fallback_id=9999)]).text
    _save(server, [dict(pane(a, 0, 0))], name="Inner")
    inner = server.ids()["Inner"]
    assert "cannot be shown instead" in _save(server, [dict(pane(b, 0, 0), fallback_id=inner)], name="Outer").text


def test_an_old_database_gains_the_stand_in_column(make_server):
    first = make_server()
    a = first.add_content("A")
    with first.db() as db:
        db.execute("ALTER TABLE split_panes DROP COLUMN fallback_id")
    upgraded = make_server()
    cols = {r[1] for r in upgraded.db().execute("PRAGMA table_info(split_panes)")}
    assert "fallback_id" in cols
    save_split(upgraded, "/content/split", "Wall", "2x4", [pane(a, 0, 0)])
    assert upgraded.client.get(f"/split/{upgraded.ids()['Wall']}/").status_code == 200
