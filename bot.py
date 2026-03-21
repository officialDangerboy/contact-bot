import asyncio
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ConversationHandler, CallbackQueryHandler, filters, ContextTypes
)
from motor.motor_asyncio import AsyncIOMotorClient

# ─── Config ───────────────────────────────────────────────────────
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
PORT = int(os.environ.get("PORT", 8080))

# ─── MongoDB ──────────────────────────────────────────────────────
mongo_client = AsyncIOMotorClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
db = mongo_client["telebot"]
users_col = db["users"]

user_map = {}  # forwarded_msg_id -> user_id
WAIT_MSG, WAIT_CHOICE, WAIT_BUTTON_INPUT, WAIT_CONFIRM = range(4)


# ─── Keep-Alive Web Server ────────────────────────────────────────
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, format, *args):
        pass

def run_web_server():
    server = HTTPServer(("0.0.0.0", PORT), KeepAliveHandler)
    server.serve_forever()


# ─── DB Helpers ───────────────────────────────────────────────────
async def save_user(user_id: int, first_name: str):
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"first_name": first_name}},
        upsert=True
    )

async def get_all_users():
    return [doc["_id"] async for doc in users_col.find({})]


# ─── DOT COMMAND HELPER ───────────────────────────────────────────
def is_dot_cmd(text: str, cmd: str) -> bool:
    """Check if message is a dot command like .broadcast"""
    if not text:
        return False
    return text.strip().lower() == f".{cmd}"


# ─── Main Message Handler ─────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.message.from_user.id
    text = update.message.text or ""

    # ── ADMIN actions ──
    if user_id == ADMIN_ID:

        # Dot commands for admin
        if is_dot_cmd(text, "help"):
            await update.message.reply_text(
                "🤖 *Admin Commands*\n\n"
                "`.help` — Show this menu\n"
                "`.broadcast` — Send message to all users\n"
                "`.users` — List all users\n"
                "`.stats` — Show total user count\n\n"
                "💬 *To reply to a user:*\n"
                "Just reply to their forwarded message",
                parse_mode="Markdown"
            )
            return

        if is_dot_cmd(text, "stats"):
            total = await users_col.count_documents({})
            await update.message.reply_text(
                f"📊 *Bot Stats*\n\n👥 Total Users: *{total}*",
                parse_mode="Markdown"
            )
            return

        if is_dot_cmd(text, "users"):
            cursor = users_col.find({})
            lines = []
            async for doc in cursor:
                lines.append(f"• {doc.get('first_name', 'Unknown')} — `{doc['_id']}`")
            if not lines:
                await update.message.reply_text("No users yet.")
            else:
                chunks = [lines[i:i+30] for i in range(0, len(lines), 30)]
                for chunk in chunks:
                    await update.message.reply_text(
                        f"👥 *Users:*\n\n" + "\n".join(chunk),
                        parse_mode="Markdown"
                    )
            return

        if is_dot_cmd(text, "broadcast"):
            context.user_data.clear()
            await update.message.reply_text(
                "📢 *Broadcast Setup*\n\nSend the message you want to broadcast.\n"
                "_Supports: text, photo, video, voice, sticker, document_",
                parse_mode="Markdown"
            )
            context.user_data["in_broadcast"] = True
            context.user_data["bc_step"] = WAIT_MSG
            return

        # Handle broadcast flow via dot command
        if context.user_data.get("in_broadcast"):
            await handle_broadcast_flow(update, context)
            return

        # Admin replying to forwarded user message
        if update.message.reply_to_message:
            original_id = update.message.reply_to_message.message_id
            if original_id in user_map:
                target_user = user_map[original_id]
                try:
                    await context.bot.copy_message(
                        chat_id=target_user,
                        from_chat_id=update.message.chat_id,
                        message_id=update.message.message_id
                    )
                except Exception as e:
                    await update.message.reply_text(f"❌ Failed: {e}")
            else:
                await update.message.reply_text(
                    "⚠️ User not found in map.\n"
                    "Ask them to send a new message first."
                )
        return  # Never forward admin messages

    # ── USER sending a message ──
    user = update.message.from_user
    await save_user(user.id, user.first_name)

    try:
        username = f" (@{user.username})" if user.username else ""
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"👤 *{user.first_name}*{username}\n`ID: {user.id}`",
            parse_mode="Markdown"
        )
        forwarded = await update.message.forward(chat_id=ADMIN_ID)
        user_map[forwarded.message_id] = user_id
        status = await update.message.reply_text("✅ Message sent!")
    except Exception:
        status = await update.message.reply_text("❌ Failed to send. Try again!")

    await asyncio.sleep(3)
    try:
        await status.delete()
    except Exception:
        pass


