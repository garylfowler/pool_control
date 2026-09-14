from app.webtouch_parser import (
    CODE_BUTTON, PAGE_VSP, NLMessage, ScreenModel, StreamParser, command_for_button,
)

CHUNK = "<script type='text/javascript'>parent.printNL({code}, \"{params}\");</script>"


def script(code, params):
    return CHUNK.format(code=code, params=params)


def test_parser_extracts_messages_and_splits_params():
    p = StreamParser()
    msgs = p.feed(script(23, "30") + script(24, "0||1||0||Pool||2950"))
    assert msgs == [NLMessage(23, ["30"]), NLMessage(24, ["0", "1", "0", "Pool", "2950"])]


def test_parser_buffers_partial_chunks():
    p = StreamParser()
    whole = script(25, "0||2800")
    assert p.feed(whole[:20]) == []
    assert p.feed(whole[20:]) == [NLMessage(25, ["0", "2800"])]


def test_parser_tolerates_single_quotes_and_whitespace():
    p = StreamParser()
    text = "<script type='text/javascript'>parent.printNL(28, '9||14||26||10||10') </script>"
    assert p.feed(text) == [NLMessage(28, ["9", "14", "26", "10", "10"])]


def test_screen_model_tracks_page_buttons_and_info():
    m = ScreenModel()
    for msg in StreamParser().feed(
        script(23, "30") + script(24, "0||1||0||Pool||2950") + script(24, "6||0||0||Cloudy||2800") + script(25, "0||2950")
    ):
        m.apply(msg)
    assert m.page_id == PAGE_VSP
    assert m.buttons[0].label == "Pool" and m.buttons[0].value == "2950" and m.buttons[0].state == 1
    assert m.buttons[6].state == 0
    assert m.info[0] == "2950"
    # speed change on the same page updates in place (captured live)
    for msg in StreamParser().feed(script(24, "0||0||0||Pool||2950") + script(24, "6||1||0||Cloudy||2800") + script(25, "0||2800")):
        m.apply(msg)
    assert m.buttons[0].state == 0 and m.buttons[6].state == 1 and m.info[0] == "2800"


def test_screen_model_resets_on_page_change():
    m = ScreenModel()
    m.apply(NLMessage(23, ["54"]))
    m.apply(NLMessage(CODE_BUTTON, ["2", "0", "0", "VSP1 Spd", "ADJ"]))
    m.apply(NLMessage(23, ["1"]))
    assert m.page_id == "1" and m.buttons == {} and m.info == {}


def test_button_by_label_is_forgiving():
    m = ScreenModel()
    m.apply(NLMessage(23, ["54"]))
    m.apply(NLMessage(CODE_BUTTON, ["2", "0", "0", "VSP1 Spd", "ADJ"]))
    m.apply(NLMessage(CODE_BUTTON, ["6", "1", "0", "Waterfall", "ON"]))
    assert m.button_by_label("vsp1 spd").index == 2
    assert m.button_by_label("WATERFALL ").state == 1
    assert m.button_by_label("nope") is None
    # Home page buttons split the label across label and value ("Water-" + "fall")
    m.apply(NLMessage(CODE_BUTTON, ["4", "0", "3", "Water-", "fall"]))
    assert m.button_by_label("Water-fall").index == 4


def test_parser_strips_the_escaped_degree_mojibake():
    p = StreamParser()
    assert p.feed(script(25, "0||82\\xC3\\u201Aº")) == [NLMessage(25, ["0", "82º"])]


def test_apply_ignores_messages_with_unparsable_numbers():
    m = ScreenModel()
    m.apply(NLMessage(23, ["54"]))
    m.apply(NLMessage(CODE_BUTTON, ["2", "0", "0", "VSP1 Spd", "ADJ"]))
    m.apply(NLMessage(CODE_BUTTON, ["", "0", "0", "Junk", ""]))  # no button index
    m.apply(NLMessage(25, ["x", "82º"]))  # no info index
    assert list(m.buttons) == [2] and m.info == {}


def test_command_for_button():
    assert command_for_button(0) == 17
    assert command_for_button(7) == 24


def test_parser_accepts_quoted_codes_as_sent_by_the_real_stream():
    p = StreamParser()
    real = ("<html><head></head><body></body></html>"
            "<script type='text/javascript'>parent.printNL('23','30');</script>"
            "<script type='text/javascript'>parent.printNL('24','0||1||0||Pool||2950');</script>")
    assert p.feed(real) == [NLMessage(23, ["30"]), NLMessage(24, ["0", "1", "0", "Pool", "2950"])]


def test_parser_keeps_word_codes_as_strings():
    p = StreamParser()
    assert p.feed("<script type='text/javascript'>parent.printNL('OFFLINE','');</script>") == [NLMessage("OFFLINE", [""])]
    m = ScreenModel()
    m.apply(NLMessage("OFFLINE", [""]))  # ignored, no error
    assert m.page_id is None
