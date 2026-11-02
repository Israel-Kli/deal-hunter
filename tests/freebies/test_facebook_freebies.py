import json

from deal_hunter.config import FacebookTopicCfg
from deal_hunter.freebies import facebook
from deal_hunter.freebies.runner import match_topics


def _story(post_id: str, text: str, url: str) -> dict:
    return {
        "post_id": post_id,
        "message": {"text": text},
        "wwwURL": url,
        "actors": [{"name": "דנה"}],
    }


def test_extract_posts_dedups_and_strips_query() -> None:
    url = "https://www.facebook.com/groups/1/permalink/10/?sale_post_id=10"
    body = "\n".join(
        [
            json.dumps({"data": {"node": _story("10", "מקרר למסירה", url)}}),
            json.dumps({"data": [{"x": _story("10", "מקרר למסירה", url)}]}),
            "not json",
        ]
    )
    posts = facebook.extract_posts([body])
    assert [(p.post_id, p.url, p.author) for p in posts] == [
        ("10", "https://www.facebook.com/groups/1/permalink/10/", "דנה")
    ]


def test_parse_cookie_header() -> None:
    cookies = facebook.parse_cookie_header('"a=1; b=x%7Cy=z; "')
    assert [(c["name"], c["value"]) for c in cookies] == [("a", "1"), ("b", "x%7Cy=z")]


def test_title_of_uses_first_nonempty_line() -> None:
    assert facebook.title_of("\n  שלום   עולם \nשורה שנייה") == "שלום עולם"


def test_match_topics_hebrew_russian_english() -> None:
    topics = [
        FacebookTopicCfg(label="מקרר", patterns=["מקרר", "холодильник\\w*", "fridge"]),
        FacebookTopicCfg(label="שולחן", patterns=["שולחן", "\\bстол(?:а|у|ом|е|ы|ов)?\\b"]),
    ]
    assert match_topics("מוסר את המקרר", topics) == ["מקרר"]
    assert match_topics("Отдам холодильник и стол", topics) == ["מקרר", "שולחן"]
    assert match_topics("отдам столько вещей", topics) == []


def test_state_reused_only_for_the_cookie_it_was_seeded_from(tmp_path) -> None:
    path = tmp_path / "state.json"
    assert facebook.load_state(path, "c_user=1") is None

    facebook.save_state(path, "c_user=1", {"cookies": [{"name": "xs", "value": "rotated"}]})
    assert facebook.load_state(path, "c_user=1") == {"cookies": [{"name": "xs", "value": "rotated"}]}
    assert facebook.load_state(path, "c_user=1; xs=new") is None
    assert path.stat().st_mode & 0o777 == 0o600
