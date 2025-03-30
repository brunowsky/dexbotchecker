import asyncio
import time
import aiohttp
import logging
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    PicklePersistence,
    CallbackQueryHandler,
    ChatMemberHandler
)
from tenacity import retry, stop_after_attempt, wait_fixed
import telegram.error

# Configure logging to display in the terminal
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Constants
MAX_TRACKING_SLOTS = 2
SOLANA_ADDRESS_PATTERN = r'^[1-9A-HJ-NP-Za-km-z]{32,44}$'

# Error handler for unexpected exceptions
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error("Exception while handling update:", exc_info=context.error)

# Helper to get storage based on chat type (group or user)
def get_storage(context: ContextTypes.DEFAULT_TYPE) -> dict:
    if context._chat_id and context._chat_id < 0:  # Group chat
        return context.chat_data
    return context.user_data

# Fetch token info with retry logic
@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
async def fetch_token_info(token_address: str) -> tuple[str | None, int | None, str | None]:
    """Fetch token status, payment timestamp, and symbol with retry logic."""
    status_url = f"https://api.dexscreener.com/orders/v1/solana/{token_address}"
    pairs_url = f"https://api.dexscreener.com/token-pairs/v1/solana/{token_address}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(status_url) as response:
                if response.status != 200:
                    return None, None, None
                status_data = await response.json()
                if isinstance(status_data, list) and len(status_data) > 0:
                    status = status_data[0]['status']
                    payment_ts = status_data[0].get('paymentTimestamp', 0)
                else:
                    status = 'not paid'
                    payment_ts = 0

            async with session.get(pairs_url) as response:
                pairs_data = await response.json() if response.status == 200 else {}
                symbol = 'Unknown'
                if isinstance(pairs_data, list) and len(pairs_data) > 0:
                    symbol = pairs_data[0].get('baseToken', {}).get('symbol', 'Unknown')
                
                return status, payment_ts, symbol
    except aiohttp.ClientError as e:
        logging.error(f"Error fetching token info: {e}")
        return None, None, None

