"""Parses the WebTouch server-push stream and keeps a model of the current screen.

The stream body is a sequence of
  <script type='text/javascript'>parent.printNL(<code>, "<params>");</script>
chunks. params fields are separated by "||". See docs/iaqualink-webtouch-protocol.md.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

LOGGER = logging.getLogger(__name__)

PAGE_HOME = "1"
PAGE_MENU = "15"
PAGE_DEVICES = "54"
PAGE_VSP = "30"

CODE_PAGE = 23
CODE_BUTTON = 24
CODE_INFO = 25

_SCRIPT_RE = re.compile(
    # Real chunks: <script type='text/javascript'>parent.printNL('24','0||1||0||Pool||2950');</script>
    # The code is a quoted number, or a word such as 'OFFLINE' (captured live 2026-09-14).
    r"<script[^>]*>\s*parent\.printNL\(\s*(['\"]?)([A-Za-z0-9_]+)\1\s*,\s*(['\"])(.*?)\3\s*\)\s*;?\s*</script>",
    re.S,
)


@dataclass(frozen=True)
class NLMessage:
    code: int | str  # numeric screen codes as int; markers such as "OFFLINE" as str
    params: list[str]


@dataclass(frozen=True)
class Button:
    index: int
    state: int
    image: str
    label: str
    value: str


def command_for_button(index: int) -> int:
    return 17 + index


class StreamParser:
    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, text: str) -> list[NLMessage]:
        self._buffer += text
        messages: list[NLMessage] = []
        last_end = 0
        for match in _SCRIPT_RE.finditer(self._buffer):
            code_text = match.group(2)
            code: int | str = int(code_text) if code_text.isdigit() else code_text
            # the panel writes the degree sign as a double-encoded escape sequence
            # (literal "\xC3‚" text, mojibake for "Â"): drop it, the "º" follows
            raw = match.group(4).replace("\\xC3\\u201A", "")
            messages.append(NLMessage(code, raw.split("||")))
            last_end = match.end()
        self._buffer = self._buffer[last_end:]
        # keep the buffer from growing without bound if garbage arrives
        if len(self._buffer) > 200_000:
            self._buffer = self._buffer[-50_000:]
        return messages


def _norm(label: str) -> str:
    return re.sub(r"\s+", "", label).lower()


@dataclass
class ScreenModel:
    page_id: str | None = None
    buttons: dict[int, Button] = field(default_factory=dict)
    info: dict[int, str] = field(default_factory=dict)

    def apply(self, msg: NLMessage) -> None:
        if not isinstance(msg.code, int):
            return
        try:
            self._apply(msg)
        except ValueError:
            # a garbled chunk must not take down the stream reader
            LOGGER.debug("Ignoring unparsable printNL(%s) message: %s", msg.code, msg.params)

    def _apply(self, msg: NLMessage) -> None:
        if msg.code == CODE_PAGE:
            self.page_id = msg.params[0].strip()
            self.buttons = {}
            self.info = {}
        elif msg.code == CODE_BUTTON and len(msg.params) >= 5:
            index = int(msg.params[0])
            state = int(msg.params[1]) if msg.params[1].strip().isdigit() else 0
            self.buttons[index] = Button(index, state, msg.params[2], msg.params[3].strip(), msg.params[4].strip())
        elif msg.code == CODE_INFO and len(msg.params) >= 2:
            self.info[int(msg.params[0])] = msg.params[1].strip()

    def button_by_label(self, label: str) -> Button | None:
        wanted = _norm(label)
        for button in self.buttons.values():
            if _norm(button.label) == wanted or _norm(button.label + button.value) == wanted:
                return button
        return None
