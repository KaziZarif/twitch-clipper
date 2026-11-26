import asyncio
import json
import random
import re
import time
import unicodedata
from collections import Counter, deque
from pathlib import Path

import websockets

IRC_URI = "wss://irc-ws.chat.twitch.tv:443"

# sWAP WITH ANY LIVE STREAMER'S LOWERCASE USERNAME
CHANNELS = ["fallenshadow", "jerma985", "missmikkaa"]

OUTPUT_PATH = Path("data/chat_dump.jsonl")

IGNORED_BOTS = {
    "fossabot",
    "nightbot",
    "streamlabs",
    "moobot",
    "streamelements",
    "botisabot",
    "botrixoficial",
    "soundalerts",
}

TRACKED_USERNOTICES = {
    # Subscriptions & Gifts
    "sub",
    "resub",
    "subgift",
    "anonsubgift",          # Legacy, kept just in case
    "submysterygift",       # Community gift sub bombs
    "giftpaidupgrade",      # Upgrading a gifted sub to paid
    "anongiftpaidupgrade",  # Upgrading an anon gifted sub to paid
    "primepaidupgrade",     # Upgrading a Prime sub to paid
    "rewardgift",           # Emote/reward unlocks from gifting
    
    # Chat Banners, Milestones & Raids
    "announcement",         # Highlighted mod/broadcaster announcements
    "viewermilestone",      # Watch streaks (often has user comment attached)
    "bitsbadgetier",        # Bits badge milestone (often has user comment)
    "modiversary",          # Mod anniversary
    "raid",
    "unraid",
    "sharedchatnotice",     # Shared Chat cross-channel events
    "ritual"                # Legacy new chatter rituals
}

URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
MAX_SEEN_IDS = 5000

def unescape_tag_value(val: str) -> str:
    """Unescape IRCv3 tag values (\\s -> space, \\: -> ;, etc.)."""
    if "\\" not in val:
        return val
    mapping = {"s": " ", ":": ";", "\\": "\\", "r": "\r", "n": "\n"}
    out = []
    i = 0
    n = len(val)
    while i < n:
        if val[i] == "\\" and i + 1 < n:
            out.append(mapping.get(val[i + 1], val[i + 1]))
            i += 2
        else:
            out.append(val[i])
            i += 1
    return "".join(out)

def parse_irc_line(line: str) -> dict:
    """Parse an IRCv3 line into tags, prefix, command, params, and trailing text."""
    tags = {}
    rest = line

    if rest.startswith("@"):
        raw_tags, rest = rest[1:].split(" ", 1)
        for tag in raw_tags.split(";"):
            if "=" in tag:
                k, v = tag.split("=", 1)
                tags[k] = unescape_tag_value(v)
            elif tag:
                tags[tag] = ""

    prefix = ""
    if rest.startswith(":"):
        prefix, rest = rest[1:].split(" ", 1)

    trailing = ""
    if " :" in rest:
        head, trailing = rest.split(" :", 1)
    else:
        head = rest

    parts = head.split()
    command = parts[0] if parts else ""
    params = parts[1:] if len(parts) > 1 else []

    return {
        "tags": tags,
        "prefix": prefix,
        "command": command,
        "params": params,
        "trailing": trailing,
    }

def strip_action_wrapper(text: str) -> str:
    """Strip /me CTCP ACTION wrappers so emote indices and text stay clean."""
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        return text[8:-1]
    return text

def extract_native_emotes(raw_text: str, emotes_tag: str) -> list[str]:
    """
    Extract exact-case native Twitch emote names using character spans
    BEFORE stripping invisible Unicode characters.
    Format of emotes_tag: 'emote_id:start-end,start-end/emote_id2:start-end'
    """
    if not emotes_tag:
        return []
    found = []
    seen = set()
    for entry in emotes_tag.split("/"):
        if ":" not in entry:
            continue
        _, spans = entry.split(":", 1)
        first_span = spans.split(",", 1)[0]
        if "-" not in first_span:
            continue
        try:
            start, end = map(int , first_span.split("-", 1))
            token = raw_text[start : end + 1]
            if token and token not in seen:
                seen.add(token)
                found.append(token)
        except ValueError:
            continue
    return found

def clean_text(text: str) -> str:
    """Remove invisible extension-injected Unicode characters and normalize spaces."""
    cleaned = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Mn", "Cs", "Co"))
    return " ".join(cleaned.split())

def parse_badge_names(badges_tag: str) -> set[str]:
    if not badges_tag:
        return set()
    return {b.split("/", 1)[0] for b in badges_tag.split(",") if b}

