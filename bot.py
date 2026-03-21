import asyncio
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from motor.motor_asyncio import AsyncIOMotorClient

# ─── Config ───────────────────────────────────────────────────────
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0"))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MONGO_URI = os.environ.get("MONGO_URI", "")
PORT      = int(os.environ.get("PORT", 8080))

# ─── MongoDB ──────────────────────────────────────────────────────
mongo_client = AsyncIOMotorClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
db        = mongo_client["telebot"]
users_col = db["users"]

# ─── State ────────────────────────────────────────────────────────
user_map = {}   # forwarded_msg_id -> user_id

# Broadcast steps
BC_IDLE    = 0
BC_WAIT    = 1   # waiting for message to broadcast
BC_BUTTONS = 2   # collecting buttons
BC_CONFIRM = 3   # waiting for confirm/cancel


# ─── Keep-Alive ───────────────────────────────────────────────────
class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a): pass

def _web():
    HTTPServer(("0.0.0.0", PORT), _H).serve_forever()


# ─── DB helpers ───────────────────────────────────────────────────
async def save_user(uid: int, name: str):
    await users_col.update_one({"_id": uid}, {"$set": {"name": name}}, upsert=True)

async def all_user_ids():
    return [d["_id"] async for d in users_col.find({})]


# ─── Keyboards ────────────────────────────────────────────────────
def kb_broadcast_options():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Add URL Buttons", callback_data="bc_addbtns"),
        InlineKeyboardButton("📤 Send Now",        callback_data="bc_sendnow"),
    ],[
        InlineKeyboardButton("❌ Cancel Broadcast", callback_data="bc_cancel"),
    ]])

def kb_confirm(btn_count: int):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm & Send", callback_data="bc_confirm"),
        InlineKeyboardButton("❌ Cancel",         callback_data="bc_cancel"),
    ],[
        InlineKeyboardButton("➕ Add More Buttons" if btn_count < 10 else "🔒 Max Buttons Reached",
                             callback_data="bc_addbtns" if btn_count < 10 else "bc_noop"),
    ]])

def kb_cancel_only():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel Broadcast", callback_data="bc_cancel"),
    ]])


# ─── .cmd menu text ───────────────────────────────────────────────
CMD_MENU = (
    "📋 *Command Menu*\n\n"
    "`.cmd` — Show this menu\n"
    "`.stats` — Total users count\n"
    "`.users` — List all users with IDs\n"
    "`.broadcast` — Send message to all users\n\n"
    "💬 *Replying to users:*\n"
    "Just reply to any forwarded message to send back to that user\n\n"
    "📢 *Broadcast guide:*\n"
    "1. Send `.broadcast`\n"
    "2. Send any message (text/photo/video/sticker/voice/file)\n"
    "3. See preview → choose *Add Buttons* or *Send Now*\n"
    "4. If adding buttons, send each one as:\n"
    "   `Button Name - https://link.com`\n"
    "5. Send `.done` when finished adding buttons\n"
    "6. Confirm & Send to all users ✅"
)


# ─── Broadcast state helpers ──────────────────────────────────────
def bc(ctx): return ctx.user_data.setdefault("bc", {"step": BC_IDLE, "buttons": []})

def bc_reset(ctx):
    ctx.user_data["bc"] = {"step": BC_IDLE, "buttons": []}


