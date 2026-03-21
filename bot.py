import asyncio
import os
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ConversationHandler, CallbackQueryHandler, filters, ContextTypes
)
from motor.motor_asyncio import AsyncIOMotorClient

# ─── Config ───────────────────────────────────────────────────────
ADMIN_ID = os.environ("ADMIN_ID")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

# ─── MongoDB ──────────────────────────────────────────────────────
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["telebot"]
users_col = db["users"]

# In-memory map: forwarded_msg_id -> user_id (resets on restart)
user_map = {}

# ─── Conversation States ──────────────────────────────────────────
WAIT_MSG, WAIT_CHOICE, WAIT_BUTTON_INPUT, WAIT_CONFIRM = range(4)


# ─── DB Helpers ───────────────────────────────────────────────────
async def save_user(user_id: int, first_name: str):
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"first_name": first_name}},
        upsert=True
    )

async def get_all_users():
    return [doc["_id"] async for doc in users_col.find({})]


# ─── Main Message Handler ─────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.message.from_user.id

    # ── ADMIN replying to forwarded message ──
    if user_id == ADMIN_ID:
        if update.message.reply_to_message:
            original_id = update.message.reply_to_message.message_id

            if original_id in user_map:
                target_user = user_map[original_id]
                try:
                    # copy_to supports ALL message types (text, photo, video, sticker, voice...)
                    await update.message.copy_to(chat_id=target_user)
                except Exception as e:
                    await update.message.reply_text(f"❌ Failed to send: {e}")
            else:
                await update.message.reply_text(
                    "⚠️ User not found in map.\n"
                    "This happens when bot restarts. Ask user to send a new message."
                )
        return  # Never process admin messages further

    # ── USER sending a message ──
    user = update.message.from_user
    await save_user(user.id, user.first_name)  # Save to MongoDB

    try:
        # Show user info label to admin
        name = user.first_name
        username = f" (@{user.username})" if user.username else ""
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"👤 *{name}*{username}\n`ID: {user.id}`",
            parse_mode="Markdown"
        )

        forwarded = await update.message.forward(chat_id=ADMIN_ID)
        user_map[forwarded.message_id] = user_id  # Save mapping

        status = await update.message.reply_text("✅ Message sent!")
    except Exception:
        status = await update.message.reply_text("❌ Failed to send. Try again!")

    await asyncio.sleep(3)
    try:
        await status.delete()
    except Exception:
        pass


# ─── BROADCAST SYSTEM ─────────────────────────────────────────────

async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        "📢 *Broadcast Setup*\n\n"
        "Send the message you want to broadcast to all users.\n\n"
        "_Supports: text, photo, video, voice, sticker, document, forwarded messages_",
        parse_mode="Markdown"
    )
    return WAIT_MSG


