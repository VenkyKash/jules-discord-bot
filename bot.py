#!/usr/bin/env python3
"""
Jules Discord Bot Bridge
------------------------
Enables interactive 2-way chat with Google Jules from Discord.
Runs continuously on Render (or local Mac) independent of Jules' ephemeral VMs.
"""

from __future__ import annotations

import asyncio
import os
import sys
import logging
from typing import Dict, Any, Optional

import discord
from discord.ext import commands
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("jules_bot")

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
JULES_API_KEY = os.getenv("JULES_API_KEY")
GITHUB_REPO = os.getenv("GITHUB_REPO", "VenkyKash/jev-use-exp")
DEFAULT_BRANCH = os.getenv("DEFAULT_BRANCH", "jules")

JULES_API_BASE = "https://jules.googleapis.com/v1alpha"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Track active sessions mapped to discord thread IDs: thread_id -> session_dict
active_sessions: Dict[int, Dict[str, Any]] = {}


def get_jules_headers() -> Dict[str, str]:
    if not JULES_API_KEY:
        raise ValueError("Missing JULES_API_KEY environment variable")
    return {
        "x-goog-api-key": JULES_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": "Jules-Discord-Bot/1.0",
    }


async def resolve_source_name(client: httpx.AsyncClient) -> str:
    """Finds the source name for the configured repo from Jules API."""
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
    # Default fallback construct
    return f"sources/github/{GITHUB_REPO}"


class PlanApprovalView(discord.ui.View):
    def __init__(self, session_id: str, thread: discord.Thread):
        super().__init__(timeout=None)
        self.session_id = session_id
        self.thread = thread

    @discord.ui.button(label="Approve Plan & Run", style=discord.ButtonStyle.green, emoji="✅")
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        button.disabled = True
        button.label = "Plan Approved"
        await interaction.message.edit(view=self)

        async with httpx.AsyncClient(timeout=30.0) as client:
            url = f"{JULES_API_BASE}/sessions/{self.session_id}:approvePlan"
            try:
                resp = await client.post(url, headers=get_jules_headers())
                resp.raise_for_status()
                await self.thread.send(" Plan approved! Jules is starting execution...")
            except Exception as e:
                logger.exception("Failed to approve plan")
                await self.thread.send(f"⚠️ Failed to approve plan: `{e}`")


async def poll_session_activities(session_id: str, thread: discord.Thread):
    """Background task polling Jules activities for progress updates."""
    seen_activity_ids = set()
    consecutive_errors = 0

    while True:
        await asyncio.sleep(5)
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                # 1. Check session status
                session_url = f"{JULES_API_BASE}/sessions/{session_id}"
                session_resp = await client.get(session_url, headers=get_jules_headers())
                if session_resp.status_code == 200:
                    sdata = session_resp.json()
                    # Check if PR created
                    for out in sdata.get("outputs", []):
                        pr = out.get("pullRequest")
                        if pr and pr.get("url") not in seen_activity_ids:
                            seen_activity_ids.add(pr.get("url"))
                            await thread.send(
                                f"🎉 **Jules created a Pull Request!**\n"
                                f"**Title:** {pr.get('title')}\n"
                                f"**Link:** {pr.get('url')}\n"
                                f"_{pr.get('description', '')[:300]}_"
                            )

                # 2. Check activities stream
                act_url = f"{JULES_API_BASE}/sessions/{session_id}/activities?pageSize=15"
                act_resp = await client.get(act_url, headers=get_jules_headers())
                if act_resp.status_code == 200:
                    adata = act_resp.json()
                    for act in adata.get("activities", []):
                        aid = act.get("id") or act.get("name")
                        if aid in seen_activity_ids:
                            continue
                        seen_activity_ids.add(aid)

                        # 1. Plan Generated
                        if "planGenerated" in act:
                            plan_data = act["planGenerated"].get("plan", {})
                            steps = plan_data.get("steps", [])
                            steps_str = "\n".join([f"{s.get('index', i)+1}. **{s.get('title')}**: {s.get('description')}" for i, s in enumerate(steps)])
                            view = PlanApprovalView(session_id, thread)
                            await thread.send(
                                f"📋 **Jules generated a Plan:**\n{steps_str[:1800]}",
                                view=view,
                            )
                        # 2. Agent Messaged
                        elif "agentMessaged" in act:
                            msg = act["agentMessaged"].get("agentMessage", "")
                            if msg:
                                await thread.send(f"🤖 **Jules:** {msg[:1900]}")
                        # 3. Progress Updated
                        elif "progressUpdated" in act:
                            p = act["progressUpdated"]
                            await thread.send(f"⏳ **Progress:** {p.get('title', '')} - _{p.get('description', '')}_")
                        # 4. Session Completed
                        elif "sessionCompleted" in act:
                            await thread.send("🏁 **Session completed successfully!**")
                            break
                        # 5. Session Failed
                        elif "sessionFailed" in act:
                            reason = act["sessionFailed"].get("reason", "Unknown error")
                            await thread.send(f"❌ **Session failed:** {reason}")
                            break

                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors > 5:
                    logger.error(f"Polling error for session {session_id}: {e}")


@bot.event
async def on_ready():
    logger.info(f"Jules Discord Bot logged in as {bot.user} (ID: {bot.user.id})")


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

            # Create a dedicated Discord Thread for this session
            thread = await ctx.message.create_thread(
                name=f"Jules: {prompt[:40]}",
                auto_archive_duration=1440,
            )

            active_sessions[thread.id] = {
                "session_id": session_id,
                "prompt": prompt,
            }

            await status_msg.edit(content=f" Session `{session_id}` started! Follow along in {thread.mention}.")
            await thread.send(
                f"**Task Prompt:** {prompt}\n"
                f"**Repo:** `{GITHUB_REPO}` (Branch: `{DEFAULT_BRANCH}`)\n"
                f"Any message you reply inside this thread will be sent directly to Jules!"
            )

            # Start background poller
            bot.loop.create_task(poll_session_activities(session_id, thread))

        except Exception as e:
            logger.exception("Failed to start Jules session")
            await status_msg.edit(content=f"❌ Failed to start Jules session: `{e}`")


@bot.event
async def on_message(message: discord.Message):
    # Ignore bot messages
    if message.author.bot:
        return

    # Check if this message is inside a registered Jules task thread
    if isinstance(message.channel, discord.Thread) and message.channel.id in active_sessions:
        session_info = active_sessions[message.channel.id]
        session_id = session_info["session_id"]

        # Forward user message to Jules
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


def main():
    if not DISCORD_BOT_TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN environment variable not set.", file=sys.stderr)
        sys.exit(1)
    if not JULES_API_KEY:
        print("ERROR: JULES_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