# Fetch token header image URL
async def fetch_token_header(token_address: str) -> str | None:
    """Fetch token header image URL."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.dexscreener.com/token-pairs/v1/solana/{token_address}") as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, list):
                        return next((pair['info']['header'] for pair in data if pair.get('info', {}).get('header')), None)
    except aiohttp.ClientError as e:
        logging.error(f"Error fetching header: {e}")
    return None

# Convert timestamp to human-readable time difference
def time_since(timestamp: int) -> str:
    """Convert timestamp to relative time string."""
    if not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return "Unknown"
    diff = time.time() - (timestamp / 1000)
    if diff < 0:
        return "Not yet paid"
    intervals = (
        (diff // 86400, 'days'),
        (diff // 3600 % 24, 'hours'),
        (diff // 60 % 60, 'minutes'),
    )
    for count, unit in intervals:
        if count >= 1:
            return f"{int(count)} {unit}"
    return f"{int(diff)} seconds"

# Command to start tracking a token
async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage = get_storage(context)
    args = context.args
    
    if not args:
        await update.message.reply_text("Please provide a token address after /track")
        return

    token_address = args[0].strip()
    # Validate Solana address format
    if not re.match(SOLANA_ADDRESS_PATTERN, token_address):
        await update.message.reply_text("Invalid Solana address format")
        return

    status, payment_ts, symbol = await fetch_token_info(token_address)
    if not status:
        await update.message.reply_text("Failed to fetch token data. Check the address or try again later.")
        return

    symbol = symbol or 'Unknown'

    # Handle approved or updated tokens immediately
    if status.lower() in ['approved', 'updated']:
        time_ago = time_since(payment_ts)
        message = f"{symbol} ({token_address})\nStatus: {status}"
        if time_ago != "Unknown":
            message += f"\nPayment Time: {time_ago} ago"
        
        header = await fetch_token_header(token_address)
        if header and status.lower() == 'approved':
            try:
                await update.message.reply_photo(header, caption=message)
            except telegram.error.BadRequest as e:
                logging.warning(f"Failed to send photo: {e}")
                await update.message.reply_text(message)
        else:
            await update.message.reply_text(message)
        return

    tracked_tokens = storage.setdefault('tracked_tokens', {})
    if token_address in tracked_tokens:
        await update.message.reply_text(f"Already tracking {token_address}")
        return

    # Handle full tracking slots
    if len(tracked_tokens) >= MAX_TRACKING_SLOTS:
        storage['pending_token'] = token_address
        keyboard = [
            [InlineKeyboardButton(f"Replace Slot {i+1}: {info['symbol']}", 
             callback_data=f'replace_{i+1}')] 
            for i, (_, info) in enumerate(list(tracked_tokens.items())[:MAX_TRACKING_SLOTS])
        ]
        await update.message.reply_text(
            "Tracking slots full! Which one to replace?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # Start tracking new token
    tracked_tokens[token_address] = {
        'symbol': symbol,
        'last_status': status,
        'last_change': time.time()
    }

    message = f"Now tracking {symbol} ({token_address})\nInitial Status: {status}\nUpdates every 10 seconds."
    header = await fetch_token_header(token_address)
    if header and status.lower() == 'processing':
        try:
            await update.message.reply_photo(header, caption=message)
        except telegram.error.BadRequest as e:
            logging.warning(f"Failed to send photo: {e}")
            await update.message.reply_text(message)
    else:
        await update.message.reply_text(message)

    # Schedule periodic updates if not already running
    job_name = f"tracking_{context._chat_id}"
    jobs = context.job_queue.get_jobs_by_name(job_name)
    if not jobs:
        context.job_queue.run_repeating(
            check_for_updates,
            interval=10,
            first=10,
            chat_id=update.effective_chat.id,
            name=job_name,
            data={'is_group': context._chat_id < 0}
        )
        logging.info(f"Started tracking job {job_name} for chat {context._chat_id}")

# Handle slot replacement callback
async def handle_replace_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    storage = get_storage(context)
    tracked_tokens = storage.get('tracked_tokens', {})
    pending_token = storage.get('pending_token')
    
    if not pending_token:
        await query.edit_message_text("No pending token to track")
        return

    try:
        slot = int(query.data.split('_')[1]) - 1
        old_address = list(tracked_tokens.keys())[slot]
    except (IndexError, ValueError, KeyError):
        await query.edit_message_text("Invalid slot selection")
        return

    del tracked_tokens[old_address]
    status, payment_ts, symbol = await fetch_token_info(pending_token)
    if not status:
        await query.edit_message_text("Failed to fetch token data for replacement")
        return

    symbol = symbol or 'Unknown'
    time_ago = time_since(payment_ts)
    message = f"Replaced slot {slot+1} with {symbol} ({pending_token})\nStatus: {status}"
    if time_ago != "Unknown":
        message += f"\nPayment Time: {time_ago} ago"

    if status.lower() not in ['approved', 'updated']:
        tracked_tokens[pending_token] = {
            'symbol': symbol,
            'last_status': status,
            'last_change': time.time()
        }
        if status.lower() == 'processing':
            header = await fetch_token_header(pending_token)
            if header:
                try:
                    await context.bot.send_photo(query.message.chat_id, header, caption=message)
                except telegram.error.BadRequest as e:
                    logging.warning(f"Failed to send photo: {e}")
                    await context.bot.send_message(query.message.chat_id, message)
                await query.edit_message_text(f"Slot {slot+1} replaced successfully")
                return

    await query.edit_message_text(message)
    del storage['pending_token']

# Periodic update checker
async def check_for_updates(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    chat_id = job.chat_id
    storage = context.chat_data if job.data.get('is_group') else context.user_data
    tracked_tokens = storage.get('tracked_tokens', {})
    
    if not tracked_tokens:
        logging.info(f"No tokens to track in chat {chat_id}, removing job")
        job.schedule_removal()
        return
    
    for token_address, token_info in list(tracked_tokens.items()):
        current_status, payment_ts, symbol = await fetch_token_info(token_address)
        if not current_status:
            logging.warning(f"Failed to fetch info for {token_address} in chat {chat_id}")
            continue

        last_status = token_info['last_status']
        last_change = token_info['last_change']
        stored_symbol = token_info['symbol']
        symbol = stored_symbol if stored_symbol else symbol or 'Unknown'

        message = f"{symbol} ({token_address})\nStatus: {current_status}"
        if (ts := time_since(payment_ts)) != "Unknown":
            message += f"\nPayment Time: {ts} ago"

        if current_status != last_status:
            header = await fetch_token_header(token_address)
            if current_status.lower() in ['processing', 'approved']:
                if header:
                    try:
                        await context.bot.send_photo(job.chat_id, header, caption=message)
                    except telegram.error.BadRequest as e:
                        logging.warning(f"Failed to send photo: {e}")
                        await context.bot.send_message(job.chat_id, message)
                else:
                    await context.bot.send_message(job.chat_id, message)
            else:
                await context.bot.send_message(job.chat_id, message)

            tracked_tokens[token_address] = {
                'symbol': symbol,
                'last_status': current_status,
                'last_change': time.time()
            }

            if current_status.lower() in ['approved', 'updated']:
                del tracked_tokens[token_address]
                final_message = f"Stopped tracking {symbol} ({token_address})"
                if header and current_status.lower() == 'approved':
                    try:
                        await context.bot.send_photo(job.chat_id, header, caption=final_message)
                    except telegram.error.BadRequest as e:
                        logging.warning(f"Failed to send photo: {e}")
                        await context.bot.send_message(job.chat_id, final_message)
                else:
                    await context.bot.send_message(job.chat_id, final_message)

        elif time.time() - last_change > 1800:  # 30 minutes timeout
            del tracked_tokens[token_address]
            await context.bot.send_message(job.chat_id,
                f"Stopped tracking {symbol} ({token_address}) - no changes for 30 minutes")

    storage['tracked_tokens'] = dict(list(tracked_tokens.items())[:MAX_TRACKING_SLOTS])
    if not storage['tracked_tokens']:
        job.schedule_removal()

# Command to list currently tracked tokens
async def watching(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage = get_storage(context)
    tracked = storage.get('tracked_tokens', {})
    if not tracked:
        await update.message.reply_text("No active tracking")
        return
    
    msg = "Currently tracking:\n" + "\n".join(
        [f"{i+1}. {info['symbol']} ({addr})" 
         for i, (addr, info) in enumerate(tracked.items())]
    )
    await update.message.reply_text(msg)

# Command to stop all tracking
async def stop_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage = get_storage(context)
    if 'tracked_tokens' in storage:
        count = len(storage['tracked_tokens'])
        del storage['tracked_tokens']
        await update.message.reply_text(f"Stopped tracking {count} tokens")
    else:
        await update.message.reply_text("No active tracking")
    
    job_name = f"tracking_{context._chat_id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

# Track group membership
async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.my_chat_member.chat
    chat_id = chat.id
    new_status = update.my_chat_member.new_chat_member.status
    logging.info(f"MyChatMember update: chat_id={chat_id}, status={new_status}, title={chat.title}")
    if new_status in ['member', 'administrator']:
        context.bot_data.setdefault('group_chats', {})[chat_id] = chat.title
        logging.info(f"Added group: {chat.title} (ID: {chat_id})")
    elif new_status in ['left', 'kicked']:
        context.bot_data.get('group_chats', {}).pop(chat_id, None)
        logging.info(f"Removed group: {chat.title} (ID: {chat_id})")

# Log group info to terminal every 5 minutes
async def log_group_info(context: ContextTypes.DEFAULT_TYPE) -> None:
    group_chats = context.bot_data.get('group_chats', {})
    logging.info(f"Bot is in {len(group_chats)} group(s):")
    for chat_id, name in group_chats.items():
        logging.info(f"- {name} (ID: {chat_id})")

# Main function to run the bot
def main() -> None:
    persistence = PicklePersistence(filepath="bot_data.pickle")
    app = ApplicationBuilder() \
        .token('7839153642:AAGeHTLcjNKfaMStrWqbbz5N5neeAWzdC98') \
        .persistence(persistence) \
        .build()

    # Restore tracking jobs on startup
    async def on_startup(context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.application.persistence:
            logging.info("No persistence data available on startup")
            return
        
        # Await the coroutines to get the actual data
        chat_data = await context.application.persistence.get_chat_data()
        user_data = await context.application.persistence.get_user_data()
        group_chats = context.bot_data.get('group_chats', {})
        logging.info(f"Startup: Loaded group_chats with {len(group_chats)} entries: {group_chats}")

        # Restore tracking jobs for group chats
        for chat_id, data in chat_data.items():
            if 'tracked_tokens' in data:
                job_name = f"tracking_{chat_id}"
                if not context.job_queue.get_jobs_by_name(job_name):
                    context.job_queue.run_repeating(
                        check_for_updates,
                        interval=10,
                        first=10,
                        chat_id=int(chat_id),
                        name=job_name,
                        data={'is_group': True}
                    )
                    logging.info(f"Restored group tracking job for chat {chat_id}")

        # Restore tracking jobs for private user chats
        for user_id, data in user_data.items():
            if 'tracked_tokens' in data:
                job_name = f"tracking_{user_id}"
                if not context.job_queue.get_jobs_by_name(job_name):
                    context.job_queue.run_repeating(
                        check_for_updates,
                        interval=10,
                        first=10,
                        chat_id=int(user_id),
                        name=job_name,
                        data={'is_group': False}
                    )
                    logging.info(f"Restored user tracking job for user {user_id}")

    # Register handlers
    app.add_handler(CommandHandler('start', lambda u, c: u.message.reply_text("Use /track [ADDRESS] to start")))
    app.add_handler(CommandHandler('track', track_command))
    app.add_handler(CommandHandler('watching', watching))
    app.add_handler(CommandHandler('stop', stop_tracking))
    app.add_handler(CallbackQueryHandler(handle_replace_callback, pattern='^replace_'))
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(error_handler)
    app.job_queue.run_once(lambda c: asyncio.create_task(on_startup(c)), 1)
    app.job_queue.run_repeating(log_group_info, interval=300, first=0)  # Log group info every 5 minutes

    app.run_polling()

if __name__ == '__main__':
    main()