# ─── BROADCAST FLOW ───────────────────────────────────────────────
async def handle_broadcast_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("bc_step")
    text = update.message.text or ""

    # Step 1: Got the broadcast message
    if step == WAIT_MSG:
        context.user_data["bc_chat_id"] = update.message.chat_id
        context.user_data["bc_msg_id"] = update.message.message_id
        context.user_data["buttons"] = []

        await update.message.reply_text("👁 *Preview:*", parse_mode="Markdown")
        await context.bot.copy_message(
            chat_id=ADMIN_ID,
            from_chat_id=update.message.chat_id,
            message_id=update.message.message_id
        )
        kb = [[
            InlineKeyboardButton("➕ Add URL Buttons", callback_data="add_btns"),
            InlineKeyboardButton("📤 Send Now", callback_data="send_now")
        ]]
        await update.message.reply_text(
            "Want to add inline URL buttons?",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        context.user_data["bc_step"] = WAIT_CHOICE
        return

    # Step 2: Collecting buttons
    if step == WAIT_BUTTON_INPUT:
        buttons = context.user_data.get("buttons", [])

        if is_dot_cmd(text, "done"):
            await show_final_preview(update, context)
            return

        if len(buttons) >= 10:
            await update.message.reply_text("⚠️ Max 10 buttons! Send `.done` to finish.")
            return

        if " - " not in text:
            await update.message.reply_text(
                "❌ Wrong format!\n`Button Name - https://example.com`",
                parse_mode="Markdown"
            )
            return

        name, url = text.split(" - ", 1)
        name, url = name.strip(), url.strip()

        if not (url.startswith("http://") or url.startswith("https://")):
            await update.message.reply_text("❌ URL must start with `http://` or `https://`", parse_mode="Markdown")
            return

        buttons.append({"name": name, "url": url})
        context.user_data["buttons"] = buttons
        remaining = 10 - len(buttons)
        await update.message.reply_text(
            f"✅ *Button {len(buttons)}* added!\n"
            f"{'Send `.done` to finish.' if remaining == 0 else f'{remaining} slots left. Send `.done` when done.'}",
            parse_mode="Markdown"
        )


async def show_final_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = context.user_data.get("buttons", [])
    keyboard = [[InlineKeyboardButton(b["name"], url=b["url"])] for b in buttons]
    total = await users_col.count_documents({})

    await update.message.reply_text("👁 *Final Preview:*", parse_mode="Markdown")
    await context.bot.copy_message(
        chat_id=ADMIN_ID,
        from_chat_id=context.user_data["bc_chat_id"],
        message_id=context.user_data["bc_msg_id"],
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
    )

    btn_list = "\n".join([f"  • {b['name']}" for b in buttons]) if buttons else "  None"
    kb = [[
        InlineKeyboardButton("✅ Confirm & Send", callback_data="confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel")
    ]]
    await update.message.reply_text(
        f"📊 *Broadcast Summary*\n\n"
        f"👥 Recipients: *{total} users*\n"
        f"🔘 Buttons ({len(buttons)}):\n{btn_list}",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    context.user_data["bc_step"] = WAIT_CONFIRM


# ─── CALLBACK HANDLER ─────────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "send_now":
        total = await users_col.count_documents({})
        await query.edit_message_text(f"📤 Sending to {total} users...")
        await _do_broadcast(context, query.message.chat_id)
        context.user_data.clear()

    elif query.data == "add_btns":
        await query.edit_message_text(
            "✏️ *Add Inline Buttons*\n\n"
            "Send each button in this format:\n"
            "`Button Name - https://example.com`\n\n"
            "Max *10 buttons*. Send `.done` when finished.",
            parse_mode="Markdown"
        )
        context.user_data["bc_step"] = WAIT_BUTTON_INPUT

    elif query.data == "confirm":
        total = await users_col.count_documents({})
        await query.edit_message_text(f"📤 Broadcasting to {total} users...")
        await _do_broadcast(context, query.message.chat_id)
        context.user_data.clear()

    elif query.data == "cancel":
        await query.edit_message_text("❌ Broadcast cancelled.")
        context.user_data.clear()


async def _do_broadcast(context: ContextTypes.DEFAULT_TYPE, admin_chat_id: int):
    buttons = context.user_data.get("buttons", [])
    keyboard = [[InlineKeyboardButton(b["name"], url=b["url"])] for b in buttons]
    rm = InlineKeyboardMarkup(keyboard) if keyboard else None

    users = await get_all_users()
    success, failed = 0, 0

    for uid in users:
        try:
            await context.bot.copy_message(
                chat_id=uid,
                from_chat_id=context.user_data["bc_chat_id"],
                message_id=context.user_data["bc_msg_id"],
                reply_markup=rm
            )
            success += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await context.bot.send_message(
        chat_id=admin_chat_id,
        text=f"✅ *Broadcast Complete!*\n\n"
             f"📤 Sent: *{success}*\n"
             f"❌ Failed: *{failed}*\n"
             f"👥 Total: *{success + failed}*",
        parse_mode="Markdown"
    )


# ─── Run ──────────────────────────────────────────────────────────
threading.Thread(target=run_web_server, daemon=True).start()
print(f"✅ Keep-alive server on port {PORT}")

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CallbackQueryHandler(callback_handler))
app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

print("🤖 Bot is running...")
app.run_polling()