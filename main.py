import os
import requests
import json
import asyncio
import asyncpg
import uuid # For generating random usernames

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

# --- Configuration ---
# Get your Telegram Bot Token from environment variables (important for Railway deployment)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Mail.tm API base URL
MAILTM_API_URL = "https://api.mail.tm"

# --- Database Connection Pool ---
# Global variable for the connection pool
db_pool = None

# --- Database Functions ---

async def init_db_pool():
    """Initializes the PostgreSQL connection pool and creates the table."""
    global db_pool
    if db_pool is None:
        try:
            # Railway automatically injects DATABASE_URL from the added PostgreSQL service
            db_pool = await asyncpg.create_pool(os.getenv("DATABASE_URL"))
            print("PostgreSQL connection pool created successfully.")
            await create_table() # Ensure the table exists
        except Exception as e:
            print(f"Error creating PostgreSQL connection pool: {e}")
            # Raising the exception here will stop the bot,
            # which is good if the database is essential.
            raise

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

async def store_user_email(chat_id: int, email_data: dict):
    """Stores or updates a user's temporary email information in the database."""
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
    print(f"Stored/updated email for chat_id: {chat_id}")

async def get_user_email(chat_id: int) -> dict | None:
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

async def delete_user_email_from_db(chat_id: int):
    """Deletes a user's temporary email information from the database."""
    async with db_pool.acquire() as conn:
        await conn.execute('''
            DELETE FROM users_temp_emails WHERE chat_id = $1
        ''', chat_id)
    print(f"Deleted email from DB for chat_id: {chat_id}")

# --- Mail.tm API Functions ---

async def get_domains() -> list[str]:
    """Fetches available Mail.tm domains."""
    try:
        response = requests.get(f"{MAILTM_API_URL}/domains", timeout=10) # Added timeout
        response.raise_for_status()

        if not response.text:
            print("Mail.tm /domains endpoint returned empty response.")
            return []

        data = response.json()

        if isinstance(data, dict) and 'hydra:member' in data:
            domains_list = data['hydra:member']
        elif isinstance(data, list): # Fallback if it's directly a list (less common based on logs)
            domains_list = data
        else:
            print(f"Mail.tm /domains endpoint returned unexpected top-level data structure: {data}")
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

async def create_account(username: str = None, domain: str = None) -> tuple[dict | None, str | None]:
    """Creates a new temporary email account."""
    if not domain:
        domains = await get_domains()
        if not domains:
            return None, "Could not fetch available domains from Mail.tm. Please try again later."
        domain = domains[0] # Use the first available domain by default

    if not username:
        username = str(uuid.uuid4()).split('-')[0] # Simple random string

    address = f"{username}@{domain}"
    password = "temp_password" # Password is required by Mail.tm but not used by us

    try:
        # Create account
        payload_create = {"address": address, "password": password}
        response_create = requests.post(f"{MAILTM_API_URL}/accounts", json=payload_create, timeout=10)
        response_create.raise_for_status()
        account_data = response_create.json()
        
        # Get authentication token for the new account
        payload_token = {"address": account_data["address"], "password": password}
        response_token = requests.post(f"{MAILTM_API_URL}/token", json=payload_token, timeout=10)
        response_token.raise_for_status()
        token_data = response_token.json()

        return {
            "address": account_data["address"],
            "id": account_data["id"],
            "token": token_data["token"]
        }, None
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error creating account: {e.response.status_code} - {e.response.text}")
        if e.response.status_code == 422: # Unprocessable Entity, often means address already exists
            return None, f"Could not create address '{address}'. It might already exist or the domain is invalid. Try generating a new one."
        return None, f"Failed to create temporary email due to API error: {e.response.status_code} - {e.response.text}"
    except requests.exceptions.RequestException as e:
        print(f"Generic Request Error creating account: {e}")
        return None, f"Failed to create temporary email due to connection issue: {e}"
    except Exception as e:
        print(f"An unexpected error occurred in create_account: {e}")
        return None, f"An unexpected error occurred: {e}"

