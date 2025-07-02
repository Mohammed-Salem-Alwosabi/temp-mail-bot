import os
import requests
import json
import asyncio # New: For async operations
import asyncpg # New: PostgreSQL driver

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Mail.tm API base URL
MAILTM_API_URL = "https://api.mail.tm"

# --- Database Connection Pool ---
# We'll use a global variable for the connection pool
db_pool = None

# --- Database Functions ---

async def init_db_pool():
    """Initializes the PostgreSQL connection pool."""
    global db_pool
    if db_pool is None:
        try:
            # Railway automatically injects DATABASE_URL from the added PostgreSQL service
            # asyncpg can parse the DATABASE_URL directly
            db_pool = await asyncpg.create_pool(os.getenv("DATABASE_URL"))
            print("PostgreSQL connection pool created successfully.")
            await create_table() # Ensure the table exists
        except Exception as e:
            print(f"Error creating PostgreSQL connection pool: {e}")
            # Consider more robust error handling / retry logic here

async def create_table():
    """Creates the users_temp_emails table if it doesn't exist."""
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users_temp_emails (
                chat_id BIGINT PRIMARY KEY,
                address TEXT NOT NULL,
                account_id TEXT NOT NULL,
                token TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        print("Table 'users_temp_emails' checked/created.")

async def store_user_email(chat_id, email_data):
    """Stores a user's temporary email information in the database."""
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO users_temp_emails (chat_id, address, account_id, token)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (chat_id) DO UPDATE
            SET address = EXCLUDED.address,
                account_id = EXCLUDED.account_id,
                token = EXCLUDED.token,
                created_at = CURRENT_TIMESTAMP
        ''', chat_id, email_data["address"], email_data["id"], email_data["token"])
    print(f"Stored email for chat_id: {chat_id}")

async def get_user_email(chat_id):
    """Retrieves a user's temporary email information from the database."""
    async with db_pool.acquire() as conn:
        record = await conn.fetchrow('''
            SELECT address, account_id, token FROM users_temp_emails WHERE chat_id = $1
        ''', chat_id)
        if record:
            return {
                "address": record["address"],
                "id": record["account_id"],
                "token": record["token"]
            }
        return None
    print(f"Retrieved email for chat_id: {chat_id}")

async def delete_user_email_from_db(chat_id):
    """Deletes a user's temporary email information from the database."""
    async with db_pool.acquire() as conn:
        await conn.execute('''
            DELETE FROM users_temp_emails WHERE chat_id = $1
        ''', chat_id)
    print(f"Deleted email from DB for chat_id: {chat_id}")

# --- Mail.tm API Functions (No changes here, they are fine) ---

async def get_domains():
    """Fetches available Mail.tm domains."""
    try:
        response = requests.get(f"{MAILTM_API_URL}/domains")
        response.raise_for_status()

        if not response.text:
            print("Mail.tm /domains endpoint returned empty response.")
            return []

        data = response.json()

        if isinstance(data, dict) and 'hydra:member' in data:
            domains_list = data['hydra:member']
        else:
            print(f"Mail.tm /domains endpoint returned unexpected top-level data structure: {data}")
            if isinstance(data, list):
                domains_list = data
            else:
                return []

        if not isinstance(domains_list, list):
            print(f"Mail.tm /domains endpoint 'hydra:member' data is not a list: {domains_list}")
            return []

        if not domains_list:
            print("Mail.tm /domains endpoint returned an empty list of domains (or hydra:member was empty).")
            return []

        return [d["domain"] for d in domains_list if isinstance(d, dict) and "domain" in d]

    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error fetching domains: {e.response.status_code} - {e.response.text}")
        return []
    except requests.exceptions.ConnectionError as e:
        print(f"Connection Error fetching domains: {e}")
        return []
    except requests.exceptions.Timeout as e:
        print(f"Timeout Error fetching domains: {e}")
        return []
    except requests.exceptions.RequestException as e:
        print(f"Generic Request Error fetching domains: {e}")
        return []
    except json.JSONDecodeError as e:
        print(f"JSON Decode Error fetching domains: {e} - Response text: {response.text}")
        return []
    except Exception as e:
        print(f"An unexpected error occurred in get_domains: {e}")
        return []

async def create_account(username=None, domain=None):
    """Creates a new temporary email account."""
    if not domain:
        domains = await get_domains()
        if not domains:
            return None, "Could not fetch domains. Please try again later."
        domain = domains[0]

    if not username:
        import uuid
        username = str(uuid.uuid4()).split('-')[0]

    try:
        payload = {"address": f"{username}@{domain}", "password": "temp_password"}
        response = requests.post(f"{MAILTM_API_URL}/accounts", json=payload)
        response.raise_for_status()
        account_data = response.json()
        
        token_response = requests.post(f"{MAILTM_API_URL}/token", json={"address": account_data["address"], "password": "temp_password"})
        token_response.raise_for_status()
        token_data = token_response.json()

        return {
            "address": account_data["address"],
            "id": account_data["id"],
            "token": token_data["token"]
        }, None
    except requests.exceptions.RequestException as e:
        print(f"Error creating account: {e}")
        if response.status_code == 422:
            return None, f"Could not create address. It might already exist or the domain is invalid. Try generating a new one."
        return None, f"Failed to create temporary email: {e}"

async def get_messages(account_id, token):
    """Fetches messages for a given temporary email account."""
    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(f"{MAILTM_API_URL}/accounts/{account_id}/messages", headers=headers)
        response.raise_for_status()
        messages = response.json()
        return messages
    except requests.exceptions.RequestException as e:
        print(f"Error fetching messages: {e}")
        return []

async def get_message_content(account_id, message_id, token):
    """Fetches the content of a specific message."""
    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(f"{MAILTM_API_URL}/accounts/{account_id}/messages/{message_id}", headers=headers)
        response.raise_for_status()
        message_content = response.json()
        return message_content
    except requests.exceptions.RequestException as e:
        print(f"Error fetching message content: {e}")
        return None

async def delete_account(account_id, token):
    """Deletes a temporary email account."""
    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.delete(f"{MAILTM_API_URL}/accounts/{account_id}", headers=headers)
        response.raise_for_status()
        return True, None
    except requests.exceptions.RequestException as e:
        print(f"Error deleting account: {e}")
        return False, f"Failed to delete account: {e}"

# --- Telegram Bot Command Handlers (Modified to use DB) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and instructions."""
    await update.message.reply_text(
        "Hello! I'm your Temp Mail Bot. I can generate temporary email addresses for you.\n\n"
        "Use /generate to get a new temporary email address.\n"
        "Use /inbox to check for new messages in your current temporary email's inbox.\n"
        "Use /delete to delete your current temporary email address."
    )

async def generate_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generates a new temporary email address for the user."""
    chat_id = update.effective_chat.id
    
    current_email_info = await get_user_email(chat_id) # Check DB

    if current_email_info:
        keyboard = [[
            InlineKeyboardButton("Yes, generate new", callback_data="confirm_generate"),
            InlineKeyboardButton("No, keep current", callback_data="cancel_generate")
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"You already have an active temporary email: `{current_email_info['address']}`. "
            "Generating a new one will delete the old one. Are you sure you want to proceed?",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        return

    await update.message.reply_text("Generating a new temporary email address, please wait...")
    
    account_info, error = await create_account()
    if account_info:
        await store_user_email(chat_id, account_info) # Store in DB
        await update.message.reply_text(
            f"Your new temporary email address is:\n`{account_info['address']}`\n\n"
            "Use /inbox to check for messages."
            "Remember, this is temporary and emails are usually deleted after some time by the service itself.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"Sorry, I couldn't generate a temporary email address at this time. {error}")

async def inbox(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks the inbox of the current temporary email address."""
    chat_id = update.effective_chat.id

    current_email_info = await get_user_email(chat_id) # Get from DB

    if not current_email_info:
        await update.message.reply_text("You don't have an active temporary email address. Use /generate to get one.")
        return

    account_id = current_email_info["id"]
    token = current_email_info["token"]
    
    await update.message.reply_text("Checking your inbox, please wait...")
    
    messages = await get_messages(account_id, token)

    if messages:
        message_list = []
        for msg in messages:
            subject = msg.get('subject', 'No Subject')
            from_address = msg.get('from', {}).get('address', 'Unknown Sender')
            msg_id = msg.get('id')
            
            keyboard = [[InlineKeyboardButton(f"Subject: {subject} (From: {from_address})", callback_data=f"view_msg_{msg_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            message_list.append(f"• From: `{from_address}`\n  Subject: `{subject}`")
            await update.message.reply_text(
                f"**New Message**\n{message_list[-1]}",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        
    else:
        await update.message.reply_text("Your inbox is empty.")

async def delete_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deletes the current temporary email address."""
    chat_id = update.effective_chat.id

    current_email_info = await get_user_email(chat_id) # Get from DB

    if not current_email_info:
        await update.message.reply_text("You don't have an active temporary email address to delete.")
        return

    keyboard = [[
        InlineKeyboardButton("Yes, delete it", callback_data="confirm_delete"),
        InlineKeyboardButton("No, keep it", callback_data="cancel_delete")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Are you sure you want to delete your current temporary email address: `{current_email_info['address']}`?",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles inline keyboard button presses."""
    query = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()

    if query.data == "confirm_delete":
        current_email_info = await get_user_email(chat_id) # Get from DB
        if not current_email_info:
            await query.edit_message_text("No active email to delete.")
            return

        account_id = current_email_info["id"]
        token = current_email_info["token"]
        
        success, error = await delete_account(account_id, token)
        if success:
            await delete_user_email_from_db(chat_id) # Delete from DB
            await query.edit_message_text("Your temporary email address has been deleted successfully.")
        else:
            await query.edit_message_text(f"Failed to delete your temporary email address. {error}")
    
    elif query.data == "cancel_delete":
        await query.edit_message_text("Deletion cancelled. Your temporary email address remains active.")

    elif query.data == "confirm_generate":
        # Delete old email first
        current_email_info = await get_user_email(chat_id) # Get from DB
        if current_email_info:
            account_id = current_email_info["id"]
            token = current_email_info["token"]
            success, error = await delete_account(account_id, token)
            if not success:
                await query.edit_message_text(f"Could not delete old email: {error}. Please try /generate again.")
                return
            await delete_user_email_from_db(chat_id) # Delete from DB

        # Then generate new
        await query.edit_message_text("Generating a new temporary email address, please wait...")
        account_info, error = await create_account()
        if account_info:
            await store_user_email(chat_id, account_info) # Store in DB
            await query.edit_message_text(
                f"Your new temporary email address is:\n`{account_info['address']}`\n\n"
                "Use /inbox to check for messages."
                "Remember, this is temporary and emails are usually deleted after some time by the service itself.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(f"Sorry, I couldn't generate a temporary email address at this time. {error}")

    elif query.data == "cancel_generate":
        await query.edit_message_text("New email generation cancelled. Your current email remains active.")

    elif query.data.startswith("view_msg_"):
        message_id = query.data.split("_")[2]
        current_email_info = await get_user_email(chat_id) # Get from DB
        if not current_email_info:
            await query.edit_message_text("Session expired. Please generate a new email.")
            return
        
        account_id = current_email_info["id"]
        token = current_email_info["token"]

        message_content = await get_message_content(account_id, message_id, token)
        if message_content:
            subject = message_content.get('subject', 'No Subject')
            from_address = message_content.get('from', {}).get('address', 'Unknown Sender')
            # Prefer 'text' over 'html' if available for cleaner display in Telegram
            text_body = message_content.get('text', message_content.get('html', 'No content available')).strip()
            
            if len(text_body) > 4000: # Telegram message limit is 4096 characters
                text_body = text_body[:3900] + "\n\n... (Message truncated)"

            await query.edit_message_text(
                f"**Subject:** `{subject}`\n"
                f"**From:** `{from_address}`\n\n"
                f"**Content:**\n```\n{text_body}\n```",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("Could not retrieve message content.")


async def post_startup_init(application: Application):
    """Initializes database pool after the bot starts."""
    await init_db_pool()

async def pre_shutdown_cleanup(application: Application):
    """Closes database pool before bot shuts down."""
    global db_pool
    if db_pool:
        await db_pool.close()
        print("PostgreSQL connection pool closed.")


def main() -> None:
    """Starts the bot."""
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN environment variable not set.")
        print("Please set it before running the bot.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("generate", generate_email))
    application.add_handler(CommandHandler("inbox", inbox))
    application.add_handler(CommandHandler("delete", delete_email))
    
    # Register callback query handler for inline buttons
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Register handlers for application lifecycle events
    application.add_handler(CommandHandler("start", start)) # Make sure start handler is still here
    application.add_handler(CommandHandler("generate", generate_email))
    application.add_handler(CommandHandler("inbox", inbox))
    application.add_handler(CommandHandler("delete", delete_email))
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Add post-startup and pre-shutdown hooks
    application.post_init(post_startup_init)
    application.pre_shutdown(pre_shutdown_cleanup)


    print("Bot started. Listening for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