# ─── MAIN HANDLER ────────────────────────────────────────────────
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    uid  = msg.from_user.id
    text = (msg.text or "").strip()

    # ══════════════ ADMIN ═══════════════
    if uid == ADMIN_ID:
        state = bc(context)

        # ── Broadcast: waiting for the message to broadcast ──
        if state["step"] == BC_WAIT:
            state["chat_id"] = msg.chat_id
            state["msg_id"]  = msg.message_id
            state["step"]    = BC_CONFIRM  # will become CONFIRM after showing preview

            await msg.reply_text("👁 *Preview of your broadcast:*", parse_mode="Markdown")
            await context.bot.copy_message(
                chat_id=ADMIN_ID,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id
            )
            total = await users_col.count_documents({})
            await msg.reply_text(
                f"📢 *Broadcast Preview*\n\n"
                f"👥 Will be sent to *{total} users*\n\n"
                f"Choose an option below:",
                parse_mode="Markdown",
                reply_markup=kb_broadcast_options()
            )
            return

        # ── Broadcast: collecting buttons ──
        if state["step"] == BC_BUTTONS:
            if text.lower() == ".done":
                await _show_confirm(msg, context)
                return

            if len(state["buttons"]) >= 10:
                await msg.reply_text(
                    "⚠️ Max 10 buttons reached!\nSend `.done` to continue.",
                    reply_markup=kb_cancel_only()
                )
                return

            if " - " not in text:
                await msg.reply_text(
                    "❌ Wrong format! Send as:\n`Button Name - https://example.com`\n\nOr send `.done` to finish.",
                    parse_mode="Markdown",
                    reply_markup=kb_cancel_only()
                )
                return

            name, url = text.split(" - ", 1)
            name, url = name.strip(), url.strip()

            if not url.startswith(("http://", "https://")):
                await msg.reply_text(
                    "❌ URL must start with `http://` or `https://`",
                    parse_mode="Markdown",
                    reply_markup=kb_cancel_only()
                )
                return

            state["buttons"].append({"name": name, "url": url})
            remaining = 10 - len(state["buttons"])
            await msg.reply_text(
                f"✅ *Button {len(state['buttons'])} added!*\n\n"
                f"Current buttons:\n" +
                "\n".join(f"  {i+1}. {b['name']}" for i, b in enumerate(state["buttons"])) +
                f"\n\n{'Send `.done` to finish.' if remaining == 0 else f'{remaining} more slots. Send `.done` when done.'}",
                parse_mode="Markdown",
                reply_markup=kb_cancel_only()
            )
            return

        # ── Dot commands ──
        if text == ".cmd":
            await msg.reply_text(CMD_MENU, parse_mode="Markdown")
            return

        if text == ".stats":
            total = await users_col.count_documents({})
            await msg.reply_text(f"📊 *Stats*\n\n👥 Total Users: *{total}*", parse_mode="Markdown")
            return

        if text == ".users":
            lines = []
            async for doc in users_col.find({}):
                lines.append(f"• {doc.get('name','Unknown')} — `{doc['_id']}`")
            if not lines:
                await msg.reply_text("No users yet.")
                return
            # send in chunks of 30
            for chunk in [lines[i:i+30] for i in range(0, len(lines), 30)]:
                await msg.reply_text(
                    f"👥 *Users ({len(lines)} total):*\n\n" + "\n".join(chunk),
                    parse_mode="Markdown"
                )
            return

        if text == ".broadcast":
            bc_reset(context)
            bc(context)["step"] = BC_WAIT
            await msg.reply_text(
                "📢 *Broadcast Setup*\n\n"
                "Send the message you want to broadcast to all users.\n\n"
                "_Supports: text, photo, video, voice, sticker, document, forwarded messages_",
                parse_mode="Markdown",
                reply_markup=kb_cancel_only()
            )
            return

        # ── Reply to user ──
        if msg.reply_to_message:
            orig_id = msg.reply_to_message.message_id
            if orig_id in user_map:
                try:
                    await context.bot.copy_message(
                        chat_id=user_map[orig_id],
                        from_chat_id=msg.chat_id,
                        message_id=msg.message_id
                    )
                except Exception as e:
                    await msg.reply_text(f"❌ Failed to send: {e}")
            else:
                await msg.reply_text(
                    "⚠️ User not found in map.\n"
                    "They need to send a new message first."
                )
        return

    # ══════════════ USER ════════════════
    user = msg.from_user
    await save_user(user.id, user.first_name)

    try:
        uname = f" (@{user.username})" if user.username else ""
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"👤 *{user.first_name}*{uname}\n`ID: {user.id}`",
            parse_mode="Markdown"
        )
        fwd = await msg.forward(chat_id=ADMIN_ID)
        user_map[fwd.message_id] = user.id
        status = await msg.reply_text("✅ Message sent!")
    except Exception:
        status = await msg.reply_text("❌ Failed to send. Try again!")

    await asyncio.sleep(3)
    try:
        await status.delete()
    except Exception:
        pass