async def get_messages(account_id: str, token: str) -> list[dict]:
    """Fetches messages for a given temporary email account."""
    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(f"{MAILTM_API_URL}/accounts/{account_id}/messages", headers=headers, timeout=10)
        response.raise_for_status()
        messages = response.json()
        return messages
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error fetching messages: {e.response.status_code} - {e.response.text}")
        if e.response.status_code == 404: # Account likely deleted by Mail.tm
            return [] # Return empty list as if no messages, or handle differently later
        return []
    except requests.exceptions.RequestException as e:
        print(f"Error fetching messages: {e}")
        return []

async def get_message_content(account_id: str, message_id: str, token: str) -> dict | None:
    """Fetches the content of a specific message."""
    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(f"{MAILTM_API_URL}/accounts/{account_id}/messages/{message_id}", headers=headers, timeout=10)
        response.raise_for_status()
        message_content = response.json()
        return message_content
    except requests.exceptions.RequestException as e:
        print(f"Error fetching message content: {e}")
        return None

async def delete_account(account_id: str, token: str) -> tuple[bool, str | None]:
    """Deletes a temporary email account from Mail.tm."""
    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.delete(f"{MAILTM_API_URL}/accounts/{account_id}", headers=headers, timeout=10)
        response.raise_for_status()
        return True, None
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error deleting account: {e.response.status_code} - {e.response.text}")
        if e.response.status_code == 404:
            return True, "Account already deleted on Mail.tm's side." # Treat 404 as success if account is gone
        return False, f"Failed to delete account from Mail.tm: {e.response.status_code} - {e.response.text}"
    except requests.exceptions.RequestException as e:
        print(f"Generic Request Error deleting account: {e}")
        return False, f"Failed to delete account due to connection issue: {e}"

