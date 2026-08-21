from confchat.colors import color_state, hex_to_256, hex_to_rgb1000, normalize_hex_color


def test_normalize_hex_color():
    assert normalize_hex_color("#6F8F7A") == "#6f8f7a"
    assert normalize_hex_color("6f8f7a") == "#6f8f7a"
    assert normalize_hex_color("# 6f8f7a") is None
    assert normalize_hex_color("zzzzzz") is None
    assert normalize_hex_color("#12345") is None


def test_hex_to_rgb1000():
    assert hex_to_rgb1000("#ffffff") == (1000, 1000, 1000)
    assert hex_to_rgb1000("#000000") == (0, 0, 0)
    assert hex_to_rgb1000("#808080") == (502, 502, 502)


def test_hex_to_256_color_cube():
    assert hex_to_256("#ff0000") == 196
    assert hex_to_256("#00ff00") == 46
    assert hex_to_256("#0000ff") == 21


def test_hex_to_256_grayscale():
    assert hex_to_256("#000000") == 232
    assert hex_to_256("#ffffff") == 255
    assert 232 <= hex_to_256("#808080") <= 255


def test_color_state_defaults_in_first_seen_order():
    records = [
        {"type": "message", "user": "alice", "text": "hi"},
        {"type": "message", "user": "bob", "text": "yo"},
        {"type": "message", "user": "alice", "text": "again"},
    ]
    defaults, overrides = color_state(records)
    assert list(defaults) == ["alice", "bob"]
    assert defaults["alice"] != defaults["bob"]
    assert overrides == {}


def test_color_state_overrides():
    records = [
        {"type": "message", "user": "alice", "text": "hi"},
        {"type": "color", "user": "alice", "color": "#ff8800"},
        {"type": "color", "user": "alice", "color": "not-a-color"},
    ]
    _, overrides = color_state(records)
    assert overrides == {"alice": "#ff8800"}