# ─── Callback handler ─────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data  = q.data
    state = bc(context)

    if data == "bc_noop":
        return

    if data == "bc_cancel":
        bc_reset(context)
        await q.edit_message_text("❌ Broadcast cancelled.")
        return

    if data == "bc_sendnow":
        total = await users_col.count_documents({})
        await q.edit_message_text(f"📤 Sending to *{total} users*...", parse_mode="Markdown")
        await _do_broadcast(context, q.message.chat_id)
        bc_reset(context)
        return

    if data == "bc_addbtns":
        state["step"] = BC_BUTTONS
        await q.edit_message_text(
            "✏️ *Add URL Buttons*\n\n"
            "Send each button on a new message:\n"
            "`Button Name - https://example.com`\n\n"
            "Max *10 buttons*.\n"
            "Send `.done` when finished.",
            parse_mode="Markdown",
            reply_markup=kb_cancel_only()
        )
        return

    if data == "bc_confirm":
        total = await users_col.count_documents({})
        await q.edit_message_text(f"📤 Broadcasting to *{total} users*...", parse_mode="Markdown")
        await _do_broadcast(context, q.message.chat_id)
        bc_reset(context)
        return


async def _show_confirm(msg, context):
    state = bc(context)
    buttons = state.get("buttons", [])
    keyboard = [[InlineKeyboardButton(b["name"], url=b["url"])] for b in buttons]
    total = await users_col.count_documents({})

    await msg.reply_text("👁 *Final Preview:*", parse_mode="Markdown")
    await context.bot.copy_message(
        chat_id=ADMIN_ID,
        from_chat_id=state["chat_id"],
        message_id=state["msg_id"],
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
    )

    btn_list = "\n".join(f"  {i+1}. {b['name']} → {b['url']}" for i, b in enumerate(buttons)) or "  None"
    state["step"] = BC_CONFIRM
    await msg.reply_text(
        f"📊 *Broadcast Summary*\n\n"
        f"👥 Recipients: *{total} users*\n"
        f"🔘 Buttons ({len(buttons)}):\n{btn_list}",
        parse_mode="Markdown",
        reply_markup=kb_confirm(len(buttons))
    )


async def _do_broadcast(context, admin_chat_id):
    state   = bc(context)
    buttons = state.get("buttons", [])
    keyboard = [[InlineKeyboardButton(b["name"], url=b["url"])] for b in buttons]
    rm = InlineKeyboardMarkup(keyboard) if keyboard else None

    uids = await all_user_ids()
    ok = fail = 0

    for uid in uids:
        try:
            await context.bot.copy_message(
                chat_id=uid,
                from_chat_id=state["chat_id"],
                message_id=state["msg_id"],
                reply_markup=rm
            )
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)

    await context.bot.send_message(
        chat_id=admin_chat_id,
        text=f"✅ *Broadcast Complete!*\n\n📤 Sent: *{ok}*\n❌ Failed: *{fail}*\n👥 Total: *{ok+fail}*",
        parse_mode="Markdown"
    )


# ─── Run ──────────────────────────────────────────────────────────
threading.Thread(target=_web, daemon=True).start()
print(f"✅ Keep-alive on port {PORT}")

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CallbackQueryHandler(on_callback))
app.add_handler(MessageHandler(filters.ALL, on_message))

print("🤖 Bot running...")
app.run_polling()