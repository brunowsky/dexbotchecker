import asyncio
import time
import aiohttp
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    DictPersistence,
    PersistenceInput,
    CallbackQueryHandler
)

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

MAX_TRACKING_SLOTS = 2

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error("Exception while handling update:", exc_info=context.error)

def get_storage(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Get appropriate storage based on chat type"""
    if context._chat_id and context._chat_id < 0:  # Group chat
        return context.chat_data
    return context.user_data

async def fetch_token_info(token_address: str) -> tuple[str | None, int | None, str | None]:
    """Fetch status, payment timestamp, and symbol"""
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

async def fetch_token_header(token_address: str) -> str | None:
    """Fetch token header image URL"""
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

def time_since(timestamp: int) -> str:
    """Convert timestamp to relative time string"""
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

async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage = get_storage(context)
    args = context.args
    
    if not args:
        await update.message.reply_text("Please provide a token address after /track")
        return

    token_address = args[0].strip()
    status, payment_ts, symbol = await fetch_token_info(token_address)
    if not status:
        await update.message.reply_text("Invalid token address or API error")
        return

    symbol = symbol or 'Unknown'

    if status.lower() in ['approved', 'updated']:
        time_ago = time_since(payment_ts)
        message = f"{symbol} ({token_address})\nStatus: {status}"
        if time_ago != "Unknown":
            message += f"\nPayment Time: {time_ago} ago"
        
        header = await fetch_token_header(token_address)
        if header and status.lower() == 'approved':
            await update.message.reply_photo(header, caption=message)
        else:
            await update.message.reply_text(message)
        return

    tracked_tokens = storage.setdefault('tracked_tokens', {})
    if token_address in tracked_tokens:
        await update.message.reply_text(f"Already tracking {token_address}")
        return

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

    tracked_tokens[token_address] = {
        'symbol': symbol,
        'last_status': status,
        'last_change': time.time()
    }

    message = f"Now tracking {symbol} ({token_address})\nInitial Status: {status}"
    header = await fetch_token_header(token_address)
    if header and status.lower() == 'processing':
        await update.message.reply_photo(header, caption=message)
    else:
        await update.message.reply_text(message)

    job_name = f"tracking_{context._chat_id}"
    if not context.job_queue.get_jobs_by_name(job_name):
        context.job_queue.run_repeating(
            check_for_updates,
            interval=10,
            first=10,
            chat_id=update.effective_chat.id,
            name=job_name,
            data={'is_group': context._chat_id < 0}
        )

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
        await query.edit_message_text("Invalid token address")
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
                await context.bot.send_photo(query.message.chat_id, header, caption=message)
                await query.edit_message_text(f"Slot {slot+1} replaced")
                return

    await query.edit_message_text(message)
    del storage['pending_token']

async def check_for_updates(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if job.data.get('is_group'):
        storage = context.chat_data
    else:
        storage = context.user_data
    
    tracked_tokens = storage.get('tracked_tokens', {})
    
    for token_address, token_info in list(tracked_tokens.items()):
        current_status, payment_ts, symbol = await fetch_token_info(token_address)
        if not current_status:
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
                    await context.bot.send_photo(job.chat_id, header, caption=message)
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
                    await context.bot.send_photo(job.chat_id, header, caption=final_message)
                else:
                    await context.bot.send_message(job.chat_id, final_message)

        elif time.time() - last_change > 1800:
            del tracked_tokens[token_address]
            await context.bot.send_message(job.chat_id,
                f"Stopped tracking {symbol} ({token_address}) - no changes for 30 minutes")

    storage['tracked_tokens'] = dict(list(tracked_tokens.items())[:MAX_TRACKING_SLOTS])
    if not storage['tracked_tokens']:
        job.schedule_removal()

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

def main() -> None:
    persistence = DictPersistence(store_data=PersistenceInput(user_data=True, chat_data=True))
    app = ApplicationBuilder() \
        .token('7839153642:AAGeHTLcjNKfaMStrWqbbz5N5neeAWzdC98') \
        .persistence(persistence) \
        .build()

    app.add_handler(CommandHandler('start', lambda u, c: u.message.reply_text("Use /track [ADDRESS] to start")))
    app.add_handler(CommandHandler('track', track_command))
    app.add_handler(CommandHandler('watching', watching))
    app.add_handler(CommandHandler('stop', stop_tracking))
    app.add_handler(CallbackQueryHandler(handle_replace_callback, pattern='^replace_'))
    app.add_error_handler(error_handler)

    app.run_polling()

if __name__ == '__main__':
    main()