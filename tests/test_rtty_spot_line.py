"""RTTY spots go out as RTTY Skimmer Server sends them ("RTTY 26 dB 45 BPS CQ"), and the
RBN feeder uploads them with Aggregator's RTTY mode code (11), not CW's (1)."""
import asyncio

from rbn_feeder import RBNSession, parse_spot
from telnet_server import SpotTelnetServer


def _spot_lines(**kw) -> list[str]:
    async def run() -> list[str]:
        server = SpotTelnetServer(host='127.0.0.1', port=0, callsign='N0CALL')
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        await reader.readuntil(b"callsign:\r\n")
        writer.write(b"N0TEST\r\n")
        await reader.readuntil(b"CwSkimmer >\r\n")
        for _ in range(100):
            if server.client_count:
                break
            await asyncio.sleep(0.01)
        server.broadcast_spot(**kw)
        line = (await reader.readuntil(b"\r\n")).decode()
        writer.close()
        await server.stop()
        return [line]
    return asyncio.run(run())


def test_rtty_spot_line_and_upload() -> None:
    (line,) = _spot_lines(freq_khz=14085.0, dx_call='KA4RRU', snr=26, wpm=45, mode='RTTY', comment='CQ')
    assert ' RTTY ' in line and ' 26 dB 45 BPS ' in line and '  CQ ' in line
    sp = parse_spot(line)
    assert sp is not None and (sp['mode'], sp['wpm'], sp['cq_flag']) == ('RTTY', 45, 'CQ')
    sent = []
    rbn = RBNSession('http://x', 'N0CALL', 'SparkGap', 'EM79', '', 'Normal', '', dry_run=True)
    rbn._post = lambda path, body: sent.append(body)
    rbn.post_spots([sp])
    assert sent[0]['s'][0][4] == '45' and sent[0]['s'][0][6] == '11'


def test_cw_spot_line_unchanged() -> None:
    (line,) = _spot_lines(freq_khz=7025.5, dx_call='K4U', snr=29, wpm=21, mode='CW', comment='CQ')
    assert ' CW ' in line and ' 29 dB 21 WPM ' in line
    sp = parse_spot(line)
    rbn = RBNSession('http://x', 'N0CALL', 'SparkGap', 'EM79', '', 'Normal', '', dry_run=True)
    sent = []
    rbn._post = lambda path, body: sent.append(body)
    rbn.post_spots([sp])
    assert sent[0]['s'][0][6] == '1'
