import asyncio
import os
import threading
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument,
    InputMediaAudio, InputMediaAnimation
)
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
user_map            = {}   # forwarded_msg_id -> user_id
media_group_buffer  = defaultdict(lambda: {"msgs": [], "user_id": None, "task": None})
bc_group_buffer     = defaultdict(lambda: {"msgs": [], "task": None})

BC_IDLE    = 0
BC_WAIT    = 1
BC_BUTTONS = 2
BC_CONFIRM = 3


# ─── Keep-Alive ───────────────────────────────────────────────────
class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *a): pass

def _web():
    HTTPServer(("0.0.0.0", PORT), _H).serve_forever()


# ─── DB Helpers ───────────────────────────────────────────────────
async def save_user(uid: int, name: str):
    await users_col.update_one({"_id": uid}, {"$set": {"name": name}}, upsert=True)

async def all_user_ids():
    return [d["_id"] async for d in users_col.find({})]


# ─── Build InputMedia from message ───────────────────────────────
def msg_to_input_media(msg, caption=None):
    """Convert a telegram message to InputMedia object for send_media_group."""
    cap = caption or msg.caption or None
    if msg.photo:
        return InputMediaPhoto(media=msg.photo[-1].file_id, caption=cap, parse_mode="HTML")
    if msg.video:
        return InputMediaVideo(media=msg.video.file_id, caption=cap, parse_mode="HTML")
    if msg.document:
        return InputMediaDocument(media=msg.document.file_id, caption=cap, parse_mode="HTML")
    if msg.audio:
        return InputMediaAudio(media=msg.audio.file_id, caption=cap, parse_mode="HTML")
    if msg.animation:
        return InputMediaAnimation(media=msg.animation.file_id, caption=cap, parse_mode="HTML")
    return None


# ─── Keyboards ────────────────────────────────────────────────────
def kb_after_preview():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Add URL Buttons", callback_data="bc_addbtns"),
        InlineKeyboardButton("📤 Send Now",        callback_data="bc_sendnow"),
    ],[
        InlineKeyboardButton("❌ Cancel Broadcast", callback_data="bc_cancel"),
    ]])

def kb_confirm(btn_count):
    rows = [[
        InlineKeyboardButton("✅ Confirm & Send", callback_data="bc_confirm"),
        InlineKeyboardButton("❌ Cancel",         callback_data="bc_cancel"),
    ]]
    if btn_count < 10:
        rows.append([InlineKeyboardButton("➕ Add More Buttons", callback_data="bc_addbtns")])
    return InlineKeyboardMarkup(rows)

def kb_cancel_only():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel Broadcast", callback_data="bc_cancel"),
    ]])


# ─── .cmd menu ────────────────────────────────────────────────────
CMD_MENU = (
    "📋 *Admin Command Menu*\n\n"
    "`.cmd` — Show this menu\n"
    "`.stats` — Total users count\n"
    "`.users` — List all users with IDs\n"
    "`.broadcast` — Broadcast to all users\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "💬 *Reply to user:*\n"
    "Reply to any forwarded message\n\n"
    "📢 *Broadcast guide:*\n"
    "1⃣ Send `.broadcast`\n"
    "2⃣ Forward/send your message (any type, albums too)\n"
    "3⃣ See preview → pick option\n"
    "4⃣ To add buttons send all at once:\n"
    "`Btn Name - https://link.com`\n"
    "`Btn 2 - https://link2.com`\n"
    "5⃣ Preview with buttons → Confirm ✅"
)


# ─── Broadcast state ──────────────────────────────────────────────
def bc(ctx):
    return ctx.user_data.setdefault("bc", {
        "step": BC_IDLE, "buttons": [],
        "msgs": [],      # list of message objects (for album)
        "chat_id": None, "msg_id": None,  # for single msg
        "is_album": False
    })

def bc_reset(ctx):
    ctx.user_data["bc"] = {
        "step": BC_IDLE, "buttons": [],
        "msgs": [], "chat_id": None, "msg_id": None, "is_album": False
    }


# ─── Send album to a user ─────────────────────────────────────────
async def send_album_to(bot, chat_id: int, msgs: list, extra_buttons=None):
    """Send a media group preserving captions. Buttons go on last item."""
    sorted_msgs = sorted(msgs, key=lambda m: m.message_id)
    media = []
    for i, m in enumerate(sorted_msgs):
        # Only first message keeps caption (Telegram album behaviour)
        cap = m.caption if i == 0 else None
        obj = msg_to_input_media(m, cap)
        if obj:
            media.append(obj)

    if not media:
        return

    if len(media) == 1:
        # Single media — can attach inline keyboard
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=sorted_msgs[0].chat_id,
            message_id=sorted_msgs[0].message_id,
            reply_markup=extra_buttons
        )
    else:
        # send_media_group doesn't support inline keyboards on individual items
        # so send group first, then send buttons as a separate message if needed
        await bot.send_media_group(chat_id=chat_id, media=media)
        if extra_buttons:
            await bot.send_message(
                chat_id=chat_id,
                text="🔗 Links:",
                reply_markup=extra_buttons
            )