async def broadcast_got_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Save the message reference
    context.user_data["bc_chat_id"] = update.message.chat_id
    context.user_data["bc_msg_id"] = update.message.message_id
    context.user_data["buttons"] = []

    # Show preview
    await update.message.reply_text("👁 *Preview:*", parse_mode="Markdown")
    await update.message.copy_to(chat_id=ADMIN_ID)

    kb = [[
        InlineKeyboardButton("➕ Add URL Buttons", callback_data="add_btns"),
        InlineKeyboardButton("📤 Send Now", callback_data="send_now")
    ]]
    await update.message.reply_text(
        "Want to add inline URL buttons to this broadcast?",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return WAIT_CHOICE


async def choice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "send_now":
        total = await users_col.count_documents({})
        await query.edit_message_text(f"📤 Sending to {total} users...")
        await _do_broadcast(context, query.message.chat_id)
        return ConversationHandler.END

    # Add buttons mode
    await query.edit_message_text(
        "✏️ *Add Inline Buttons*\n\n"
        "Send each button on a new message in this format:\n"
        "`Button Name - https://example.com`\n\n"
        "━━━━━━━━━━━━━━\n"
        "Example:\n"
        "`Join Channel - https://t.me/mychannel`\n"
        "`Visit Website - https://mysite.com`\n"
        "━━━━━━━━━━━━━━\n"
        "Max *10 buttons*. Send /done when finished.",
        parse_mode="Markdown"
    )
    return WAIT_BUTTON_INPUT


async def collect_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    buttons = context.user_data.get("buttons", [])

    if len(buttons) >= 10:
        await update.message.reply_text("⚠️ Max 10 buttons reached! Send /done to finish.")
        return WAIT_BUTTON_INPUT

    if " - " not in text:
        await update.message.reply_text(
            "❌ Wrong format! Use:\n`Button Name - https://example.com`",
            parse_mode="Markdown"
        )
        return WAIT_BUTTON_INPUT

    name, url = text.split(" - ", 1)
    name, url = name.strip(), url.strip()

    if not (url.startswith("http://") or url.startswith("https://")):
        await update.message.reply_text("❌ URL must start with `http://` or `https://`", parse_mode="Markdown")
        return WAIT_BUTTON_INPUT

    buttons.append({"name": name, "url": url})
    context.user_data["buttons"] = buttons

    remaining = 10 - len(buttons)
    await update.message.reply_text(
        f"✅ *Button {len(buttons)}* added!\n"
        f"{'Send /done to finish.' if remaining == 0 else f'Send more or /done to finish. ({remaining} slots left)'}",
        parse_mode="Markdown"
    )
    return WAIT_BUTTON_INPUT


async def buttons_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = context.user_data.get("buttons", [])
    keyboard = [[InlineKeyboardButton(b["name"], url=b["url"])] for b in buttons]
    total = await users_col.count_documents({})

    # Show final preview with buttons
    await update.message.reply_text("👁 *Final Preview:*", parse_mode="Markdown")
    await context.bot.copy_message(
        chat_id=ADMIN_ID,
        from_chat_id=context.user_data["bc_chat_id"],
        message_id=context.user_data["bc_msg_id"],
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
    )

    # Confirm buttons
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
    return WAIT_CONFIRM


async def confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("❌ Broadcast cancelled.")
        return ConversationHandler.END

    total = await users_col.count_documents({})
    await query.edit_message_text(f"📤 Broadcasting to {total} users...")
    await _do_broadcast(context, query.message.chat_id)
    return ConversationHandler.END


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
        await asyncio.sleep(0.05)  # Avoid Telegram flood limit

    await context.bot.send_message(
        chat_id=admin_chat_id,
        text=f"✅ *Broadcast Complete!*\n\n"
             f"📤 Sent: *{success}*\n"
             f"❌ Failed: *{failed}*\n"
             f"👥 Total: *{success + failed}*",
        parse_mode="Markdown"
    )


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Broadcast cancelled.")
    return ConversationHandler.END


# ─── Stats Command ────────────────────────────────────────────────
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    total = await users_col.count_documents({})
    await update.message.reply_text(
        f"📊 *Bot Stats*\n\n👥 Total Users: *{total}*",
        parse_mode="Markdown"
    )


# ─── App Setup ────────────────────────────────────────────────────
app = ApplicationBuilder().token(BOT_TOKEN).build()

broadcast_conv = ConversationHandler(
    entry_points=[CommandHandler("broadcast", broadcast_start)],
    states={
        WAIT_MSG: [
            MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_got_msg)
        ],
        WAIT_CHOICE: [
            CallbackQueryHandler(choice_handler, pattern="^(add_btns|send_now)$")
        ],
        WAIT_BUTTON_INPUT: [
            CommandHandler("done", buttons_done),
            MessageHandler(filters.TEXT & ~filters.COMMAND, collect_button),
        ],
        WAIT_CONFIRM: [
            CallbackQueryHandler(confirm_handler, pattern="^(confirm|cancel)$")
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel_conv)],
    per_message=False,
)

app.add_handler(broadcast_conv)
app.add_handler(CommandHandler("stats", stats))
app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

# ─── Web Server (Required for Render Free Web Service) ────────────
async def health(request):
    return web.Response(text="✅ Bot is running!")

async def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = web.Application()
    server.router.add_get("/", health)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Web server running on port {port}")

# ─── Main Runner ──────────────────────────────────────────────────
async def main():
    await run_web_server()
    print("🤖 Bot is running...")
    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        # Keep running forever
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())