class ChatCollector:
    def __init__(self, channels: list[str], output_path: Path):
        self.channels = [c.lower().lstrip("#") for c in channels]
        self.output_path = output_path
        self.seen_ids: set[str] = set()
        self.seen_order: deque[str] = deque()
        self.saved_counts: Counter = Counter()
        self.dropped_counts: Counter = Counter()

    def _is_duplicate_msg(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        if msg_id in self.seen_ids:
            return True
        if len(self.seen_order) >= MAX_SEEN_IDS:
            oldest = self.seen_order.popleft()
            self.seen_ids.discard(oldest)
        self.seen_order.append(msg_id)
        self.seen_ids.add(msg_id)
        return False

    def handle_parsed(self, msg: dict) -> dict | None:
        cmd = msg["command"]
        tags = msg["tags"]
        params = msg["params"]
        channel = params[0].lstrip("#").lower() if params else ""
        ts = int(tags["tmi-sent-ts"]) if tags.get("tmi-sent-ts", "").isdigit() else int(time.time() * 1000)

        if cmd == "PRIVMSG":
            msg_id = tags.get("id", "")
            if self._is_duplicate_msg(msg_id):
                self.dropped_counts["shared_chat_dup"] += 1
                return None

            user = msg["prefix"].split("!", 1)[0].lower()
            badges = parse_badge_names(tags.get("badges", ""))

            if "bot-badge" in badges or user in IGNORED_BOTS:
                self.dropped_counts["bot"] += 1
                return None

            raw_text = strip_action_wrapper(msg["trailing"])
            native_emotes = extract_native_emotes(raw_text, tags.get("emotes", ""))
            cleaned = clean_text(raw_text)

            if not cleaned:
                self.dropped_counts["empty"] += 1
                return None
            if cleaned.startswith("!"):
                self.dropped_counts["command"] += 1
                return None
            if URL_RE.search(cleaned):
                self.dropped_counts["url"] += 1
                return None

            self.saved_counts[channel] += 1
            return {
                "type": "msg",
                "ts": ts,
                "ch": channel,
                "uid": tags.get("user-id", ""),
                "user": user,
                "mod": tags.get("mod") == "1",
                "emotes": native_emotes,
                "text": cleaned,
            }

        if cmd == "ROOMSTATE":
            state_keys = ["slow", "subs-only", "r9k", "emote-only", "followers-only"]
            state = {k: tags[k] for k in state_keys if k in tags}
            if state:
                return {"type": "roomstate", "ts": ts, "ch": channel, "state": state}

        if cmd == "USERNOTICE":
            notice_type = tags.get("msg-id", "")
            if notice_type in TRACKED_USERNOTICES:
                record = {
                    "type": "usernotice",
                    "ts": ts,
                    "ch": channel,
                    "msg_id": notice_type,
                }
                if "msg-param-mass-gift-count" in tags:
                    record["count"] = int(tags["msg-param-mass-gift-count"])
                elif "msg-param-viewerCount" in tags:
                    record["viewers"] = int(tags["msg-param-viewerCount"])
                return record

        return None

    async def run(self):
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        backoff = 1

        with self.output_path.open("a", encoding="utf-8") as fh:
            try:
                while True:
                    nick = f"justinfan{random.randint(10000, 99999)}"
                    try:
                        async with websockets.connect(IRC_URI, ping_interval=None) as ws:
                            await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
                            await ws.send("PASS SCHMOOPIIE")
                            await ws.send(f"NICK {nick}")

                            for ch in self.channels:
                                await ws.send(f"JOIN #{ch}")
                                await asyncio.sleep(1.0)

                            print(f"Connected as {nick} | Recording {len(self.channels)} channels -> {self.output_path}")
                            last_status = time.monotonic()

                            async for raw_frame in ws:
                                reconnect_requested = False

                                for line in raw_frame.split("\r\n"):
                                    if not line:
                                        continue
                                    if line.startswith("PING"):
                                        await ws.send(line.replace("PING", "PONG", 1))
                                        continue

                                    parsed = parse_irc_line(line)
                                    if parsed["command"] == "001":
                                        backoff = 1  # authenticated cleanly
                                    elif parsed["command"] == "RECONNECT":
                                        print("Server requested RECONNECT; cycling socket...")
                                        reconnect_requested = True
                                        break

                                    record = self.handle_parsed(parsed)
                                    if record:
                                        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

                                if reconnect_requested:
                                    break

                                now = time.monotonic()
                                if now - last_status >= 10.0:
                                    fh.flush()
                                    saved_summary = ", ".join(
                                        f"#{ch}:{self.saved_counts[ch]}" for ch in self.channels
                                    )
                                    dropped_total = sum(self.dropped_counts.values())
                                    print(
                                        f"[{time.strftime('%H:%M:%S')}] Saved ({saved_summary}) | "
                                        f"Dropped: {dropped_total} {dict(self.dropped_counts)}"
                                    )
                                    last_status = now

                    except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                        print(f"Disconnected ({exc}). Reconnecting in {backoff}s...")
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30)
            finally:
                fh.flush()
                print("\nFlushed buffer and stopped cleanly.")


if __name__ == "__main__":
    collector = ChatCollector(CHANNELS, OUTPUT_PATH)
    try:
        asyncio.run(collector.run())
    except KeyboardInterrupt:
        pass
        