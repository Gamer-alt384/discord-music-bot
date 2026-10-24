"""A Discord music bot that plays audio from YouTube (name, link, or full playlist).

Supports:
  - /play <song name>          -> searches YouTube, plays it
  - /play <youtube link>       -> plays that video
  - /play <youtube playlist>   -> queues the whole playlist
  - /play <spotify playlist>   -> matches every Spotify track on YouTube and queues them
                                  (needs SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in .env)

Run with:  uv run bot.py
Requires:  DISCORD_TOKEN in a .env file next to this script.
"""

import asyncio
import json
import logging
import os
import random
import re
from dataclasses import dataclass, field

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from static_ffmpeg import run as static_ffmpeg_run

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("music-bot")

# Filled once at startup: absolute paths to the bundled ffmpeg / ffprobe binaries.
FFMPEG_PATH: str | None = None

# Safety cap so a giant playlist can't blow up the queue / hammer YouTube.
MAX_PLAYLIST_TRACKS = 200

YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "format": "bestaudio/best",
}

# Flat extraction: only ids/titles, no stream URLs. Fast, used for playlists/search.
YDL_FLAT_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": "in_playlist",
}


@dataclass
class Track:
    title: str
    url: str                    # YouTube watch/page URL (for display + lazy resolving)
    stream_url: str | None      # direct audio URL; None = resolve right before playing
    duration: int               # seconds
    requester: discord.Member
    thumbnail: str | None = None  # image URL for the "now playing" embed


# YouTube ke audio stream URLs aksar User-Agent ke bina 403 dete hain.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _pick_audio_url(info: dict) -> str | None:
    """Get a direct audio URL out of a yt-dlp info dict.

    yt-dlp frequently leaves the top-level 'url' empty and instead puts the
    real stream links inside 'requested_formats' / 'formats', so fall back to
    those (prefer an audio-only format).
    """
    if info.get("url"):
        return info["url"]
    formats = info.get("requested_formats") or info.get("formats") or []
    for fmt in reversed(formats):
        if fmt.get("url") and fmt.get("acodec") not in (None, "none"):
            return fmt["url"]
    for fmt in formats:
        if fmt.get("url"):
            return fmt["url"]
    return None


def _fetch_stream_url(url: str) -> str | None:
    """Resolve a YouTube watch URL to a direct audio stream URL (blocking, off-thread)."""
    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
        if "entries" in info:
            info = info["entries"][0]
        return _pick_audio_url(info)
    except Exception as err:  # noqa: BLE001
        log.warning("Could not resolve stream for %s: %s", url, err)
        return None


