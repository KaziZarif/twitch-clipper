import asyncio
import random
import time
from collections import deque
import websockets

IRC_URI = "wss://irc-ws.chat.twitch.tv:443"
CHANNEL = "kiilanie"  # swap with any live streamer's lowercase username


def parse_privmsg(line: str):
    """Extract (user, text) from a raw Twitch IRC PRIVMSG line."""
    if " PRIVMSG " not in line:
        return None
    try:
        prefix, rest = line.split(" PRIVMSG ", 1)
        user = prefix.split("!", 1)[0].lstrip(":")
        _, text = rest.split(" :", 1)
        return user, text
    except ValueError:
        return None


async def listen(channel: str):
    nickname = f"justinfan{random.randint(10000, 99999)}"
    timestamps = deque()
    last_stat_print = time.monotonic()

    async with websockets.connect(IRC_URI) as ws:
        await ws.send("PASS SCHMOOPIIE")
        await ws.send(f"NICK {nickname}")
        await ws.send(f"JOIN #{channel.lower()}")
        print(f"Connected as {nickname} to #{channel}")

        async for raw_frame in ws:
            now = time.monotonic()

            for line in raw_frame.split("\r\n"):
                if not line:
                    continue

                if line.startswith("PING"):
                    await ws.send(line.replace("PING", "PONG", 1))
                    continue

                parsed = parse_privmsg(line)
                if parsed:
                    user, text = parsed
                    timestamps.append(now)
                    print(f"[{user}]: {text}")

            # Drop timestamps older than 5 seconds
            while timestamps and now - timestamps[0] > 5.0:
                timestamps.popleft()

            # Print rolling 5-second message count every 5 seconds
            if now - last_stat_print >= 5.0:
                rate = len(timestamps) / 5.0
                print(f"\n--- 5s window: {len(timestamps)} msgs ({rate:.2f} msgs/sec) ---\n")
                last_stat_print = now


if __name__ == "__main__":
    asyncio.run(listen(CHANNEL))