# --- Telegram Bot Command Handlers ---

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
    
    current_email_info = await get_user_email(chat_id)

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
        await store_user_email(chat_id, account_info)
        await update.message.reply_text(
            f"Your new temporary email address is:\n`{account_info['address']}`\n\n"
            "Use /inbox to check for messages."
            "Remember, this is temporary and emails are usually deleted by Mail.tm after some time.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"Sorry, I couldn't generate a temporary email address at this time. {error}")

async def inbox(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks the inbox of the current temporary email address."""
    chat_id = update.effective_chat.id

    current_email_info = await get_user_email(chat_id)

    if not current_email_info:
        await update.message.reply_text("You don't have an active temporary email address. Use /generate to get one.")
        return

    account_id = current_email_info["id"]
    token = current_email_info["token"]
    
    await update.message.reply_text("Checking your inbox, please wait...")
    
    messages = await get_messages(account_id, token)

    if messages:
        # Filter out messages that might have an invalid ID or structure, though unlikely
        valid_messages = [msg for msg in messages if msg.get('id')]
        
        if not valid_messages:
            await update.message.reply_text("Your inbox is empty or messages could not be parsed.")
            return

        for msg in valid_messages:
            subject = msg.get('subject', 'No Subject')
            from_address = msg.get('from', {}).get('address', 'Unknown Sender')
            msg_id = msg.get('id')
            
            keyboard = [[InlineKeyboardButton(f"Subject: {subject} (From: {from_address})", callback_data=f"view_msg_{msg_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"**New Message**\n"
                f"• From: `{from_address}`\n"
                f"  Subject: `{subject}`",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        
    else:
        await update.message.reply_text("Your inbox is empty.")

async def delete_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deletes the current temporary email address."""
    chat_id = update.effective_chat.id

    current_email_info = await get_user_email(chat_id)

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
    await query.answer() # Acknowledge the callback query immediately

    if query.data == "confirm_delete":
        current_email_info = await get_user_email(chat_id)
        if not current_email_info:
            await query.edit_message_text("No active email to delete or session expired.")
            return

        account_id = current_email_info["id"]
        token = current_email_info["token"]
        
        success, error = await delete_account(account_id, token)
        if success:
            await delete_user_email_from_db(chat_id)
            await query.edit_message_text("Your temporary email address has been deleted successfully.")
        else:
            await query.edit_message_text(f"Failed to delete your temporary email address from Mail.tm. {error}")
    
    elif query.data == "cancel_delete":
        await query.edit_message_text("Deletion cancelled. Your temporary email address remains active.")

    elif query.data == "confirm_generate":
        # Delete old email from Mail.tm first, if exists and still valid
        current_email_info = await get_user_email(chat_id)
        if current_email_info:
            account_id = current_email_info["id"]
            token = current_email_info["token"]
            # Attempt to delete from Mail.tm. We don't care much if it fails, as it might already be gone.
            await delete_account(account_id, token) 
            await delete_user_email_from_db(chat_id) # Always remove from our DB
            
        # Then generate new
        await query.edit_message_text("Generating a new temporary email address, please wait...")
        account_info, error = await create_account()
        if account_info:
            await store_user_email(chat_id, account_info)
            await query.edit_message_text(
                f"Your new temporary email address is:\n`{account_info['address']}`\n\n"
                "Use /inbox to check for messages."
                "Remember, this is temporary and emails are usually deleted by Mail.tm after some time.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(f"Sorry, I couldn't generate a temporary email address at this time. {error}")

    elif query.data == "cancel_generate":
        await query.edit_message_text("New email generation cancelled. Your current email remains active.")

    elif query.data.startswith("view_msg_"):
        message_id = query.data.split("_")[2]
        current_email_info = await get_user_email(chat_id)
        if not current_email_info:
            await query.edit_message_text("Session expired or no active email. Please generate a new email.")
            return
        
        account_id = current_email_info["id"]
        token = current_email_info["token"]

        message_content = await get_message_content(account_id, message_id, token)
        if message_content:
            subject = message_content.get('subject', 'No Subject')
            from_address = message_content.get('from', {}).get('address', 'Unknown Sender')
            text_body = message_content.get('text', message_content.get('html', 'No content available')).strip()
            
            # Simple HTML stripping if needed, for better display of HTML content
            if '<html' in text_body.lower() and '<body' in text_body.lower():
                from html.parser import HTMLParser
                class HTMLStripper(HTMLParser):
                    def __init__(self):
                        super().__init__()
                        self.reset()
                        self.strict = False
                        self.convert_charrefs= True
                        self.text = []
                    def handle_data(self, data):
                        self.text.append(data)
                    def get_data(self):
                        return ''.join(self.text)
                
                stripper = HTMLStripper()
                stripper.feed(text_body)
                text_body = stripper.get_data()
                text_body = " ".join(text_body.split()).strip() # Remove excessive whitespace

            # Limit message length to avoid Telegram API limits
            if len(text_body) > 4000:
                text_body = text_body[:3900] + "\n\n... (Message truncated)"

            await query.edit_message_text(
                f"**Subject:** `{subject}`\n"
                f"**From:** `{from_address}`\n\n"
                f"**Content:**\n```\n{text_body}\n```",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("Could not retrieve message content. It might have expired or been deleted by Mail.tm.")

# --- Application Lifecycle Hooks ---

async def post_startup_init(application: Application):
    """Initializes database pool after the bot starts."""
    print("Running post_startup_init hook...")
    await init_db_pool()
    print("Bot fully initialized and connected to DB.")


async def pre_shutdown_cleanup(application: Application):
    """Closes database pool before bot shuts down."""
    global db_pool
    if db_pool:
        await db_pool.close()
        print("PostgreSQL connection pool closed during pre_shutdown_cleanup.")


# --- Main Bot Setup ---

def main() -> None:
    """Starts the bot."""
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN environment variable not set.")
        print("Please set it before running the bot.")
        return
    
    # Check for DATABASE_URL as well
    if not os.getenv("DATABASE_URL"):
        print("Error: DATABASE_URL environment variable not set.")
        print("Please ensure PostgreSQL database is added and linked in Railway.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register command handlers (ONLY ONCE)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("generate", generate_email))
    application.add_handler(CommandHandler("inbox", inbox))
    application.add_handler(CommandHandler("delete", delete_email))
    
    # Register callback query handler for inline buttons (ONLY ONCE)
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Add post-startup and pre-shutdown hooks
    application.post_init(post_startup_init)
    application.pre_shutdown(pre_shutdown_cleanup)

    print("Bot setup complete. Starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
