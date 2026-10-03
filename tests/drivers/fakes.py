"""Stand-ins for the hardware: a USR-CAN315 answering from its AT manual, and
`ip -details -json` output in the shape iproute2 prints for a CAN interface."""

from __future__ import annotations

import json

FACTORY: dict[str, str] = {
    "AT+CAN": "\r\n+CAN:100,1,NDTF\r\n\r\nOK\r\n",
    "AT+CANFD": "\r\n+CANFD:1,1,2000\r\n\r\nOK\r\n",
    "AT+CANMODE": "\r\n+CANMODE:NORMAL\r\n\r\nOK\r\n",
    "AT+TMODE": "\r\n+TMODE:TRANS\r\n\r\nOK\r\n",
    "AT+TRDIR": "\r\n+TRDIR:BOTHWAY\r\n\r\nOK\r\n",
    "AT+MSG": "\r\n+MSG:0,0\r\n\r\nOK\r\n",
    "AT+WANN": "\r\n+WANN:STATIC,192.168.0.7,255.255.255.0,192.168.0.1\r\n\r\nOK\r\n",
    "AT+SOCKA": "\r\n+SOCKA:TCPS,192.168.0.7,20001\r\n\r\nOK\r\n",
    "AT+VER": "\r\n+VER:V1.0.00.000000.0000\r\n\r\nOK\r\n",
    "AT+MID": "\r\n+MID:USR-CAN315\r\n\r\nOK\r\n",
    "AT+SN": "\r\n+SN:03500324092600069753\r\n\r\nOK\r\n",
}
"""A device straight out of the box."""

LAGUNBUS_READY: dict[str, str] = FACTORY | {
    "AT+CAN": "\r\n+CAN:500,0,EDTF\r\n\r\nOK\r\n",
    "AT+CANFD": "\r\n+CANFD:0,0,100\r\n\r\nOK\r\n",
    "AT+TMODE": "\r\n+TMODE:PROTOL\r\n\r\nOK\r\n",
    "AT+MSG": "\r\n+MSG:1,1\r\n\r\nOK\r\n",
}
"""A device already set up for LagunBus."""


class FakeTransport:
    """Answers from a table; anything unknown gets a bare ``OK``. Records what was sent."""

    where = "fake"

    def __init__(self, replies: dict[str, str], *, echo: bool = False) -> None:
        self.replies = replies
        self.echo = echo
        self.sent: list[str] = []

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def request(self, command: str) -> bytes:
        self.sent.append(command)
        reply = self.replies.get(command, "\r\nOK\r\n")
        return ((command + "\r\n" if self.echo else "") + reply).encode()

    @property
    def assignments(self) -> list[str]:
        return [command for command in self.sent if "=" in command]


def link_json(**data: object) -> str:
    info: dict[str, object] = {"ctrlmode": [], "state": "ERROR-ACTIVE", "restart_ms": 100}
    info |= {"bittiming": {"bitrate": 500000, "sample_point": "0.875"}, "clock": 40000000}
    info |= data
    return json.dumps(
        [
            {
                "ifname": "can0",
                "flags": ["NOARP", "UP", "LOWER_UP", "ECHO"],
                "operstate": "UP",
                "txqlen": 10,
                "link_type": "can",
                "linkinfo": {"info_kind": "can", "info_data": info},
                "parentbus": "spi",
                "parentdev": "spi0.0",
            }
        ]
    )