# ─── Show confirm preview ─────────────────────────────────────────
async def show_confirm_preview(chat_id, context, state):
    buttons  = state.get("buttons", [])
    keyboard = [[InlineKeyboardButton(b["name"], url=b["url"])] for b in buttons]
    rm       = InlineKeyboardMarkup(keyboard) if keyboard else None
    total    = await users_col.count_documents({})

    await context.bot.send_message(
        chat_id=chat_id,
        text="👁 *Final Preview:*",
        parse_mode="Markdown"
    )

    if state["is_album"]:
        await send_album_to(context.bot, chat_id, state["msgs"], rm)
    else:
        await context.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=state["chat_id"],
            message_id=state["msg_id"],
            reply_markup=rm
        )

    btn_list = "\n".join(
        f"  {i+1}. {b['name']} → {b['url']}" for i, b in enumerate(buttons)
    ) or "  None"

    state["step"] = BC_CONFIRM
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📊 *Broadcast Summary*\n\n"
            f"👥 Will send to *{total} users*\n"
            f"📎 {'Album: ' + str(len(state['msgs'])) + ' items' if state['is_album'] else 'Single message'}\n"
            f"🔘 Buttons ({len(buttons)}):\n{btn_list}\n\n"
            f"Confirm to send?"
        ),
        parse_mode="Markdown",
        reply_markup=kb_confirm(len(buttons))
    )


# ─── Do broadcast ─────────────────────────────────────────────────
async def _do_broadcast(context, admin_chat_id):
    state    = bc(context)
    buttons  = state.get("buttons", [])
    keyboard = [[InlineKeyboardButton(b["name"], url=b["url"])] for b in buttons]
    rm       = InlineKeyboardMarkup(keyboard) if keyboard else None

    uids = await all_user_ids()
    ok = fail = 0

    for uid in uids:
        try:
            if state["is_album"]:
                await send_album_to(context.bot, uid, state["msgs"], rm)
            else:
                await context.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=state["chat_id"],
                    message_id=state["msg_id"],
                    reply_markup=rm
                )
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.1)

    await context.bot.send_message(
        chat_id=admin_chat_id,
        text=(
            f"✅ *Broadcast Complete!*\n\n"
            f"📤 Sent: *{ok}*\n"
            f"❌ Failed: *{fail}*\n"
            f"👥 Total: *{ok + fail}*"
        ),
        parse_mode="Markdown"
    )


# ─── Flush user media group → forward to admin ────────────────────
async def flush_user_media_group(group_id: str, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(1.0)
    buf = media_group_buffer.get(group_id)
    if not buf or not buf["msgs"]:
        return

    sorted_msgs = sorted(buf["msgs"], key=lambda m: m.message_id)
    user_id     = buf["user_id"]
    user        = sorted_msgs[0].from_user
    first_msg   = sorted_msgs[0]

    try:
        uname = f" (@{user.username})" if user.username else ""
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"👤 *{user.first_name}*{uname}\n`ID: {user.id}`\n📎 _{len(sorted_msgs)} media items_",
            parse_mode="Markdown"
        )

        # forward_messages preserves album grouping AND captions perfectly
        fwd_list = await context.bot.forward_messages(
            chat_id=ADMIN_ID,
            from_chat_id=first_msg.chat_id,
            message_ids=[m.message_id for m in sorted_msgs]
        )

        # Map ALL forwarded IDs → user so admin can reply to any of them
        for fwd in fwd_list:
            user_map[fwd.message_id] = user_id

        status = await first_msg.reply_text("✅ Message sent!")
        await asyncio.sleep(3)
        try:
            await status.delete()
        except Exception:
            pass

    except Exception:
        try:
            await first_msg.reply_text("❌ Failed to send. Try again!")
        except Exception:
            pass

    del media_group_buffer[group_id]