@dataclass
class Player:
    """Per-guild playback state: a queue plus a background worker task."""

    voice: discord.VoiceClient
    queue: asyncio.Queue[Track] = field(default_factory=asyncio.Queue)
    next_track: asyncio.Event = field(default_factory=asyncio.Event)
    current: Track | None = None
    volume: float = 0.5
    text_channel: discord.abc.Messageable | None = None
    history: list[Track] = field(default_factory=list)
    _volume_ctrl: discord.PCMVolumeTransformer | None = None
    _worker: asyncio.Task | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    # playback-position bookkeeping (for seek / progress)
    _start_time: float = 0.0
    _paused_total: float = 0.0
    _paused_at: float | None = None
    _seek_offset: float = 0.0
    _no_advance: bool = False

    def start(self) -> None:
        if self._worker is None:
            self._loop = asyncio.get_running_loop()
            self._worker = asyncio.create_task(self._run(), name="music-worker")

    async def _run(self) -> None:
        while True:
            self.next_track.clear()
            track = await self.queue.get()
            # Resolve the direct stream lazily, right before playback.
            if not track.stream_url:
                track.stream_url = await asyncio.to_thread(_fetch_stream_url, track.url)
            if not track.stream_url:
                log.info("Skipping unplayable track: %s", track.title)
                self._advance()          # move straight to the next one
                await self.next_track.wait()
                continue
            self.current = track
            self.history.append(track)
            if len(self.history) > 30:
                self.history.pop(0)
            self._play(track)
            await self._announce(track)
            await self.next_track.wait()

    async def _announce(self, track: Track) -> None:
        """Post a 'now playing' embed with thumbnail + control buttons."""
        if self.text_channel is None:
            return
        try:
            await self.text_channel.send(embed=now_playing_embed(track), view=ControlView())
        except Exception as err:  # noqa: BLE001
            log.warning("Could not post now-playing message: %s", err)

    def _play(self, track: Track, start_offset: float = 0.0) -> None:
        before = (
            "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
            f'-user_agent "{USER_AGENT}"'
        )
        if start_offset and start_offset > 0:
            before += f" -ss {int(start_offset)}"
        source = discord.FFmpegPCMAudio(
            track.stream_url, executable=FFMPEG_PATH, before_options=before,
        )
        self._volume_ctrl = discord.PCMVolumeTransformer(source, volume=self.volume)
        # Reset position bookkeeping for this (re)start.
        self._seek_offset = float(start_offset or 0.0)
        self._start_time = self._now()
        self._paused_total = 0.0
        self._paused_at = None

        def _after(err: Exception | None) -> None:
            if err:
                log.error("Playback error: %s", err)
            self._advance()

        self.voice.play(self._volume_ctrl, after=_after)

    def _now(self) -> float:
        loop = self._loop or asyncio.get_event_loop()
        return loop.time()

    def _advance(self) -> None:
        """Called from the audio thread when a track ends; wake the worker."""
        if self._no_advance:      # a seek/back restarted playback on purpose
            self._no_advance = False
            return
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self.next_track.set)
        except RuntimeError:
            pass

    # ---- position / seek ----
    def position(self) -> float:
        if self.current is None:
            return 0.0
        if self._paused_at is not None:
            elapsed = self._paused_at - self._start_time - self._paused_total
        else:
            elapsed = self._now() - self._start_time - self._paused_total
        return max(0.0, self._seek_offset + elapsed)

    def seek(self, delta: float) -> float | None:
        """Jump by delta seconds within the current track; returns new position."""
        track = self.current
        if track is None or not track.stream_url:
            return None
        target = max(0.0, self.position() + delta)
        if track.duration and target >= track.duration:
            target = max(0.0, track.duration - 1)
        self._no_advance = True
        vc = self.voice
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()             # _after -> _advance is suppressed by _no_advance
        self._play(track, start_offset=target)
        return target

    # ---- pause / resume ----
    def is_paused(self) -> bool:
        return self.voice is not None and self.voice.is_paused()

    def pause(self) -> bool:
        if self.voice and self.voice.is_playing():
            self.voice.pause()
            self._paused_at = self._now()
            return True
        return False

    def resume(self) -> bool:
        if self.voice and self.voice.is_paused():
            self.voice.resume()
            if self._paused_at is not None:
                self._paused_total += self._now() - self._paused_at
                self._paused_at = None
            return True
        return False

    # ---- volume ----
    def adjust_volume(self, delta: float) -> float:
        self.volume = min(5.0, max(0.0, round(self.volume + delta, 2)))
        if self._volume_ctrl is not None:
            self._volume_ctrl.volume = self.volume
        return self.volume

    def set_volume(self, volume: float) -> None:
        self.volume = min(5.0, max(0.0, volume))
        if self._volume_ctrl is not None:
            self._volume_ctrl.volume = self.volume

    # ---- queue controls ----
    def shuffle_queue(self) -> int:
        items = list(self.queue._queue)  # peek the pending tracks
        if len(items) < 2:
            return len(items)
        random.shuffle(items)
        self.queue._queue.clear()
        self.queue._queue.extend(items)
        return len(items)

    def back(self) -> Track | None:
        """Go to the previous track (requeue it at the front and stop current)."""
        if len(self.history) < 2:
            return None
        prev = self.history[-2]
        self.history = self.history[:-1]      # drop the current one
        self.queue._queue.appendleft(prev)
        vc = self.voice
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()                          # worker advances into `prev`
        else:
            self._advance()
        return prev

    async def stop_and_disconnect(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        while not self.queue.empty():
            self.queue.get_nowait()
        self.current = None
        self.history.clear()
        if self.voice.is_connected():
            await self.voice.disconnect()


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.players: dict[int, Player] = {}

    async def setup_hook(self) -> None:
        global FFMPEG_PATH
        log.info("Preparing ffmpeg (first run downloads it)...")
        FFMPEG_PATH, _ = static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
        log.info("ffmpeg ready at %s", FFMPEG_PATH)
        await self.tree.sync()

    def get_player(self, guild: discord.Guild, voice: discord.VoiceClient) -> Player:
        player = self.players.get(guild.id)
        if player is None:
            player = Player(voice=voice)
            self.players[guild.id] = player
            player.start()
        return player


bot = MusicBot()
tree = bot.tree


def fmt_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- #
# URL classification
# --------------------------------------------------------------------------- #

def is_youtube_playlist(url: str) -> bool:
    return ("youtube.com" in url or "youtu.be" in url) and ("list=" in url or "/playlist" in url)


def _best_thumbnail(entry: dict) -> str | None:
    """Pull the best available image URL out of a yt-dlp entry/info dict."""
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    thumbs = entry.get("thumbnails")
    if thumbs:
        return thumbs[-1].get("url")
    return None


def _track_from_flat(entry: dict, requester: discord.Member) -> Track | None:
    """Build a Track from a yt-dlp flat entry (no stream URL yet)."""
    vid = entry.get("id")
    url = entry.get("url") or ""
    if url.startswith("http"):
        watch = url
    elif vid:
        watch = f"https://www.youtube.com/watch?v={vid}"
    else:
        return None
    return Track(
        title=entry.get("title") or "Unknown",
        url=watch,
        stream_url=None,
        duration=entry.get("duration") or 0,
        requester=requester,
        thumbnail=_best_thumbnail(entry),
    )


# --------------------------------------------------------------------------- #
# YouTube resolution
# --------------------------------------------------------------------------- #

async def resolve_track(query: str, requester: discord.Member) -> Track:
    """Turn a single search term or video URL into a playable Track."""
    search = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"

    def _extract() -> dict:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            return ydl.extract_info(search, download=False)

    info = await asyncio.to_thread(_extract)
    if "entries" in info:
        info = info["entries"][0]
    # Leave stream_url=None on purpose: the worker re-resolves a fresh (un-expired)
    # audio URL right before playback, which is what fixes the "no sound" on queued tracks.
    return Track(
        title=info.get("title", "Unknown"),
        url=info.get("webpage_url") or info.get("url"),
        stream_url=None,
        duration=info.get("duration", 0),
        requester=requester,
        thumbnail=_best_thumbnail(info),
    )


async def resolve_youtube_playlist(url: str, requester: discord.Member) -> tuple[str, list[Track]]:
    """Expand a YouTube playlist URL into Tracks (flat, fast)."""

    def _extract() -> dict:
        with yt_dlp.YoutubeDL(YDL_FLAT_OPTS) as ydl:
            return ydl.extract_info(url, download=False)

    info = await asyncio.to_thread(_extract)
    title = info.get("title", "YouTube playlist")
    entries = info.get("entries") or []
    tracks: list[Track] = []
    for e in entries:
        if len(tracks) >= MAX_PLAYLIST_TRACKS:
            break
        t = _track_from_flat(e, requester)
        if t:
            tracks.append(t)
    return title, tracks


# --------------------------------------------------------------------------- #
# Spotify resolution — NO client ID needed.
# Spotify's public /embed/ page ships the track names inside a __NEXT_DATA__
# JSON blob, so we read that (no auth) and match every name on YouTube.
# --------------------------------------------------------------------------- #

_SPOTIFY_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


def _spotify_id(url: str, kind: str) -> str | None:
    m = re.search(rf"open\.spotify\.com/(?:intl-[a-z]+/)?{kind}/([A-Za-z0-9]+)", url)
    return m.group(1) if m else None


async def _spotify_embed_entity(kind: str, sid: str) -> dict:
    """Fetch Spotify's public embed page and return the entity JSON (no auth)."""
    url = f"https://open.spotify.com/embed/{kind}/{sid}"
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            html = await resp.text()
    m = _SPOTIFY_NEXT_DATA.search(html)
    if not m:
        raise ValueError("Spotify page se data nahi padh paaya (shayad link private hai).")
    data = json.loads(m.group(1))
    return data["props"]["pageProps"]["state"]["data"]["entity"]


async def _spotify_search_names(names: list[str], requester: discord.Member) -> list[Track]:
    """YouTube-search each 'Artist Song' name (flat) and return Tracks."""
    tracks: list[Track] = []

    def _search(query: str) -> dict | None:
        with yt_dlp.YoutubeDL(YDL_FLAT_OPTS) as ydl:
            return ydl.extract_info(f"ytsearch1:{query}", download=False)

    for name in names:
        if not name.strip() or len(tracks) >= MAX_PLAYLIST_TRACKS:
            continue
        try:
            info = await asyncio.to_thread(_search, name)
        except Exception:  # noqa: BLE001
            continue
        entries = (info or {}).get("entries") or []
        if entries:
            t = _track_from_flat(entries[0], requester)
            if t:
                tracks.append(t)
    return tracks


async def resolve_spotify(url: str, requester: discord.Member) -> tuple[str, list[Track]]:
    """Resolve any Spotify track / playlist / album link into YouTube-matched Tracks."""
    for kind in ("playlist", "track", "album"):
        if f"/{kind}/" not in url:
            continue
        sid = _spotify_id(url, kind)
        if not sid:
            continue
        entity = await _spotify_embed_entity(kind, sid)
        names: list[str] = []

        if kind == "track":
            artists = ", ".join(a.get("name", "") for a in entity.get("artists", []))
            title = entity.get("title") or entity.get("name") or ""
            names.append(f"{artists} {title}".strip())
            pl_title = title or "Spotify track"
        else:
            pl_title = entity.get("title") or entity.get("name") or "Spotify"
            items = entity.get("trackList") or (entity.get("tracks") or {}).get("items", [])
            for it in items:
                song = it.get("title") or it.get("name") or ""
                artist = it.get("subtitle") or ""
                if not artist:
                    artist = ", ".join(a.get("name", "") for a in it.get("artists", []))
                if song:
                    names.append(f"{artist} {song}".strip())
                if len(names) >= MAX_PLAYLIST_TRACKS:
                    break

        tracks = await _spotify_search_names(names, requester)
        return pl_title, tracks

    raise ValueError("Ye sahi Spotify link nahi lag raha (track/playlist/album).")


# --------------------------------------------------------------------------- #
# Voice helper
# --------------------------------------------------------------------------- #

async def ensure_voice(inter: discord.Interaction) -> discord.VoiceClient | None:
    """Join the caller's voice channel; return the VoiceClient or None on failure."""
    member = inter.user
    voice_state = getattr(member, "voice", None)
    if voice_state is None or voice_state.channel is None:
        # play() already deferred this interaction, so we must reply via followup,
        # not inter.response.send_message (that raises "unknown interaction").
        await inter.followup.send(
            "❌ Pehle ek voice channel join karo, phir ye command chalao.", ephemeral=True
        )
        return None

    channel = voice_state.channel
    existing = inter.guild.voice_client
    if existing is not None:
        if existing.channel.id != channel.id:
            await existing.move_to(channel)
        return existing
    return await channel.connect()


# --------------------------------------------------------------------------- #
# Now-playing embed + control buttons
# --------------------------------------------------------------------------- #

def now_playing_embed(track: Track) -> discord.Embed:
    name = getattr(track.requester, "display_name", None) or getattr(track.requester, "name", "someone")
    embed = discord.Embed(
        title="🎵 Ab chal raha hai",
        description=f"**[{track.title}]({track.url})**\n⏱️ `{fmt_duration(track.duration)}`",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Requested by {name}")
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


class ControlView(discord.ui.View):
    """Full music controls — 2 rows of buttons on every now-playing message."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    def _player(self, inter: discord.Interaction) -> "Player | None":
        return bot.players.get(inter.guild.id)

    async def _no_music(self, inter: discord.Interaction) -> bool:
        p = self._player(inter)
        if p is None or p.current is None:
            await inter.response.send_message("❌ Abhi koi gaana nahi chal raha.", ephemeral=True)
            return True
        return False

    # ---------------- Row 0 ----------------
    @discord.ui.button(label="Vol -", emoji="🔉", style=discord.ButtonStyle.secondary, row=0)
    async def vol_down(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._no_music(inter):
            return
        v = self._player(inter).adjust_volume(-0.10)
        await inter.response.send_message(f"🔉 Volume: **{int(v * 100)}%**", ephemeral=True)

    @discord.ui.button(label="Back", emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._no_music(inter):
            return
        prev = self._player(inter).back()
        if prev is None:
            await inter.response.send_message("❌ Isse pehle koi gaana nahi hai.", ephemeral=True)
            return
        await inter.response.send_message(f"⏮️ Wapas chala raha hu: **{prev.title}**", ephemeral=True)

    @discord.ui.button(label="Pause/Resume", emoji="⏯️", style=discord.ButtonStyle.primary, row=0)
    async def pause_resume(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._no_music(inter):
            return
        p = self._player(inter)
        if p.is_paused():
            p.resume()
            await inter.response.send_message("▶️ Resume kar diya.", ephemeral=True)
        elif p.pause():
            await inter.response.send_message("⏸️ Pause kar diya.", ephemeral=True)
        else:
            await inter.response.send_message("❌ Abhi koi gaana nahi chal raha.", ephemeral=True)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.primary, row=0)
    async def skip(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._no_music(inter):
            return
        p = self._player(inter)
        title = p.current.title
        if p.voice and p.voice.is_playing():
            p.voice.stop()
        await inter.response.send_message(f"⏭️ Skip kiya: **{title}**", ephemeral=True)

    @discord.ui.button(label="Vol +", emoji="🔊", style=discord.ButtonStyle.secondary, row=0)
    async def vol_up(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._no_music(inter):
            return
        v = self._player(inter).adjust_volume(+0.10)
        await inter.response.send_message(f"🔊 Volume: **{int(v * 100)}%**", ephemeral=True)

    # ---------------- Row 1 ----------------
    @discord.ui.button(label="-10s", emoji="⏪", style=discord.ButtonStyle.secondary, row=1)
    async def back_10(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._no_music(inter):
            return
        pos = self._player(inter).seek(-10)
        if pos is None:
            await inter.response.send_message("❌ Seek nahi kar paaya.", ephemeral=True)
            return
        await inter.response.send_message(f"⏪ `{fmt_duration(pos)}` pe gaya.", ephemeral=True)

    @discord.ui.button(label="Track", emoji="🎵", style=discord.ButtonStyle.secondary, row=1)
    async def track_info(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._no_music(inter):
            return
        p = self._player(inter)
        await inter.response.send_message(embed=now_playing_embed(p.current), ephemeral=True)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=1)
    async def stop(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
        p = self._player(inter)
        if p is None:
            await inter.response.send_message("❌ Kuch nahi chal raha.", ephemeral=True)
            return
        await p.stop_and_disconnect()
        bot.players.pop(inter.guild.id, None)
        await inter.response.send_message("⏹️ Music band, voice channel chhod diya.", ephemeral=True)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.success, row=1)
    async def shuffle(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._no_music(inter):
            return
        n = self._player(inter).shuffle_queue()
        if n < 2:
            await inter.response.send_message("❌ Queue me shuffle karne ke liye gaane kam hain.", ephemeral=True)
            return
        await inter.response.send_message(f"🔀 Queue shuffle kar di — **{n} gaane**.", ephemeral=True)

    @discord.ui.button(label="+10s", emoji="⏩", style=discord.ButtonStyle.secondary, row=1)
    async def fwd_10(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._no_music(inter):
            return
        pos = self._player(inter).seek(+10)
        if pos is None:
            await inter.response.send_message("❌ Seek nahi kar paaya.", ephemeral=True)
            return
        await inter.response.send_message(f"⏩ `{fmt_duration(pos)}` pe gaya.", ephemeral=True)

    # Volume "slider" — Discord me real slider nahi hota, isliye dropdown se choose karo.
    @discord.ui.select(
        row=2,
        placeholder="🔊 Volume set karo…",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="Mute", value="0", emoji="🔇"),
            discord.SelectOption(label="25%", value="25"),
            discord.SelectOption(label="50%", value="50", emoji="🔈"),
            discord.SelectOption(label="75%", value="75"),
            discord.SelectOption(label="100%", value="100", emoji="🔉"),
            discord.SelectOption(label="150%", value="150"),
            discord.SelectOption(label="200%", value="200"),
            discord.SelectOption(label="250%", value="250"),
            discord.SelectOption(label="300%", value="300", emoji="🔊"),
            discord.SelectOption(label="400%", value="400"),
            discord.SelectOption(label="500% (bohot loud)", value="500", emoji="📢"),
        ],
    )
    async def volume_select(self, inter: discord.Interaction, select: discord.ui.Select) -> None:
        player = self._player(inter)
        if player is None:
            await inter.response.send_message("❌ Kuch nahi chal raha.", ephemeral=True)
            return
        percent = int(select.values[0])
        player.set_volume(percent / 100)
        label = "Mute" if percent == 0 else f"{percent}%"
        await inter.response.send_message(f"🔊 Volume: **{label}**", ephemeral=True)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

@tree.command(name="play", description="Gaana bajao: naam, YouTube link, ya Spotify/YouTube playlist")
@app_commands.describe(query="Gaane ka naam, YouTube/Spotify link, ya playlist link")
async def play(inter: discord.Interaction, query: str) -> None:
    await inter.response.defer()
    voice = await ensure_voice(inter)
    if voice is None:
        return

    player = bot.get_player(inter.guild, voice)
    player.text_channel = inter.channel
    q = query.strip()

    # ---- Playlist / album paths (queue many tracks) ----
    if "open.spotify.com" in q:
        try:
            title, tracks = await resolve_spotify(q, inter.user)
        except Exception as err:  # noqa: BLE001
            await inter.followup.send(f"❌ Spotify link se gaana nahi la paaya: `{err}`")
            return
        if not tracks:
            await inter.followup.send(f"❌ '{title}' me koi gaana nahi mila.")
            return
        for t in tracks:
            await player.queue.put(t)
        await inter.followup.send(f"➕ Spotify **{title}** queue me daal di — **{len(tracks)} gaane** 🎵")
        return

    if is_youtube_playlist(q):
        try:
            title, tracks = await resolve_youtube_playlist(q, inter.user)
        except Exception as err:  # noqa: BLE001
            await inter.followup.send(f"❌ YouTube playlist nahi la paaya: `{err}`")
            return
        if not tracks:
            await inter.followup.send(f"❌ '{title}' me koi gaana nahi mila.")
            return
        for t in tracks:
            await player.queue.put(t)
        await inter.followup.send(f"➕ Playlist **{title}** queue me daal di — **{len(tracks)} gaane** 🎵")
        return

    # ---- Single track (name or video link) ----
    try:
        track = await resolve_track(q, inter.user)
    except Exception as err:  # noqa: BLE001
        await inter.followup.send(f"❌ Ye gaana nahi mila / nahi chala: `{err}`")
        return

    await player.queue.put(track)
    if player.current is None and player.queue.qsize() == 1:
        await inter.followup.send(
            f"▶️ Abhi baja raha hu: **{track.title}** (`{fmt_duration(track.duration)}`)"
        )
    else:
        await inter.followup.send(
            f"➕ Queue me add ho gaya: **{track.title}** "
            f"(`{fmt_duration(track.duration)}`) — position #{player.queue.qsize()}"
        )


@tree.command(name="skip", description="Current gaana skip karo")
async def skip(inter: discord.Interaction) -> None:
    player = bot.players.get(inter.guild.id)
    if player is None or player.current is None:
        await inter.response.send_message("❌ Abhi koi gaana nahi chal raha.", ephemeral=True)
        return
    title = player.current.title
    if inter.guild.voice_client and inter.guild.voice_client.is_playing():
        inter.guild.voice_client.stop()  # triggers _after -> next track
    await inter.response.send_message(f"⏭️ Skip kiya: **{title}**")


@tree.command(name="stop", description="Music band karo aur voice channel chhod do")
async def stop(inter: discord.Interaction) -> None:
    player = bot.players.get(inter.guild.id)
    if player is None:
        await inter.response.send_message("❌ Kuch nahi chal raha.", ephemeral=True)
        return
    await player.stop_and_disconnect()
    bot.players.pop(inter.guild.id, None)
    await inter.response.send_message("⏹️ Music band, voice channel chhod diya.")


@tree.command(name="pause", description="Gaana rok do (pause)")
async def pause(inter: discord.Interaction) -> None:
    player = bot.players.get(inter.guild.id)
    if player is None or not player.pause():
        await inter.response.send_message("❌ Abhi koi gaana nahi chal raha.", ephemeral=True)
        return
    await inter.response.send_message("⏸️ Pause kar diya. `/resume` se wapas chalega.")


@tree.command(name="resume", description="Ruka hua gaana wapas chalao")
async def resume(inter: discord.Interaction) -> None:
    player = bot.players.get(inter.guild.id)
    if player is None or not player.resume():
        await inter.response.send_message("❌ Kuch paused nahi hai.", ephemeral=True)
        return
    await inter.response.send_message("▶️ Wapas chala diya.")


@tree.command(name="np", description="Abhi kya chal raha hai")
async def now_playing(inter: discord.Interaction) -> None:
    player = bot.players.get(inter.guild.id)
    if player is None or player.current is None:
        await inter.response.send_message("❌ Abhi kuch nahi chal raha.", ephemeral=True)
        return
    player.text_channel = inter.channel
    await inter.response.send_message(
        embed=now_playing_embed(player.current), view=ControlView()
    )


@tree.command(name="queue", description="Aage ke gaane dikhao")
async def queue(inter: discord.Interaction) -> None:
    player = bot.players.get(inter.guild.id)
    if player is None or (player.current is None and player.queue.empty()):
        await inter.response.send_message("❌ Queue khali hai.", ephemeral=True)
        return
    lines = []
    if player.current:
        lines.append(f"🎵 **Abhi:** {player.current.title}")
    upcoming = list(player.queue._queue)  # peek without consuming
    if upcoming:
        lines.append(f"**Aage ({len(upcoming)} gaane):**")
        for i, t in enumerate(upcoming[:10], 1):
            lines.append(f"{i}. {t.title} (`{fmt_duration(t.duration)}`)")
        if len(upcoming) > 10:
            lines.append(f"... aur {len(upcoming) - 10} gaane")
    await inter.response.send_message("\n".join(lines))


@tree.command(name="volume", description="Volume set karo (0-500)")
@app_commands.describe(percent="0 se 500 tak")
async def volume(inter: discord.Interaction, percent: app_commands.Range[int, 0, 500]) -> None:
    player = bot.players.get(inter.guild.id)
    if player is None:
        await inter.response.send_message("❌ Pehle `/play` chalao.", ephemeral=True)
        return
    player.set_volume(percent / 100)
    await inter.response.send_message(f"🔊 Volume: **{percent}%**")


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    log.info("Bot ready. Use /play in a server.")


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN nahi mila. .env file banao aur usme DISCORD_TOKEN=... daalo."
        )
    bot.run(token)


if __name__ == "__main__":
    main()
