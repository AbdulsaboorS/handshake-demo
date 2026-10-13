"""How reliably does the call code survive the line? Sends random codes as DTMF over one Realtime call
and counts exact decodes, optionally while other processes keep the CPU busy (like a screen recorder).

  uv run python -m evals.binding --sends 10
  uv run python -m evals.binding --sends 10 --load 4
"""

import argparse
import asyncio
import multiprocessing
import secrets

from dotenv import load_dotenv

load_dotenv(".env")

from backend.voice import VoiceLine  # noqa: E402


def burn() -> None:
    while True:
        pass


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sends", type=int, default=10)
    parser.add_argument("--load", type=int, default=0, help="busy processes running alongside")
    args = parser.parse_args()

    burners = [multiprocessing.Process(target=burn, daemon=True) for _ in range(args.load)]
    for b in burners:
        b.start()
    line = VoiceLine()
    try:
        await line.open()
        ok = 0
        for _ in range(args.sends):
            code = f"{secrets.randbelow(10**6):06d}"
            heard = await line.send_code(code)
            ok += heard == code
            print(f"sent {code} heard {heard!r:10} {'ok' if heard == code else 'WRONG'}", flush=True)
        print(f"\n{ok}/{args.sends} exact with {args.load} busy processes")
    finally:
        await line.close()
        for b in burners:
            b.terminate()


if __name__ == "__main__":
    asyncio.run(main())
