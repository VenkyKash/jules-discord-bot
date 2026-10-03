# Jules Discord Bot Bridge

Interactive 2-way Discord bridge for [Google Jules](https://jules.google).

Enables assigning tasks, discussing code, approving plans, and receiving Pull Request alerts directly inside Discord without needing your Mac awake.

---

## Features
- **`!jules <prompt>`**: Dispatches a new task to Jules on GitHub repo `VenkyKash/jev-use-exp` (`jules` branch).
- **Auto-Thread Creation**: Creates a dedicated Discord thread for each task session to keep conversation organized.
- **Interactive Plan Approval**: Renders a clickable **`[Approve Plan & Run]`** button when Jules generates a plan.
- **Live Progress & Streaming**: Streams Jules' agent messages and task updates into the Discord thread.
- **Two-Way Thread Chat**: Any message you send inside the task's Discord thread is immediately forwarded to Jules via `POST /v1alpha/sessions/{id}:sendMessage`.
- **Completion Alerts**: Automatically posts the created GitHub Pull Request link and summary when Jules finishes.

---

## 24/7 Deployment on Render (Option B)

This repo includes a preconfigured [`render.yaml`](../../render.yaml) blueprint.

### Step 1: Get Your API Tokens
1. **Discord Bot Token**:
   - Go to [discord.com/developers/applications](https://discord.com/developers/applications) ➔ **New Application**.
   - Under **Bot**:
     - Click **Reset Token** and copy the token (`DISCORD_BOT_TOKEN`).
     - Under **Privileged Gateway Intents**, enable **`Message Content Intent`**.
   - Under **OAuth2 ➔ URL Generator**:
     - Check `bot`.
     - Under permissions: `Send Messages`, `Create Public Threads`, `Send Messages in Threads`, `Read Message History`, `Add Reactions`.
     - Open the generated URL in your browser to invite the bot to your Discord server.
2. **Jules API Key**:
   - Visit [jules.google.com/settings#api](https://jules.google.com/settings#api) ➔ Create API key (`JULES_API_KEY`).

### Step 2: Deploy to Render
1. Go to [dashboard.render.com](https://dashboard.render.com/) ➔ **New ➔ Blueprint**.
2. Connect your GitHub repository: `VenkyKash/jev-use-exp`.
3. Render will detect [`render.yaml`](../../render.yaml) and prompt you to fill in:
   - `DISCORD_BOT_TOKEN`: (paste from Step 1)
   - `JULES_API_KEY`: (paste from Step 1)
4. Click **Apply**. Render will build the lightweight worker container and keep it running 24/7 for free!
