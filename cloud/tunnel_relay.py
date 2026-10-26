"""인스턴스 안에서 늘 켜 두는 작은 TCP 중계기: python3 tunnel_relay.py 18188:8188,18189:8189,18000:8000

vast 의 중계 SSH(sshN.vast.ai)로 연 터널은, 아직 안 뜬 포트(예: 올리는 중인 LLM 8000)로 한 번 요청이 가면
'channel open failed: connect failed' 뒤로 같은 터널의 다른 포트(ComfyUI)까지 전부 멈춘다(2026-10-25 실측).
그래서 터널은 늘 떠 있는 이 중계기 포트로만 잇고, 중계기가 뒤 서비스로 넘긴다. 뒤가 아직 안 떴으면 연결만 닫는다
(터널 쪽에서는 정상으로 열렸다 닫힌 연결이라 터널이 안 죽는다). 웹소켓도 바이트 그대로 넘긴다. 표준 라이브러리만 쓴다.
"""
import asyncio
import sys


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except (ConnectionError, OSError):
            pass


def handler(dst: int):
    async def handle(c_reader, c_writer):
        try:
            s_reader, s_writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", dst), 5)
        except (OSError, asyncio.TimeoutError):
            c_writer.close()  # 뒤 서비스가 아직 없다: 조용히 닫는다
            return
        await asyncio.gather(pipe(c_reader, s_writer), pipe(s_reader, c_writer))
    return handle


async def main(pairs):
    for listen, dst in pairs:
        await asyncio.start_server(handler(dst), "127.0.0.1", listen)
        print(f"relay 127.0.0.1:{listen} -> 127.0.0.1:{dst}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main([tuple(int(x) for x in p.split(":")) for p in sys.argv[1].split(",")]))
