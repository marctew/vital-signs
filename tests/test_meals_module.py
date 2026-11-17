"""The meal plan and bins modules: HomeForm's feeds, shaped for the pages."""
import json
from datetime import date, timedelta

from server import modules

TODAY = date(2026, 11, 17)


def meal(day, slot, title):
    return {"id": f"{day}:{slot}:1", "title": title, "subtitle": "…", "tone": "muted", "meta": {"day": day, "slot": slot, "recipe_id": 1}}


MEALS = [meal("2026-11-17", "lunch", "Soup"), meal("2026-11-17", "dinner", "Chilli"), meal("2026-11-17", "dinner", "Garlic bread"),
         meal("2026-11-18", "dinner", "Roast chicken"), meal("2026-11-20", "lunch", "Sandwiches"), meal("2026-11-20", "dinner", " ")]


def test_days_are_labelled_and_in_order():
    days = modules.parse_meals(MEALS, 5, "both", TODAY)
    assert [d["label"] for d in days] == ["Today", "Tomorrow", "Thursday", "Friday", "Saturday"]
    assert [d["today"] for d in days] == [True, False, False, False, False]
    assert days[0]["meals"] == [{"slot": "lunch", "names": ["Soup"]}, {"slot": "dinner", "names": ["Chilli", "Garlic bread"]}]
    assert days[2]["meals"] == [] and days[4]["meals"] == []
    assert days[3]["meals"] == [{"slot": "lunch", "names": ["Sandwiches"]}], "blank names are dropped"


def test_dinner_only_and_fewer_days():
    days = modules.parse_meals(MEALS, 2, "dinner", TODAY)
    assert len(days) == 2 and days[0]["meals"] == [{"slot": "dinner", "names": ["Chilli", "Garlic bread"]}]
    assert len(modules.parse_meals(MEALS, 99, "both", TODAY)) == 7 and len(modules.parse_meals(MEALS, 1, "both", TODAY)) == 1
    assert len(modules.parse_meals(MEALS, "x", "both", TODAY)) == 5


def test_a_strange_reply_is_an_empty_plan():
    for items in ([], None, [{"title": "x"}], [{"title": "x", "meta": "y"}], ["soup"]):
        assert all(not d["meals"] for d in modules.parse_meals(items, 5, "both", TODAY))


def feed(server, monkeypatch, items):
    calls = []

    def get(url, headers=None):
        calls.append((url, headers))
        return json.dumps({"feed": "x", "items": items}).encode(), "application/json"
    monkeypatch.setattr(modules, "_get", get)
    return calls


def test_the_page_gets_the_plan_using_the_token(server, monkeypatch):
    calls = feed(server, monkeypatch, [meal(date.today().isoformat(), "dinner", "Fish pie")])
    server.post("/content/module/meals", name="Meal plan", opt_url="http://192.168.4.79:8797/", opt_token="secret-token",
                opt_days="3", opt_slots="both")
    content_id = server.ids()["Meal plan"]
    data = server.client.get(f"/api/module/{content_id}/data").json
    assert len(data["days"]) == 3 and data["empty"] is False and data["fetched"] > 0
    assert data["days"][0]["meals"] == [{"slot": "dinner", "names": ["Fish pie"]}]
    assert calls == [("http://192.168.4.79:8797/api/v1/feeds/meals?limit=100", {"Authorization": "Bearer secret-token"})]
    assert "token" not in server.client.get(f"/api/module/{content_id}/config").json["options"], "the token never reaches the page"
    assert modules.list_modules()["meals"]["eink"] is True and modules.list_modules()["meals"]["can_hide"] is True


def test_address_and_token_are_required(server):
    assert "HomeForm address is required" in server.post("/content/module/meals", name="Meal plan", opt_url="").get_data(as_text=True)
    page = server.post("/content/module/bins", name="Bins", opt_url="http://homeform.lan", opt_token="")
    assert "HomeForm API token is required" in page.get_data(as_text=True)


def bins(day, title, away):
    return {"id": f"bins:{day}", "title": title, "subtitle": "…", "meta": {"date": day, "days_away": away, "bins": ["x"]}}


def test_bins_are_sorted_labelled_and_limited():
    items = [bins("2026-11-25", "Black bin", 8), bins("2026-11-18", "Recycling, Food waste", 1), bins("2026-11-16", "Old", -1),
             bins("2026-11-21", "Garden", 4), bins("nonsense", "x", 0), {"title": "no meta"}]
    days = modules.parse_bins(items, 4, TODAY)
    assert [(d["label"], d["soon"], d["bins"]) for d in days] == [
        ("Tomorrow", True, ["Recycling", "Food waste"]), ("Saturday", False, ["Garden"]), ("Wed 25 Nov", False, ["Black bin"])]
    assert len(modules.parse_bins(items, 1, TODAY)) == 1
    assert modules.parse_bins([bins(TODAY.isoformat(), "Black bin", 0)], 4, TODAY)[0]["label"] == "Today"
    assert modules.parse_bins(None, 4, TODAY) == []


def test_the_bins_page_gets_its_collections(server, monkeypatch):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    calls = feed(server, monkeypatch, [bins(tomorrow, "Black bin", 1)])
    server.post("/content/module/bins", name="Bins", opt_url="http://192.168.4.79:8797", opt_token="t", opt_count="4")
    data = server.client.get(f"/api/module/{server.ids()['Bins']}/data").json
    assert data["empty"] is False and data["days"][0]["label"] == "Tomorrow" and data["days"][0]["soon"] is True
    assert calls[0][0] == "http://192.168.4.79:8797/api/v1/feeds/bins?limit=100"