# ─── Flush broadcast media group ──────────────────────────────────
async def flush_bc_media_group(group_id: str, context: ContextTypes.DEFAULT_TYPE, state: dict):
    await asyncio.sleep(1.0)
    buf = bc_group_buffer.get(group_id)
    if not buf or not buf["msgs"]:
        return

    sorted_msgs      = sorted(buf["msgs"], key=lambda m: m.message_id)
    state["is_album"] = True
    state["msgs"]     = sorted_msgs
    state["chat_id"]  = sorted_msgs[0].chat_id
    state["step"]     = BC_CONFIRM
    total = await users_col.count_documents({})

    await context.bot.send_message(chat_id=ADMIN_ID, text="👁 *Preview:*", parse_mode="Markdown")

    # Show album preview as-is
    await context.bot.forward_messages(
        chat_id=ADMIN_ID,
        from_chat_id=sorted_msgs[0].chat_id,
        message_ids=[m.message_id for m in sorted_msgs]
    )

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"📢 *Broadcast Preview*\n\n"
            f"📎 Album: *{len(sorted_msgs)} items* (with captions)\n"
            f"👥 Will send to *{total} users*\n\n"
            f"Choose an option:"
        ),
        parse_mode="Markdown",
        reply_markup=kb_after_preview()
    )

    del bc_group_buffer[group_id]


# ─── MAIN HANDLER ─────────────────────────────────────────────────
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    uid      = msg.from_user.id
    text     = (msg.text or "").strip()
    group_id = msg.media_group_id

    # ══════════════ ADMIN ═════════════════════════════════════════
    if uid == ADMIN_ID:
        state = bc(context)

        # ── Waiting for broadcast message ──
        if state["step"] == BC_WAIT:
            if group_id:
                buf = bc_group_buffer[group_id]
                buf["msgs"].append(msg)
                if buf.get("task") is None or buf["task"].done():
                    buf["task"] = asyncio.create_task(
                        flush_bc_media_group(group_id, context, state)
                    )
                return
            else:
                state["is_album"] = False
                state["chat_id"]  = msg.chat_id
                state["msg_id"]   = msg.message_id
                state["step"]     = BC_CONFIRM
                total = await users_col.count_documents({})

                await msg.reply_text("👁 *Preview:*", parse_mode="Markdown")
                await context.bot.copy_message(
                    chat_id=ADMIN_ID,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id
                )
                await msg.reply_text(
                    f"📢 *Broadcast Preview*\n\n"
                    f"👥 Will send to *{total} users*\n\n"
                    f"Choose an option:",
                    parse_mode="Markdown",
                    reply_markup=kb_after_preview()
                )
                return

        # ── Collecting buttons ──
        if state["step"] == BC_BUTTONS:
            lines  = [l.strip() for l in text.splitlines() if l.strip()]
            errors = []
            added  = []

            for line in lines:
                if len(state["buttons"]) >= 10:
                    errors.append("⚠️ Max 10 buttons reached.")
                    break
                if " - " not in line:
                    errors.append(f"❌ Bad format: `{line}`")
                    continue
                name, url = line.split(" - ", 1)
                name, url = name.strip(), url.strip()
                if not url.startswith(("http://", "https://")):
                    errors.append(f"❌ Invalid URL: `{url}`")
                    continue
                state["buttons"].append({"name": name, "url": url})
                added.append(name)

            if errors and not added:
                await msg.reply_text(
                    "\n".join(errors) + "\n\nFormat:\n`Button Name - https://link.com`",
                    parse_mode="Markdown", reply_markup=kb_cancel_only()
                )
                return

            await show_confirm_preview(msg.chat_id, context, state)
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
                "Forward or send the message you want to broadcast.\n\n"
                "_Supports: text, photo, video, voice, sticker, document, albums_",
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
                    await msg.reply_text(f"❌ Failed: {e}")
            else:
                await msg.reply_text("⚠️ User not found.\nAsk them to send a new message first.")
        return

    # ══════════════ USER ══════════════════════════════════════════
    user = msg.from_user
    await save_user(user.id, user.first_name)

    if group_id:
        # Album — buffer and flush after 1s
        buf = media_group_buffer[group_id]
        buf["msgs"].append(msg)
        buf["user_id"] = user.id
        if buf.get("task") is None or buf["task"].done():
            buf["task"] = asyncio.create_task(
                flush_user_media_group(group_id, context)
            )
        return

    # Single message
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

    await asyncio.sleep(1)
    try:
        await status.delete()
    except Exception:
        pass


# ─── CALLBACK HANDLER ─────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q     = update.callback_query
    await q.answer()
    data  = q.data
    state = bc(context)

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
            "Send all buttons in *one message*, one per line:\n\n"
            "`Button Name - https://example.com`\n"
            "`Button 2 - https://example2.com`\n\n"
            "_Up to 10 buttons at once._",
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


# ─── Run ──────────────────────────────────────────────────────────
threading.Thread(target=_web, daemon=True).start()
print(f"✅ Keep-alive on port {PORT}")

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CallbackQueryHandler(on_callback))
app.add_handler(MessageHandler(filters.ALL, on_message))

print("🤖 Bot running...")
app.run_polling()