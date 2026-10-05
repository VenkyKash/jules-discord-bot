#!/usr/bin/env python3
"""
Jules Discord Bot Bridge
------------------------
Enables interactive 2-way chat with Google Jules from Discord.
Runs continuously on Render with channel / thread management.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional

import discord
from discord.ext import commands
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("jules_bot")

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
JULES_API_KEY = os.getenv("JULES_API_KEY")
GITHUB_REPO = os.getenv("GITHUB_REPO", "VenkyKash/jev-use-exp")
DEFAULT_BRANCH = os.getenv("DEFAULT_BRANCH", "jules")
CATEGORY_NAME = os.getenv("CATEGORY_NAME", "🤖 JULES SESSIONS")

JULES_API_BASE = "https://jules.googleapis.com/v1alpha"

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Track active sessions: channel_or_thread_id -> session_dict
active_conversations: Dict[int, Dict[str, Any]] = {}
tracked_session_ids: set[str] = set()


def get_jules_headers() -> Dict[str, str]:
    if not JULES_API_KEY:
        raise ValueError("Missing JULES_API_KEY environment variable")
    return {
        "x-goog-api-key": JULES_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": "Jules-Discord-Bot/3.0",
    }



def is_session_existing_in_discord(guild: discord.Guild, session_id: str) -> bool:
    for ch in guild.channels:
        if isinstance(ch, discord.TextChannel) and ch.topic and f"session:{session_id}" in ch.topic:
            return True
        if hasattr(ch, "threads"):
            for th in ch.threads:
                if th.id in active_conversations and active_conversations[th.id].get("session_id") == session_id:
                    return True
    return False

def slugify(title: str, max_len: int = 25) -> str:
    clean = re.sub(r"[^a-zA-Z0-9\s-]", "", title).strip().lower()
    slug = re.sub(r"[\s-]+", "-", clean)[:max_len].strip("-")
    return slug or "session"


async def get_or_create_category(guild: discord.Guild) -> Optional[discord.CategoryChannel]:
    """Finds or creates category if bot has manage_channels permission."""
    try:
        for cat in guild.categories:
            if cat.name.lower() == CATEGORY_NAME.lower():
                return cat
        if guild.me.guild_permissions.manage_channels:
            return await guild.create_category(name=CATEGORY_NAME)
    except Exception as e:
        logger.warning(f"Could not create category '{CATEGORY_NAME}': {e}")
    return None


async def resolve_source_name(client: httpx.AsyncClient) -> str:
    url = f"{JULES_API_BASE}/sources"
    resp = await client.get(url, headers=get_jules_headers())
    resp.raise_for_status()
    data = resp.json()
    for src in data.get("sources", []):
        gh = src.get("githubRepo", {})
        if f"{gh.get('owner')}/{gh.get('repo')}".lower() == GITHUB_REPO.lower():
            return src.get("name")
        if src.get("id", "").lower().endswith(GITHUB_REPO.lower()):
            return src.get("name")
    return f"sources/github/{GITHUB_REPO}"


class PlanApprovalView(discord.ui.View):
    def __init__(self, session_id: str, target: discord.abc.Messageable):
        super().__init__(timeout=None)
        self.session_id = session_id
        self.target = target

    @discord.ui.button(label="Approve Plan & Run", style=discord.ButtonStyle.green, emoji="✅")
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        button.disabled = True
        button.label = "Plan Approved"
        button.style = discord.ButtonStyle.secondary
        await interaction.message.edit(view=self)

        async with httpx.AsyncClient(timeout=30.0) as client:
            url = f"{JULES_API_BASE}/sessions/{self.session_id}:approvePlan"
            try:
                resp = await client.post(url, headers=get_jules_headers())
                resp.raise_for_status()
                embed = discord.Embed(
                    title="Plan Approved",
                    description="Jules has been authorized and is now executing the plan.",
                    color=0x2ECC71,
                )
                await self.target.send(embed=embed)
            except Exception as e:
                logger.exception("Failed to approve plan")
                await self.target.send(f"⚠️ Failed to approve plan: `{e}`")


async def poll_session_activities(session_id: str, target: discord.abc.Messageable, prompt: str = ""):
    """Polls Jules session and populates target channel/thread with live updates."""
    seen_activity_ids = set()
    consecutive_errors = 0
    last_progress_text = ""

    status_embed = discord.Embed(
        title="🤖 Jules Session Status",
        description="Initializing container environment...",
        color=0x3498DB,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    status_embed.add_field(name="Session ID", value=f"`{session_id}`", inline=True)
    status_embed.add_field(name="Status", value="⚙️ Starting", inline=True)
    status_embed.add_field(name="Branch", value=f"`{DEFAULT_BRANCH}`", inline=True)
    if prompt:
        status_embed.add_field(name="Task Prompt", value=f"_{prompt[:300]}_", inline=False)
    status_embed.set_footer(text="Live status card • Google Jules")

    status_card: Optional[discord.Message] = None
    try:
        status_card = await target.send(embed=status_embed)
        if isinstance(target, discord.TextChannel):
            try:
                await status_card.pin()
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Could not post initial status card: {e}")

    while True:
        await asyncio.sleep(6)
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                session_url = f"{JULES_API_BASE}/sessions/{session_id}"
                session_resp = await client.get(session_url, headers=get_jules_headers())
                if session_resp.status_code == 200:
                    sdata = session_resp.json()
                    for out in sdata.get("outputs", []):
                        pr = out.get("pullRequest")
                        if pr and pr.get("url") not in seen_activity_ids:
                            seen_activity_ids.add(pr.get("url"))
                            pr_embed = discord.Embed(
                                title="🎉 Pull Request Ready!",
                                url=pr.get("url"),
                                description=f"**{pr.get('title')}**\n\n{pr.get('description', '')[:600]}",
                                color=0x2ECC71,
                                timestamp=datetime.datetime.now(datetime.timezone.utc),
                            )
                            pr_embed.add_field(name="GitHub URL", value=f"[Open Pull Request]({pr.get('url')})", inline=False)
                            await target.send(embed=pr_embed)

                            if status_card:
                                status_embed.color = 0x2ECC71
                                status_embed.set_field_at(1, name="Status", value="🟢 PR Created", inline=True)
                                try:
                                    await status_card.edit(embed=status_embed)
                                except Exception:
                                    pass

                act_url = f"{JULES_API_BASE}/sessions/{session_id}/activities?pageSize=50"
                act_resp = await client.get(act_url, headers=get_jules_headers())
                if act_resp.status_code == 200:
                    adata = act_resp.json()
                    
                    activities = adata.get("activities", [])
                    page_token = adata.get("nextPageToken")
                    
                    while page_token:
                        next_url = f"{JULES_API_BASE}/sessions/{session_id}/activities?pageSize=50&pageToken={page_token}"
                        next_resp = await client.get(next_url, headers=get_jules_headers())
                        if next_resp.status_code != 200:
                            break
                        next_data = next_resp.json()
                        activities.extend(next_data.get("activities", []))
                        page_token = next_data.get("nextPageToken")

                    for act in activities:
                        aid = act.get("id") or act.get("name")
                        if not aid:
                            aid = str(act)
                        
                        if aid in seen_activity_ids:
                            continue
                        seen_activity_ids.add(aid)

                        if "userMessaged" in act:
                            u_msg = act["userMessaged"].get("userMessage", "")
                            if u_msg:
                                u_embed = discord.Embed(
                                    description=f"💬 **User:** {u_msg[:1900]}",
                                    color=0x95A5A6,
                                )
                                await target.send(embed=u_embed)

                        elif "agentMessaged" in act:
                            msg = act["agentMessaged"].get("agentMessage", "")
                            if msg:
                                a_embed = discord.Embed(
                                    title="🤖 Jules",
                                    description=msg[:1900],
                                    color=0x9B59B6,
                                )
                                await target.send(embed=a_embed)

                        elif "planGenerated" in act:
                            plan_data = act["planGenerated"].get("plan", {})
                            steps = plan_data.get("steps", [])
                            steps_lines = []
                            for i, s in enumerate(steps):
                                idx = s.get("index", i) + 1
                                steps_lines.append(f"**{idx}. {s.get('title')}**\n_{s.get('description', '')}_")

                            plan_embed = discord.Embed(
                                title="📋 Proposed Plan",
                                description="\n\n".join(steps_lines)[:3800] or "No step details provided.",
                                color=0xF1C40F,
                            )
                            plan_embed.set_footer(text="Click below to approve and run")
                            view = PlanApprovalView(session_id, target)
                            await target.send(embed=plan_embed, view=view)

                            if status_card:
                                status_embed.color = 0xF1C40F
                                status_embed.set_field_at(1, name="Status", value="🟡 Awaiting Plan Approval", inline=True)
                                try:
                                    await status_card.edit(embed=status_embed)
                                except Exception:
                                    pass

                        elif "progressUpdated" in act:
                            p = act["progressUpdated"]
                            title_step = p.get("title", "")
                            desc_step = p.get("description", "")
                            curr_text = f"{title_step}: {desc_step}"
                            if curr_text != last_progress_text:
                                last_progress_text = curr_text
                                if status_card:
                                    status_embed.description = f"**Current Step:** {title_step}\n_{desc_step}_"
                                    status_embed.color = 0x3498DB
                                    status_embed.set_field_at(1, name="Status", value="⚡ Working...", inline=True)
                                    status_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
                                    try:
                                        await status_card.edit(embed=status_embed)
                                    except Exception:
                                        pass

                        elif "sessionCompleted" in act:
                            if status_card:
                                status_embed.description = " Task completed successfully!"
                                status_embed.color = 0x2ECC71
                                status_embed.set_field_at(1, name="Status", value="🟢 Completed", inline=True)
                                try:
                                    await status_card.edit(embed=status_embed)
                                except Exception:
                                    pass
                            return

                        elif "sessionFailed" in act:
                            reason = act["sessionFailed"].get("reason", "Unknown error")
                            fail_embed = discord.Embed(
                                title="❌ Session Failed",
                                description=f"```{reason[:1800]}```",
                                color=0xE74C3C,
                            )
                            await target.send(embed=fail_embed)
                            if status_card:
                                status_embed.description = f"❌ Execution failed: {reason}"
                                status_embed.color = 0xE74C3C
                                status_embed.set_field_at(1, name="Status", value="🔴 Failed", inline=True)
                                try:
                                    await status_card.edit(embed=status_embed)
                                except Exception:
                                    pass
                            return

                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors > 8:
                    logger.error(f"Polling error for session {session_id}: {e}")


async def create_conversation_target(guild: discord.Guild, session_id: str, title: str, prompt: str = ""):
    """Tries to create a dedicated channel, falls back gracefully to a thread if permissions are missing."""
    target_channel = None
    for ch in guild.text_channels:
        if ch.permissions_for(guild.me).send_messages:
            target_channel = ch
            break

    if not target_channel:
        logger.warning(f"No writable text channel found in guild {guild.name}")
        return

    # 1. Try creating a dedicated channel if bot has manage_channels permission
    if guild.me.guild_permissions.manage_channels:
        try:
            category = await get_or_create_category(guild)
            ch_slug = f"jules-{slugify(title)}"
            channel = await guild.create_text_channel(
                name=ch_slug,
                category=category,
                topic=f"Jules Task Session | session:{session_id} | Repo: {GITHUB_REPO}",
            )
            active_conversations[channel.id] = {"session_id": session_id, "prompt": prompt or title}
            tracked_session_ids.add(session_id)
            bot.loop.create_task(poll_session_activities(session_id, channel, prompt or title))
            logger.info(f"Created dedicated channel #{channel.name} for session {session_id}")
            return
        except Exception as e:
            logger.warning(f"Failed to create dedicated channel, falling back to thread: {e}")

    # 2. Fallback: Create a Discord Thread in target_channel
    try:
        announce_embed = discord.Embed(
            title="🔔 Jules Session Detected",
            description=f"**{title}**",
            color=0x3498DB,
        )
        announce_embed.add_field(name="Session ID", value=f"`{session_id}`", inline=True)
        announce_embed.add_field(name="Branch", value=f"`{DEFAULT_BRANCH}`", inline=True)

        msg = await target_channel.send(embed=announce_embed)
        thread = await msg.create_thread(
            name=f"Jules: {title[:32]}",
            auto_archive_duration=1440,
        )
        active_conversations[thread.id] = {"session_id": session_id, "prompt": prompt or title}
        tracked_session_ids.add(session_id)
        bot.loop.create_task(poll_session_activities(session_id, thread, prompt or title))
        logger.info(f"Created thread #{thread.name} for session {session_id}")
    except Exception as e:
        logger.error(f"Failed to create fallback thread for session {session_id}: {e}")


async def auto_sync_loop():
    """Continuously checks Jules API and attaches to any new sessions."""
    await bot.wait_until_ready()
    logger.info("Auto-sync loop running.")

    while not bot.is_closed():
        try:
            for guild in bot.guilds:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(f"{JULES_API_BASE}/sessions?pageSize=15", headers=get_jules_headers())
                    if resp.status_code == 200:
                        data = resp.json()
                        sessions = data.get("sessions", [])
                        for s in sessions:
                            sid = s.get("id") or s.get("name", "").split("/")[-1]
                            title = s.get("title") or s.get("prompt", f"Session {sid}")[:40]

                            if is_session_existing_in_discord(guild, sid):
                                continue

                            await create_conversation_target(guild, sid, title, s.get("prompt", ""))
        except Exception as e:
            logger.warning(f"Auto-sync loop error: {e}")

        await asyncio.sleep(20)


@bot.event
async def on_ready():
    logger.info(f"Jules Discord Bot logged in as {bot.user} (ID: {bot.user.id})")
    
    # Re-attach to existing channels after restart
    for guild in bot.guilds:
        for ch in guild.channels:
            if isinstance(ch, discord.TextChannel) and ch.topic and "session:" in ch.topic:
                m = re.search(r"session:([^\s|]+)", ch.topic)
                if m:
                    sid = m.group(1)
                    if sid not in tracked_session_ids:
                        active_conversations[ch.id] = {"session_id": sid, "prompt": "Resumed Session"}
                        tracked_session_ids.add(sid)
                        bot.loop.create_task(poll_session_activities(sid, ch, "Resumed Session"))
                        logger.info(f"Resumed tracking session {sid} on channel #{ch.name}")

    if not hasattr(bot, "_autosync_started"):
        bot._autosync_started = True
        bot.loop.create_task(auto_sync_loop())


@bot.command(name="jules")
async def start_jules_task(ctx: commands.Context, *, prompt: str):
    """Starts a new Jules session on the repository: !jules <prompt>"""
    if not prompt:
        await ctx.reply("Please provide a prompt for Jules. Example: `!jules Fix compound clicks on Linux`")
        return

    status_msg = await ctx.reply(f"🚀 Initializing task with Jules on `{GITHUB_REPO}` ({DEFAULT_BRANCH})...")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            source_name = await resolve_source_name(client)
            payload = {
                "prompt": prompt,
                "sourceContext": {
                    "source": source_name,
                    "githubRepoContext": {
                        "startingBranch": DEFAULT_BRANCH,
                    },
                },
                "automationMode": "AUTO_CREATE_PR",
                "requirePlanApproval": True,
                "title": prompt[:50],
            }

            resp = await client.post(
                f"{JULES_API_BASE}/sessions",
                headers=get_jules_headers(),
                json=payload,
            )
            resp.raise_for_status()
            sdata = resp.json()
            session_id = sdata.get("id") or sdata.get("name", "").split("/")[-1]

            await create_conversation_target(ctx.guild, session_id, prompt[:30], prompt)
            await status_msg.edit(content=f" Session `{session_id}` started and synced to your Discord server!")

        except Exception as e:
            logger.exception("Failed to start Jules session")
            await status_msg.edit(content=f"❌ Failed to start Jules session: `{e}`")


@bot.command(name="sync")
async def sync_jules_sessions(ctx: commands.Context):
    """Manually triggers a sync scan of Jules sessions."""
    status_msg = await ctx.reply("🔍 Scanning Jules API for sessions...")
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(f"{JULES_API_BASE}/sessions?pageSize=15", headers=get_jules_headers())
            resp.raise_for_status()
            data = resp.json()
            sessions = data.get("sessions", [])

            synced_count = 0
            for s in sessions:
                sid = s.get("id") or s.get("name", "").split("/")[-1]
                title = s.get("title") or s.get("prompt", f"Session {sid}")[:40]

                if is_session_existing_in_discord(ctx.guild, sid):
                    if sid not in tracked_session_ids:
                        # Resume tracking if it exists but isn't actively polled in memory
                        for ch in ctx.guild.channels:
                            if isinstance(ch, discord.TextChannel) and ch.topic and f"session:{sid}" in ch.topic:
                                active_conversations[ch.id] = {"session_id": sid, "prompt": title}
                                tracked_session_ids.add(sid)
                                bot.loop.create_task(poll_session_activities(sid, ch, title))
                                synced_count += 1
                                break
                    continue

                await create_conversation_target(ctx.guild, sid, title, s.get("prompt", ""))
                synced_count += 1

            if synced_count > 0:
                await status_msg.edit(content=f" Synced {synced_count} Jules session(s)!")
            else:
                await status_msg.edit(content=" All sessions are already tracked.")

        except Exception as e:
            logger.exception("Failed to sync sessions")
            await status_msg.edit(content=f"❌ Error syncing sessions: `{e}`")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Two-way forwarding: works for both dedicated text channels and threads
    if message.channel.id in active_conversations:
        session_info = active_conversations[message.channel.id]
        session_id = session_info["session_id"]

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                url = f"{JULES_API_BASE}/sessions/{session_id}:sendMessage"
                resp = await client.post(
                    url,
                    headers=get_jules_headers(),
                    json={"prompt": message.content},
                )
                resp.raise_for_status()
                await message.add_reaction("📨")
            except Exception as e:
                logger.exception("Failed to forward message to Jules")
                await message.channel.send(f"⚠️ Failed to send message to Jules: `{e}`")
        return

    await bot.process_commands(message)


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Health check web server running on port {port}")
    server.serve_forever()


def main():
    threading.Thread(target=run_health_server, daemon=True).start()

    if not DISCORD_BOT_TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN environment variable not set.", file=sys.stderr)
        sys.exit(1)
    if not JULES_API_KEY:
        print("ERROR: JULES_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
